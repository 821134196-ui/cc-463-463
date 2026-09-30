"""SQLite 连接与表结构。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id               TEXT PRIMARY KEY,
    store_id         TEXT NOT NULL REFERENCES stores(id),
    code             TEXT NOT NULL,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,           -- 便当 / 饭团 / 炸物 / 包点 / 小吃
    base_price       REAL NOT NULL,
    temp_min         REAL NOT NULL,           -- 保温下限 ℃
    temp_max         REAL NOT NULL,           -- 保温上限 ℃
    life_minutes     INTEGER NOT NULL,        -- 上柜后效期(分钟)
    preset_discounts TEXT NOT NULL DEFAULT '[]' -- 店员可用预设折扣,如 [0.8,0.7]
);

CREATE TABLE IF NOT EXISTS shelves (
    id           TEXT PRIMARY KEY,
    store_id     TEXT NOT NULL REFERENCES stores(id),
    cabinet_name TEXT NOT NULL,
    layer_index  INTEGER NOT NULL,            -- 从下到上的实际层板序号
    label        TEXT NOT NULL,
    zone         TEXT NOT NULL,               -- 该层位默认品类区
    capacity     INTEGER NOT NULL             -- 最大陈列份数
);

CREATE TABLE IF NOT EXISTS batches (
    id            TEXT PRIMARY KEY,
    batch_no      TEXT NOT NULL UNIQUE,
    store_id      TEXT NOT NULL REFERENCES stores(id),
    product_id    TEXT NOT NULL REFERENCES products(id),
    produced_at   REAL NOT NULL,              -- 出炉时间
    probed_at     REAL,                       -- 抽检温度记录时间
    probe_temp    REAL,
    shelf_id      TEXT REFERENCES shelves(id),-- 上柜层位(上柜前为空)
    out_at        REAL,                       -- 上柜开始计时时间
    expires_at    REAL,                       -- 到期时间
    qty_initial   INTEGER NOT NULL,
    qty_remaining INTEGER NOT NULL,
    status        TEXT NOT NULL,              -- made/placed/pending/discarded/soldout
    waste_reason  TEXT,                       -- expired/temp/close_remaining
    discount      REAL,                        -- 当前成交价折扣
    temp_state    TEXT NOT NULL DEFAULT 'unknown', -- ok/bad/missing/unknown
    bad_streak    INTEGER NOT NULL DEFAULT 0,
    near_flagged  INTEGER NOT NULL DEFAULT 0,
    missing_flagged INTEGER NOT NULL DEFAULT 0,
    wasted_qty    INTEGER,
    wasted_at     REAL,
    version       INTEGER NOT NULL DEFAULT 0, -- 乐观锁版本
    created_by    TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_store ON batches(store_id);

CREATE TABLE IF NOT EXISTS temp_readings (
    id         TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batches(id),
    ts         REAL NOT NULL,
    temp       REAL NOT NULL,
    source     TEXT NOT NULL,                 -- probe(抽检)/sensor(模拟传感器)
    ok         INTEGER NOT NULL,
    attachment TEXT,                          -- 本地附件相对路径
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_temp_batch ON temp_readings(batch_id);

CREATE TABLE IF NOT EXISTS sales (
    id         TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batches(id),
    ts         REAL NOT NULL,
    qty        INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    receipt_no TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_batch ON sales(batch_id);

CREATE TABLE IF NOT EXISTS price_changes (
    id           TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL REFERENCES batches(id),
    ts           REAL NOT NULL,
    old_discount REAL,
    new_discount REAL,
    actor        TEXT NOT NULL,
    role         TEXT NOT NULL                -- clerk/manager
);

-- 全量留痕:温度缺失、上错层位、数量差异、提前售罄、临期重叠、误下架、闭店剩余等
CREATE TABLE IF NOT EXISTS audit_events (
    id         TEXT PRIMARY KEY,
    ts         REAL NOT NULL,
    store_id   TEXT NOT NULL,
    batch_id   TEXT REFERENCES batches(id),
    event_type TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}',
    actor      TEXT NOT NULL DEFAULT '系统'
);
CREATE INDEX IF NOT EXISTS idx_audit_store ON audit_events(store_id);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id);
"""


def open_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
