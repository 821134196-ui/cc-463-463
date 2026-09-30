"""模拟适配器:持续返回热柜温度读数与销售回执。

真实门店中这些来自 IoT 温控与 POS;本系统用可复现的脚本曲线模拟,
并可通过故障脚本制造"温度持续不合格""提前售罄"等样例。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class BatchSimState:
    """某批次的模拟脚本状态。"""
    base_temp: float = 68.0              # 围绕的中心温度
    phase: float = 0.0
    faulty: bool = False                 # True -> 持续超温
    missing: bool = False                # True -> 停止上报读数
    fault_at: float | None = None        # 多少模拟秒后进入故障
    sales_rate: float = 6.0              # 平均销售速度(份/模拟小时)
    sellout_early: bool = False          # 临期最后 2 分钟清空(自然售罄)
    sellout_after: float | None = None   # 上柜后 N 模拟秒强制清空(提前售罄)


class TemperatureAdapter:
    """模拟温控探头:正弦波动 + 随机抖动,故障后持续高于上限。"""

    def __init__(self, seed: int = 463):
        self.rng = random.Random(seed)

    def read(self, st: BatchSimState, t_seconds: float, temp_max: float) -> float | None:
        if st.missing:
            return None
        if st.fault_at is not None and t_seconds >= st.fault_at:
            st.faulty = True
        if st.faulty:
            return round(temp_max + 3.0 + self.rng.random() * 2.0, 1)
        wave = math.sin(t_seconds / 45.0 + st.phase) * 2.0
        return round(st.base_temp + wave + self.rng.uniform(-0.6, 0.6), 1)


class SalesAdapter:
    """模拟 POS 回执:按销售速率返回随机扣减份数(0~2)。"""

    def __init__(self, seed: int = 464):
        self.rng = random.Random(seed)

    def receipt(self, st: BatchSimState, remaining: int, minutes_left: float | None,
                on_shelf_seconds: float = 0.0, dt_seconds: float = 60.0) -> int:
        """按 份/模拟小时 的速率对本 tick 做泊松抽样。"""
        if remaining <= 0:
            return 0
        # 脚本安排:上柜一段时间后强制清空,制造"提前售罄"
        if st.sellout_after is not None and on_shelf_seconds >= st.sellout_after:
            return remaining
        # 临期最后 2 分钟一次性清空,自然走完效期(到期前售罄)
        if st.sellout_early and minutes_left is not None and minutes_left < 2:
            return remaining
        # 临期降价带动客流上升
        rate = st.sales_rate * (1.8 if minutes_left is not None and minutes_left < 20 else 1.0)
        lam = rate * dt_seconds / 3600.0
        # 小 lambda 泊松抽样(L Knuth),lambda 很小时多为 0,节奏接近真实零星购买
        l, k, p = pow(2.718281828, -lam), 0, 1.0
        while True:
            k += 1
            p *= self.rng.random()
            if p <= l:
                break
        return min(remaining, k - 1)
