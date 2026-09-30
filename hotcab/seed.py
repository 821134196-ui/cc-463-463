"""主数据与样例数据：一条命令生成高峰补货、温度异常与闭店报损等场景。

样例中的商品与销售数据均为虚构。时间相对真实当前时刻落位，启动后界面即处于
“营业中”状态。
"""
import json
import os
from contextlib import contextmanager
from datetime import timedelta

from . import config
from .clock import OffsetClock, SystemClock
from .database import connect, init_db
from .errors import ValidationError
from .service import BatchService

MASTER_SQL = """
INSERT INTO stores(id, code, name) VALUES (1, 'S0463', '463 号示范便利店');
INSERT INTO cabinets(id, store_id, code, name, shelf_count)
VALUES (1, 1, 'HC-01', '前台热柜', 4);
INSERT INTO shelves(id, cabinet_id, position, label, category) VALUES
 (1, 1, 1, '第一层·热餐', '热餐'),
 (2, 1, 2, '第二层·炸物', '炸物'),
 (3, 1, 3, '第三层·面点', '面点'),
 (4, 1, 4, '第四层·小食', '小食');
INSERT INTO products(id, sku, name, category, life_minutes, temp_min,
                     temp_max, unit_price, unit_cost) VALUES
 (1, 'RICE-01', '台式卤肉饭',   '热餐', 240, 60, 85, 13.9, 6.5),
 (2, 'RICE-02', '咖喱鸡肉饭',   '热餐', 240, 60, 85, 14.9, 7.0),
 (3, 'FRY-01',  '香辣鸡腿',     '炸物', 180, 60, 80,  6.9, 3.1),
 (4, 'FRY-02',  '唐扬鸡块',     '炸物', 180, 60, 80,  9.9, 4.6),
 (5, 'DOU-01',  '鲜肉大包',     '面点', 360, 55, 75,  3.5, 1.4),
 (6, 'DOU-02',  '玉米蒸饺(4只)','面点', 300, 55, 75,  7.9, 3.6),
 (7, 'SNK-01',  '火山石烤肠',   '小食', 480, 55, 75,  4.5, 1.9),
 (8, 'SNK-02',  '照烧鸡排串',   '小食', 360, 55, 75,  6.5, 3.0);
INSERT INTO users(id, username, name, role) VALUES
 (1, 'clerk01', '小林(店员)', 'clerk'),
 (2, 'manager01', '阿店长', 'manager');
"""

STORE_ID = 1


def reset_db(db_path=None):
    path = db_path or config.DB_PATH
    config.ensure_dirs()
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    init_db(path)


def seed_master(db_path=None):
    conn = connect(db_path)
    try:
        conn.executescript(MASTER_SQL)
        conn.commit()
    finally:
        conn.close()


