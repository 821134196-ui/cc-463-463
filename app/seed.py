"""样例数据:启动即生成高峰补货、温度异常、临期重叠、提前售罄、闭店报损等场景。

商品与销售数据均为样例。时间以模拟时钟的"现在"为基准反推。
"""
from __future__ import annotations

import random
import time
import uuid

from .tasks import BackgroundTask, SimClock
from .service import HotFoodService

STORE = ("S1", "鲜时便利 · 人民广场店")

# (id, 编码, 名称, 品类, 基础价, 温度下限, 温度上限, 效期分钟, 预设折扣)
PRODUCTS = [
    ("P-BD01", "BD01", "照烧鸡排便当", "便当", 15.9, 60, 75, 240, [0.8, 0.7]),
    ("P-BD02", "BD02", "黑椒牛柳便当", "便当", 18.5, 60, 75, 240, [0.8, 0.7]),
    ("P-FT01", "FT01", "金枪鱼饭团", "饭团", 6.9, 55, 70, 180, [0.85, 0.7]),
    ("P-FT02", "FT02", "鲜蔬豆腐饭团", "饭团", 6.5, 55, 70, 180, [0.85, 0.7]),
    ("P-FR01", "FR01", "唐扬鸡块", "炸物", 9.9, 60, 72, 120, [0.8, 0.6]),
    ("P-BZ01", "BZ01", "鲜肉大包", "包点", 4.5, 60, 72, 180, [0.8, 0.7]),
    ("P-XC01", "XC01", "炭烤肠", "小吃", 5.5, 60, 72, 150, [0.8, 0.65]),
]

# (id, 层序, 标签, 品类区, 容量)
SHELVES = [
    ("SH-1", 1, "第1层 · 便当区", "便当", 36),
    ("SH-2", 2, "第2层 · 饭团区", "饭团", 30),
    ("SH-3", 3, "第3层 · 炸物区", "炸物", 48),
    ("SH-4", 4, "第4层 · 包点区", "包点", 30),
]


def _uid() -> str:
    return uuid.uuid4().hex


