import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("HOTCAB_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("HOTCAB_DB", os.path.join(DATA_DIR, "hotcab.db"))
ATTACH_DIR = os.environ.get("HOTCAB_ATTACH", os.path.join(DATA_DIR, "attachments"))

# 临期阈值（分钟）：剩余 <= 该值进入效期队列临期区
NEAR_EXPIRY_MINUTES = 20
# 连续多少次温度抽检不合格即转待下架
TEMP_BAD_LIMIT = 3

# 预设折扣阶梯：剩余分钟 <= from_min 时可由店员执行 (rate, label)
PRESET_DISCOUNTS = (
    (20, 90, "临期9折"),
    (10, 70, "临期7折"),
)

# 后台任务节拍（秒）
TICK_EXPIRY = 5
TICK_TEMP = 4
TICK_SALES = 6

ROLE_CLERK = "clerk"
ROLE_MANAGER = "manager"
ROLE_LABELS = {ROLE_CLERK: "店员", ROLE_MANAGER: "店长"}

# 象牙白 / 番茄红 / 炭灰
COLOR_IVORY = "#FAF6EC"
COLOR_TOMATO = "#D23B3B"
COLOR_CHARCOAL = "#2B2D2F"


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)