def seed_demo(db_path=None):
    """生成完整样例。返回场景标记（供适配器加载故障/高动销批次）。"""
    svc = BatchService(SystemClock(), db_path)
    base_clock = SystemClock()
    scenario = {"faulty": [], "hot": []}

    @contextmanager
    def offset(minutes):
        svc.clock = OffsetClock(base_clock, timedelta(minutes=-minutes))
        try:
            yield
        finally:
            svc.clock = base_clock

    def fresh(product_id, qty, ago_minutes, temp=72.0, shelf_hint=None,
              no=None):
        """ago_minutes 前出炉、测温、上柜，返回批次。"""
        with offset(ago_minutes):
            b = svc.create_batch(STORE_ID, product_id, qty,
                                 actor="阿店长", batch_no=no)
            svc.check_temp(b["id"], temp, actor="小林(店员)")
            if shelf_hint is None:
                shelf_hint = _shelf_for(svc, b["product_id"])
            b = svc.assign_shelf(b["id"], shelf_hint, actor="小林(店员)")
        return svc.get_batch(b["id"])

    # ---- 午间高峰补货（4 锅新鲜在售） ----
    b_rice = fresh(1, 12, 6, no="B20260930A01")
    b_curry = fresh(2, 10, 4, no="B20260930A02")
    b_chicken = fresh(3, 16, 5, temp=68.0, no="B20260930A03")
    b_dog = fresh(7, 20, 8, temp=62.0, no="B20260930A04")
    scenario["hot"] = [b_chicken["batch_no"]]

    # 高峰已有几笔销售
    svc.sell(b_rice["id"], 2, actor="小林(店员)")
    svc.sell(b_chicken["id"], 5, actor="小林(店员)")
    svc.sell(b_dog["id"], 3, actor="小林(店员)")

    # ---- 临期：同品两批重叠（卤肉饭 18 / 12 分钟） ----
    b_near1 = fresh(1, 8, 240 - 18, no="B20260930N01")
    b_near2 = fresh(1, 6, 240 - 12, no="B20260930N02")
    svc.tick_expiry()  # 产生 near_expiry 与 near_overlap 留痕

    # 临期已由店员执行 9 折
    svc.apply_discount(b_near2["id"], 90, role="clerk",
                       actor="小林(店员)", label="临期9折")
    svc.sell(b_near2["id"], 2, actor="小林(店员)")

    # ---- 温度异常批次：预铺 2 次不合格读数，适配器再喂 1 次即转待下架 ----
    b_fault = fresh(8, 10, 20, temp=68.0, no="B20260930T01")
    svc.ingest_temp(b_fault["id"], 52.4, note="探头读数偏低")
    svc.ingest_temp(b_fault["id"], 51.8, note="持续偏低")
    scenario["faulty"] = [b_fault["batch_no"]]

    # ---- 出炉测温读数缺失：批次卡住，不能上柜（留痕） ----
    b_missing = svc.create_batch(STORE_ID, 4, 6, actor="阿店长",
                                 batch_no="B20260930M01")
    svc.check_temp(b_missing["id"], None, note="测温枪未同步，读数缺失",
                   actor="小林(店员)")

    # ---- 已测温待上柜 ----
    b_ready = svc.create_batch(STORE_ID, 6, 8, actor="阿店长",
                               batch_no="B20260930R01")
    svc.check_temp(b_ready["id"], 66.5, actor="小林(店员)")

    # ---- 上错层位留痕：面点批次试图放进炸物层（被拒绝） ----
    b_wrong = svc.create_batch(STORE_ID, 5, 8, actor="阿店长",
                               batch_no="B20260930W01")
    svc.check_temp(b_wrong["id"], 64.0, actor="小林(店员)")
    try:
        svc.assign_shelf(b_wrong["id"], 2, actor="新人小赵")
    except ValidationError:
        pass
    svc.assign_shelf(b_wrong["id"], 3, actor="新人小赵")

    # ---- 到期批次：效期已过 1 分钟，tick 后自动转待下架 ----
    b_exp = fresh(4, 9, 180 + 1, temp=70.0, no="B20260930E01")
    svc.tick_expiry()

    # ---- 早高峰闭店剩余场景（历史批次，多数已报损，留 2 个待清点） ----
    with offset(9 * 60 + 30):
        h1 = svc.create_batch(STORE_ID, 3, 14, actor="阿店长",
                              batch_no="B20260930H01")
        svc.check_temp(h1["id"], 69.0, actor="小林(店员)")
        h1 = svc.assign_shelf(h1["id"], 2, actor="小林(店员)")
        h2 = svc.create_batch(STORE_ID, 5, 12, actor="阿店长",
                              batch_no="B20260930H02")
        svc.check_temp(h2["id"], 63.0, actor="小林(店员)")
        h2 = svc.assign_shelf(h2["id"], 3, actor="小林(店员)")
        h3 = svc.create_batch(STORE_ID, 7, 18, actor="阿店长",
                              batch_no="B20260930H03")
        svc.check_temp(h3["id"], 61.0, actor="小林(店员)")
        h3 = svc.assign_shelf(h3["id"], 4, actor="小林(店员)")
        h4 = svc.create_batch(STORE_ID, 2, 10, actor="阿店长",
                              batch_no="B20260930H04")
        svc.check_temp(h4["id"], 71.0, actor="小林(店员)")
        h4 = svc.assign_shelf(h4["id"], 1, actor="小林(店员)")
        svc.sell(h1["id"], 6)
        svc.sell(h2["id"], 5)
        svc.sell(h3["id"], 10)
        svc.sell(h4["id"], 10)  # 提前售罄留痕
        svc.close_store(STORE_ID, actor="阿店长",
                        batch_ids=[h1["id"], h2["id"], h3["id"]])
        # 账实差异：h1 账上 8 份，清点只有 7 份
        svc.confirm_loss(h1["id"], 7, actor="阿店长", reason="close")
        svc.confirm_loss(h3["id"], 8, actor="阿店长", reason="close")
        # h2 留待当前闭店清点页处理（剩 7）

    # h2 是第二个待清点项；再让 b_exp（到期）也在清点队列，凑成触屏清点场景
    _write_scenario(scenario)
    return scenario


def _shelf_for(svc, product_id):
    with svc._conn() as conn:
        cat = conn.execute("SELECT category FROM products WHERE id=?",
                           (product_id,)).fetchone()["category"]
        row = conn.execute(
            "SELECT s.id FROM shelves s JOIN cabinets c ON c.id=s.cabinet_id"
            " WHERE c.store_id=? AND s.category=? ORDER BY s.position",
            (STORE_ID, cat)).fetchone()
        return row["id"]


def _write_scenario(scenario):
    config.ensure_dirs()
    with open(os.path.join(config.DATA_DIR, "scenario.json"), "w",
              encoding="utf-8") as f:
        json.dump(scenario, f, ensure_ascii=False, indent=2)


def load_scenario():
    path = os.path.join(config.DATA_DIR, "scenario.json")
    if not os.path.exists(path):
        return {"faulty": [], "hot": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def seed_all(db_path=None, reset=True):
    if reset:
        reset_db(db_path)
    seed_master(db_path)
    return seed_demo(db_path)


if __name__ == "__main__":  # pragma: no cover
    seed_all()
    print("样例数据已生成")