def seed(svc: HotFoodService, bg: BackgroundTask, T: float) -> None:
    rng = random.Random(7)
    svc.add_store(*STORE)
    for pid, code, name, cat, price, lo, hi, life, discs in PRODUCTS:
        svc.add_product(pid, STORE[0], code, name, cat, price, lo, hi, life, discs)
    for sid, idx, label, zone, cap in SHELVES:
        svc.add_shelf(sid, STORE[0], "1号热柜", idx, label, zone, cap)

    pid_of = {p[2]: p[0] for p in PRODUCTS}
    shelf_of_zone = {s[3]: s[0] for s in SHELVES}
    life_of = {p[2]: p[7] for p in PRODUCTS}

    def fresh_batch(product_name: str, qty: int, out_offset_min: float,
                    shelf_zone: str | None, probe_temp: float = 67.0,
                    attach: bool = False, sensor_gap_min: float = 0.0):
        """出炉 -> 抽检 -> 上柜,随后把时间反推到 T-out_offset_min。

        sensor_gap_min>0 时,历史传感器读数在距现在该分钟数处中断(模拟探头失联)。
        """
        pid = pid_of[product_name]
        bid = svc.make_batch(pid, qty, actor="值班店长", produced_at=T)
        payload = (b"\xff\xd8\xff\xe0sample-probe-photo" if attach else None)
        svc.record_probe(bid, probe_temp, actor="值班店长", attachment=payload,
                         note="出炉抽检,中心温度")
        if shelf_zone is not None:
            svc.place_on_shelf(bid, shelf_of_zone[shelf_zone], actor="值班店长")
            life = life_of[product_name]
            out_at = T - out_offset_min * 60
            svc.conn.execute(
                "UPDATE batches SET produced_at=?, probed_at=?, out_at=?, expires_at=?, updated_at=? WHERE id=?",
                (out_at - 5 * 60, out_at - 2 * 60, out_at, out_at + life * 60, out_at, bid))
            # 抽检读数同步回拨
            svc.conn.execute(
                "UPDATE temp_readings SET ts=? WHERE batch_id=? AND source='probe'",
                (out_at - 2 * 60, bid))
            # 回填历史传感器读数;sensor_gap_min>0 时读数在该时刻后中断
            cur = out_at
            while cur < T - max(60, sensor_gap_min * 60):
                t = rng.uniform(65.5, 69.5)
                svc.conn.execute(
                    "INSERT INTO temp_readings(id,batch_id,ts,temp,source,ok) VALUES(?,?,?,?, 'sensor',1)",
                    (_uid(), bid, cur, round(t, 1)))
                cur += 20 * 60
            # 回填几笔历史销售
            sold = 0
            for _ in range(rng.randint(1, 3)):
                q = rng.randint(1, 3)
                if sold + q >= qty:
                    break
                price = next(p[4] for p in PRODUCTS if p[0] == pid)
                svc.conn.execute(
                    "INSERT INTO sales(id,batch_id,ts,qty,unit_price,receipt_no,created_by) VALUES(?,?,?,?,?,?,?)",
                    (_uid(), bid, cur - rng.randint(5, 50) * 60, q, price,
                     f"H-{_uid()[:8]}", "POS"))
                sold += q
            svc.conn.execute("UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?", (sold, bid))
        return bid

    # 1) 早高峰补货(健康批次)
    b1 = fresh_batch("照烧鸡排便当", 16, 10, "便当")
    b2 = fresh_batch("唐扬鸡块", 20, 9, "炸物")
    b3 = fresh_batch("鲜肉大包", 12, 8, "包点", attach=True)

    # 2) 温度异常:上柜后探头持续超温
    b4 = fresh_batch("金枪鱼饭团", 10, 1, "饭团")
    bg.register(b4, base_temp=66.0, fault_at=60, sales_rate=3.0)

    # 3) 温度读数缺失:探头失联超过 20 分钟
    b5 = fresh_batch("金枪鱼饭团", 8, 26, "饭团", sensor_gap_min=22)
    bg.register(b5, missing=True, sales_rate=1.5)

    # 4) 临期重叠:两份便当分别 14 / 22 分钟后到期
    b6 = fresh_batch("照烧鸡排便当", 10, 226, "便当")
    b7 = fresh_batch("黑椒牛柳便当", 6, 218, "便当")
    bg.register(b6, sales_rate=0.8)
    bg.register(b7, sales_rate=0.8)

    # 5) 到期前售罄 + 上错层位(小吃放到包点区)
    b8 = fresh_batch("炭烤肠", 15, 145, "包点")
    bg.register(b8, sellout_early=True, sales_rate=4.0)

    # 5b) 提前售罄:上柜仅约 7 模拟分钟即清空,而效期还剩近 2 小时(高峰备货不足)
    b10 = fresh_batch("唐扬鸡块", 18, 3, "炸物")
    bg.register(b10, sellout_after=7 * 60, sales_rate=2.0)

    # 6) 到期边界:约 70 模拟秒后到期,自动转待下架
    b9 = fresh_batch("唐扬鸡块", 9, 118.83, "炸物")
    bg.register(b9, sales_rate=1.0)

    # 常规销售速率(份/模拟小时)
    bg.register(b1, sales_rate=3.0)
    bg.register(b2, sales_rate=8.0)
    bg.register(b3, sales_rate=3.5)

    # 7) 已出炉待抽检(供界面走"抽检 -> 上柜"流程)
    svc.make_batch(pid_of["鲜蔬豆腐饭团"], 10, actor="早班员工", produced_at=T - 3 * 60)

    # 8) 出炉抽检不合格:不得上柜,直接待下架
    bad = svc.make_batch(pid_of["照烧鸡排便当"], 8, actor="早班员工", produced_at=T - 4 * 60)
    svc.record_probe(bad, 81.6, actor="早班员工", note="中心温度超标")

    # 9) 历史已报损:昨夜闭店剩余 + 今晨到期,供日报统计
    def wasted_batch(product_name: str, qty_initial: str, wasted_qty: int,
                     produced_offset_h: float, wasted_offset_h: float, reason: str):
        p = next(x for x in PRODUCTS if x[2] == product_name)
        bid = _uid()
        produced = T - produced_offset_h * 3600
        wasted = T - wasted_offset_h * 3600
        seq = svc.conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] + 1
        svc.conn.execute(
            """INSERT INTO batches(id,batch_no,store_id,product_id,produced_at,probed_at,probe_temp,
                   shelf_id,out_at,expires_at,qty_initial,qty_remaining,status,waste_reason,
                   temp_state,wasted_qty,wasted_at,version,created_by,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'discarded',?,'ok',?,?,1,'值班店长',?)""",
            (bid, f"B{time.strftime('%m%d', time.localtime(produced))}-{seq:03d}", STORE[0], p[0],
             produced, produced + 180, 66.5, shelf_of_zone[p[3]], produced + 300,
             produced + p[6] * 60, qty_initial, 0, reason, wasted_qty, wasted, wasted))
        svc.conn.execute(
            "INSERT INTO sales(id,batch_id,ts,qty,unit_price,receipt_no,created_by) VALUES(?,?,?,?,?,?,?)",
            (_uid(), bid, produced + 3600, qty_initial - wasted_qty, p[4],
             f"H-{_uid()[:8]}", "POS"))
        svc.audit(STORE[0], "wasted",
                  {"batch_no": f"历史-{product_name}", "reason": reason,
                   "counted_qty": wasted_qty, "system_qty": wasted_qty}, bid, "值班店长")

    wasted_batch("鲜肉大包", 14, 9, 20.5, 17.0, "close_remaining")
    wasted_batch("唐扬鸡块", 12, 7, 5.5, 3.6, "expired")

    svc.commit()
