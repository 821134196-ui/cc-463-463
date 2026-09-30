"""进程内后台任务:周期性计算临期/到期,拉取模拟温度与销售回执。

使用可加速的模拟时钟:sim_now = 启动时刻 + 真实流逝 * 时间倍率,
使数小时的保温效期可在几分钟内完整演示(出炉 -> 临期 -> 到期报损)。
"""
from __future__ import annotations

import threading
import time
from typing import Dict

from .adapters import BatchSimState, SalesAdapter, TemperatureAdapter
from .service import HotFoodService


class SimClock:
    def __init__(self, scale: float = 30.0, start: float | None = None):
        self.scale = scale
        self._start_real = time.time()
        self._start_sim = start if start is not None else self._start_real

    def now(self) -> float:
        return self._start_sim + (time.time() - self._start_real) * self.scale

    def sleep_sim(self, sim_seconds: float) -> None:
        time.sleep(sim_seconds / self.scale)


class BackgroundTask(threading.Thread):
    """守护线程:每个 tick 扫描效期并喂入模拟温度/销售。"""

    def __init__(self, svc: HotFoodService, store_id: str, clock: SimClock,
                 temp_adapter: TemperatureAdapter, sales_adapter: SalesAdapter,
                 interval_real: float = 2.0):
        super().__init__(daemon=True, name="hotfood-bg")
        self.svc = svc
        self.store_id = store_id
        self.clock = clock
        self.temp_adapter = temp_adapter
        self.sales_adapter = sales_adapter
        self.interval = interval_real
        self.states: Dict[str, BatchSimState] = {}
        self._stop = threading.Event()

    def register(self, batch_id: str, **kwargs) -> None:
        self.states[batch_id] = BatchSimState(**kwargs)

    def state(self, batch_id: str) -> BatchSimState:
        return self.states.setdefault(batch_id, BatchSimState())

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 后台异常不得杀死线程
                print(f"[bg] tick 异常: {exc!r}")

    def tick(self) -> dict:
        with self.svc.lock:
            counts = self.svc.scan_expiry(emit_overlap=self.svc.log_overlap)
            dt = self.interval * self.clock.scale  # 一个 tick 对应的模拟秒数
            for b in self.svc.list_batches(self.store_id):
                if b["status"] != "placed":
                    continue
                st = self.state(b["id"])
                t_sim = max(0.0, self.clock.now() - (b["out_at"] or self.clock.now()))
                reading = self.temp_adapter.read(st, t_sim, b["temp_max"])
                if reading is not None:
                    self.svc.add_sensor_reading(b["id"], reading)
                # 读数为 None 表示探头失联,scan_expiry 会在宽限期后留痕 temp_missing
                minutes_left = ((b["expires_at"] - self.clock.now()) / 60
                                if b["expires_at"] else None)
                qty = self.sales_adapter.receipt(st, b["qty_remaining"], minutes_left,
                                                 on_shelf_seconds=t_sim, dt_seconds=dt)
                if qty > 0:
                    try:
                        self.svc.sell(b["id"], qty, actor="POS模拟",
                                      receipt_no=f"SIM-{int(self.clock.now())}-{b['batch_no']}")
                    except Exception as exc:
                        self.svc.audit(self.store_id, "sim_sale_rejected",
                                       {"batch_no": b["batch_no"], "qty": qty, "error": str(exc)},
                                       b["id"], actor="POS模拟")
            self.svc.commit()
            return counts
