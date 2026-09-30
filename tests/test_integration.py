"""集成测试:后台 tick 驱动模拟适配器 + HTTP API 全链路。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.adapters import SalesAdapter, TemperatureAdapter
from app.db import open_db
from app.service import HotFoodService
from app.tasks import BackgroundTask
from app.web import make_handler

STORE = "S"


class ControlClock:
    """可手动推进的时钟,同时提供 time.time 风格调用与 .now()。"""
    def __init__(self, t=2_000_000.0):
        self.t = t
        self.scale = 1.0

    def __call__(self):
        return self.t

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class SimulationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = open_db(self.dir / "i.db")
        self.clock = ControlClock()
        self.svc = HotFoodService(self.conn, clock=self.clock,
                                  attachment_dir=self.dir / "att")
        self.svc.add_store(STORE, "集成测试店")
        self.svc.add_product("P1", STORE, "C1", "便当", "便当", 15.0, 60, 72, 100, [0.8, 0.7])
        self.svc.add_shelf("SH1", STORE, "柜", 1, "便当区", "便当", 40)
        self.bg = BackgroundTask(self.svc, STORE, self.clock,
                                 TemperatureAdapter(seed=1), SalesAdapter(seed=2),
                                 interval_real=1)
        # tick 内 dt = interval * scale
        self.clock.scale = 60.0  # 每个 tick 代表 60 模拟秒

    def tearDown(self):
        self.bg.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _place(self, qty=10):
        bid = self.svc.make_batch("P1", qty, "员工")
        self.svc.record_probe(bid, 66.0, "员工")
        self.svc.place_on_shelf(bid, "SH1", "员工")
        return bid

    def test_persistent_overtemp_pulls_batch(self):
        bid = self._place()
        self.bg.register(bid, base_temp=66.0, fault_at=0, sales_rate=0)
        for _ in range(3):
            self.clock.advance(60)
            self.bg.tick()
        self.assertEqual(self.svc._batch(bid)["status"], "pending")
        self.assertEqual(self.svc._batch(bid)["waste_reason"], "temp")

    def test_missing_sensor_flagged_then_pending_on_expiry(self):
        bid = self._place()
        self.bg.register(bid, missing=True, sales_rate=0)
        for _ in range(21):                       # 21 模拟分钟无读数
            self.clock.advance(60)
            self.bg.tick()
        self.assertEqual(self.svc._batch(bid)["temp_state"], "missing")
        kinds = {e["event_type"] for e in self.svc.events(STORE, 999)}
        self.assertIn("temp_missing", kinds)
        for _ in range(85):                       # 走到 100 分钟到期
            self.clock.advance(60)
            self.bg.tick()
        self.assertEqual(self.svc._batch(bid)["status"], "pending")
        self.assertEqual(self.svc._batch(bid)["waste_reason"], "expired")

    def test_sales_receipts_deduct_and_eventual_waste(self):
        bid = self._place(qty=30)
        self.bg.register(bid, sales_rate=0)       # 不卖,留到到期报损
        for _ in range(100):
            self.clock.advance(60)
            self.bg.tick()
        self.assertEqual(self.svc._batch(bid)["status"], "pending")
        self.svc.confirm_waste(bid, 29, "员工")   # 账上 30,实物 29 -> 差异
        self.assertEqual(self.svc._batch(bid)["wasted_qty"], 29)
        variances = [e for e in self.svc.events(STORE, 999) if e["event_type"] == "qty_variance"]
        self.assertTrue(variances)


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.conn = open_db(cls.dir / "h.db")
        cls.clock = ControlClock()
        cls.svc = HotFoodService(cls.conn, clock=cls.clock, attachment_dir=cls.dir / "att")
        cls.svc.add_store(STORE, "HTTP店")
        cls.svc.add_product("P1", STORE, "C1", "便当", "便当", 15.0, 60, 72, 100, [0.8])
        cls.svc.add_shelf("SH1", STORE, "柜", 1, "便当区", "便当", 40)
        cls.bg = BackgroundTask(cls.svc, STORE, cls.clock,
                                TemperatureAdapter(), SalesAdapter())
        handler = make_handler(cls.svc, cls.bg, cls.clock,
                               Path(__file__).resolve().parent.parent / "app" / "static")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.conn.close()
        cls.tmp.cleanup()

    def call(self, path, body=None, role="clerk"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps({"role": role, **body}).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_full_flow_over_http(self):
        s, r = self.call("/api/batch/make", {"product_id": "P1", "qty": 8})
        self.assertEqual(s, 200)
        bid = r["batch_id"]
        # 未抽检不能上柜
        s, r = self.call("/api/batch/place", {"batch_id": bid, "shelf_id": "SH1"})
        self.assertEqual(s, 400)
        # 抽检 -> 上柜
        self.assertEqual(self.call("/api/batch/probe", {"batch_id": bid, "temp": 67})[0], 200)
        self.assertEqual(self.call("/api/batch/place", {"batch_id": bid, "shelf_id": "SH1"})[0], 200)
        # 乐观锁:并发旧版本 409
        self.assertEqual(self.call("/api/sell",
                         {"batch_id": bid, "qty": 1, "expected_version": 0})[0], 200)
        s, r = self.call("/api/sell", {"batch_id": bid, "qty": 1, "expected_version": 0})
        self.assertEqual(s, 409)
        self.assertIn("版本", r["error"])
        # 店员非临期预设折扣被拒
        s, r = self.call("/api/discount", {"batch_id": bid, "discount": 0.8})
        self.assertEqual(s, 400)
        # 到期:tick 自动转待下架
        exp = self.svc._batch(bid)["expires_at"]
        self.clock.t = exp
        s, r = self.call("/api/tick", {})
        self.assertEqual(s, 200)
        self.assertGreaterEqual(r["counts"]["expired"], 1)
        # 报损不可逆
        self.assertEqual(self.call("/api/waste", {"batch_id": bid, "counted_qty": 6})[0], 200)
        s, r = self.call("/api/sell", {"batch_id": bid, "qty": 1})
        self.assertEqual(s, 400)
        self.assertIn("报损", r["error"])

    def test_static_assets_served(self):
        for path in ("/", "/styles.css", "/app.js"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                self.assertEqual(r.status, 200)
                self.assertGreater(len(r.read()), 100)


if __name__ == "__main__":
    unittest.main()
