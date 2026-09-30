"""HTTP 端到端测试：JSON API、乐观锁 409、折扣权限 403、报损不可逆、附件。"""
import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime

_TEST_DATA = tempfile.mkdtemp(prefix="hotcab-webtest-")
os.environ["HOTCAB_DATA"] = _TEST_DATA

from hotcab.clock import FakeClock
from hotcab.database import init_db
from hotcab.seed import seed_master
from hotcab.webapp import App, create_server


def request(url, method="GET", body=None, role="clerk", actor="clerk01"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Role", role)
    req.add_header("X-Actor", actor)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def request_raw(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read()


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "t.db")
        os.environ["HOTCAB_DATA"] = cls.tmp.name
        init_db(cls.db)
        seed_master(cls.db)
        cls.clock = FakeClock(datetime(2026, 9, 30, 12, 0, 0))
        cls.httpd, cls.app = create_server("127.0.0.1", 0,
                                           clock=cls.clock, db_path=cls.db)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    @property
    def svc(self):
        return self.app.svc

    def prepare_on_sale(self, product_id=3, qty=10):
        st, b = request(f"{self.base}/api/batches", "POST",
                        {"product_id": product_id, "qty": qty})
        bid = b["id"]
        self.assertEqual(st, 200)
        st, b = request(f"{self.base}/api/batches/{bid}/check-temp", "POST",
                        {"temp": 70.0})
        self.assertEqual(st, 200)
        shelf = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}[product_id]
        st, b = request(f"{self.base}/api/batches/{bid}/assign", "POST",
                        {"shelf_id": shelf})
        self.assertEqual(st, 200)
        return b

    def test_full_flow_and_pages(self):
        for path in ("/", "/app.css", "/app.js"):
            st, payload = request_raw(self.base + path)
            self.assertEqual(st, 200, path)
            self.assertTrue(len(payload) > 100)
        for path in ("/api/ref", "/api/cabinet", "/api/queue",
                     "/api/pending", "/api/sales", "/api/losses",
                     "/api/report", "/api/audit", "/api/batches"):
            st, _ = request(self.base + path)
            self.assertEqual(st, 200, path)

    def test_concurrent_sell_old_version_409(self):
        b = self.prepare_on_sale(qty=10)
        v = b["version"]
        st, _ = request(f"{self.base}/api/batches/{b['id']}/sell", "POST",
                        {"qty": 2, "version": v})
        self.assertEqual(st, 200)
        st, err = request(f"{self.base}/api/batches/{b['id']}/sell", "POST",
                          {"qty": 1, "version": v})
        self.assertEqual(st, 409)
        self.assertEqual(err["code"], "version_conflict")

    def test_clerk_non_preset_403_manager_200(self):
        b = self.prepare_on_sale(qty=8)
        self.clock.advance(minutes=170)
        # 店员不能用未预设的 85 折
        st, err = request(f"{self.base}/api/batches/{b['id']}/discount",
                          "POST", {"rate": 85, "version": b["version"]},
                          role="clerk")
        self.assertEqual(st, 403)
        # 店长可以例外价
        st, b2 = request(f"{self.base}/api/batches/{b['id']}/discount",
                         "POST", {"rate": 85, "label": "店长85折"},
                         role="manager", actor="manager01")
        self.assertEqual(st, 200)
        self.assertEqual(b2["discount_rate"], 85)

    def test_loss_irreversible_over_http(self):
        b = self.prepare_on_sale(qty=5)
        self.clock.advance(minutes=180)
        self.svc.tick_expiry()
        st, r = request(f"{self.base}/api/batches/{b['id']}/loss", "POST",
                        {"counted_qty": 5, "reason": "expired"},
                        role="manager")
        self.assertEqual(st, 200)
        # 已报损不能销售
        st, err = request(f"{self.base}/api/batches/{b['id']}/sell", "POST",
                          {"qty": 1})
        self.assertEqual(st, 400)
        self.assertIn("报损", err["message"])
        # 不能二次报损
        st, _ = request(f"{self.base}/api/batches/{b['id']}/loss", "POST",
                        {"counted_qty": 5}, role="manager")
        self.assertEqual(st, 400)

    def test_attachment_upload(self):
        st, b = request(f"{self.base}/api/batches", "POST",
                        {"product_id": 3, "qty": 4})
        bid = b["id"]
        png = base64.b64encode(b"\x89PNG fake-bytes").decode()
        st, b = request(f"{self.base}/api/batches/{bid}/check-temp", "POST",
                        {"temp": 68.5,
                         "attachment": {"filename": "probe.jpg",
                                        "data_base64": png}})
        self.assertEqual(st, 200)
        self.assertTrue(b["check_attachment"])
        self.assertTrue(os.path.isfile(b["check_attachment"]))

    def test_wrong_shelf_400(self):
        st, b = request(f"{self.base}/api/batches", "POST",
                        {"product_id": 5, "qty": 4})  # 面点
        request(f"{self.base}/api/batches/{b['id']}/check-temp", "POST",
                {"temp": 64.0})
        st, err = request(f"{self.base}/api/batches/{b['id']}/assign",
                          "POST", {"shelf_id": 2})  # 炸物层
        self.assertEqual(st, 400)
        self.assertIn("层位", err["message"])


if __name__ == "__main__":
    unittest.main()
