"""进程内任务：定时计算临期/到期、拉取模拟温度与销售回执。"""
import threading
import time
import traceback

from . import config
from .adapters import SalesAdapter, TempAdapter
from .errors import HotCabError
from .service import BatchService


class Scheduler:
    def __init__(self, service: BatchService, store_id,
                 temp_adapter: TempAdapter = None,
                 sales_adapter: SalesAdapter = None, log=None):
        self.svc = service
        self.store_id = store_id
        self.temp_adapter = temp_adapter or TempAdapter()
        self.sales_adapter = sales_adapter or SalesAdapter()
        self._log = log or (lambda msg: None)
        self._stop = threading.Event()
        self.threads = []
        self.sales_count = 0
        self.temp_count = 0

    def log(self, msg):
        try:
            self._log(msg)
        except Exception:
            pass

    def _loop(self, name, interval, fn):
        while not self._stop.wait(interval):
            try:
                fn()
            except Exception:
                self.log(f"[scheduler:{name}] 后台任务异常\n"
                         + traceback.format_exc())

    def _do_expiry(self):
        r = self.svc.tick_expiry()
        if r["moved_to_pending"] or r["near_marked"]:
            self.log(f"[效期] 临期标记 {r['near_marked']}，"
                     f"到期转待下架 {r['moved_to_pending']}")

    def _do_temp(self):
        for b in self.svc.list_batches(store_id=self.store_id,
                                       status="on_sale"):
            t = self.temp_adapter.read(b)
            self.svc.ingest_temp(b["id"], t)
            self.temp_count += 1
            if t is None:
                self.log(f"[温度] 批次 {b['batch_no']} 探头读数缺失")
            elif not (b["temp_min"] <= t <= b["temp_max"]):
                self.log(f"[温度] 批次 {b['batch_no']} 读数 {t}℃ 超出 "
                         f"{b['temp_min']}-{b['temp_max']}℃")

    def _do_sales(self):
        batches = self.svc.list_batches(store_id=self.store_id,
                                        status="on_sale")
        receipts = self.sales_adapter.next_receipts(batches)
        for batch_id, qty in receipts:
            try:
                b = self.svc.sell(batch_id, qty, source="adapter")
                self.sales_count += 1
                if b["status"] == "sold_out":
                    self.log(f"[销售] 批次 {b['batch_no']} 提前售罄")
            except HotCabError as e:
                self.log(f"[销售] 回执丢弃：{e}")

    def start(self):
        jobs = (("expiry", config.TICK_EXPIRY, self._do_expiry),
                ("temp", config.TICK_TEMP, self._do_temp),
                ("sales", config.TICK_SALES, self._do_sales))
        for name, interval, fn in jobs:
            t = threading.Thread(target=self._loop, args=(name, interval, fn),
                                 name=f"hotcab-{name}", daemon=True)
            t.start()
            self.threads.append(t)
        self.log("[scheduler] 进程内任务已启动：效期/温度/销售")

    def run_once(self):
        """测试用：同步跑一拍。"""
        self._do_expiry()
        self._do_temp()
        self._do_sales()

    def stop(self):
        self._stop.set()
