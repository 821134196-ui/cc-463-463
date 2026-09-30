"""SQLite schema 与连接工厂。每次服务调用开一个连接，写操作由服务层锁串行化。"""
import os
import sqlite3
import threading

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cabinets (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    shelf_count INTEGER NOT NULL DEFAULT 4,
    UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS shelves (
    id INTEGER PRIMARY KEY,
    cabinet_id INTEGER NOT NULL REFERENCES cabinets(id),
    position INTEGER NOT NULL,          -- 从上往下的层板序号
    label TEXT NOT NULL,
    category TEXT NOT NULL,             -- 层位允许品类：热餐/炸物/面点/小食
    UNIQUE(cabinet_id, position)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    life_minutes INTEGER NOT NULL,      -- 标准保温效期（分钟）
    temp_min REAL NOT NULL,             -- 合格保温温区
    temp_max REAL NOT NULL,
    unit_price REAL NOT NULL,           -- 原价
    unit_cost REAL NOT NULL             -- 成本（报损金额口径）
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('clerk','manager'))
);

-- 制作批次（status: draft 已出炉待测温 / checked 测温合格待上柜 /
-- on_sale 上柜销售中 / pending_off 待下架清点 / sold_out 提前售罄 / loss 已报损）
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    batch_no TEXT NOT NULL UNIQUE,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL REFERENCES products(id),

    produced_at TEXT NOT NULL,
    check_temp REAL,                    -- 出炉抽检温度，NULL=读数缺失
    check_ok INTEGER,
    check_attachment TEXT,
    checked_at TEXT,

    shelf_id INTEGER REFERENCES shelves(id),
    assigned_at TEXT,

    initial_qty INTEGER NOT NULL CHECK(initial_qty > 0),
    remaining INTEGER NOT NULL,
    sold INTEGER NOT NULL DEFAULT 0,
    loss_qty INTEGER NOT NULL DEFAULT 0,

    expiry_at TEXT,                     -- 上柜时 = 现在 + 标准效期
    status TEXT NOT NULL DEFAULT 'draft',
    pending_reason TEXT,                -- expired/temp/manual/close/wrong_shelf
    bad_streak INTEGER NOT NULL DEFAULT 0,
    near_logged INTEGER NOT NULL DEFAULT 0,

    discount_rate INTEGER,              -- 实际售价 = 原价 * rate/100
    discount_label TEXT,
    discount_role TEXT,
    discount_by TEXT,
    discounted_at TEXT,

    counted_qty INTEGER,
    confirmed_by TEXT,
    loss_reason TEXT,
    loss_at TEXT,

    version INTEGER NOT NULL DEFAULT 1, -- 乐观锁版本
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_batches_store ON batches(store_id, produced_at);

CREATE TABLE IF NOT EXISTS temp_readings (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    temp_c REAL,                        -- NULL 表示适配器读数缺失
    source TEXT NOT NULL,               -- check 出炉抽检 / adapter 模拟探头 / manual 人工
    ok INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_temp_batch ON temp_readings(batch_id, created_at);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    qty INTEGER NOT NULL CHECK(qty > 0),
    unit_price REAL NOT NULL,
    amount REAL NOT NULL,
    receipt_no TEXT NOT NULL,
    source TEXT NOT NULL,               -- pos 收银 / adapter 模拟销售回执
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_batch ON sales(batch_id, created_at);

CREATE TABLE IF NOT EXISTS loss_orders (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    remaining_before INTEGER NOT NULL,
    counted_qty INTEGER NOT NULL,
    qty INTEGER NOT NULL,               -- 实损 = 清点数量
    reason TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER,
    store_id INTEGER,
    event_type TEXT NOT NULL,          -- 见 service.AUDIT_*
    detail TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_store ON audit_events(store_id, created_at);

CREATE TABLE IF NOT EXISTS store_closes (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    closed_at TEXT NOT NULL,
    batch_count INTEGER NOT NULL,
    qty INTEGER NOT NULL
);
"""

_write_lock = threading.RLock()


def connect(db_path=None):
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path=None):
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def write_lock():
    """服务层所有写事务持有的进程内锁。"""
    return _write_lock
