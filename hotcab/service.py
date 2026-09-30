"""核心领域服务：批次生命周期、乐观锁、效期、温度、折扣、报损、审计、日报。"""
import json
import os
import uuid
from datetime import datetime, timedelta

from . import config
from .database import connect, write_lock
from .errors import (AuthError, ConflictError, NotFoundError, StateError,
                     ValidationError)

STATUS_DRAFT = "draft"
STATUS_CHECKED = "checked"
STATUS_ON_SALE = "on_sale"
STATUS_PENDING = "pending_off"
STATUS_SOLD_OUT = "sold_out"
STATUS_LOSS = "loss"
SALEABLE = {STATUS_ON_SALE}

# 审计事件类型
AUDIT_TEMP_MISSING = "temp_missing"          # 温度读数缺失
AUDIT_WRONG_SHELF = "wrong_shelf"            # 上错层位
AUDIT_QTY_DIFF = "qty_discrepancy"           # 批次数量差异
AUDIT_EARLY_SOLDOUT = "early_soldout"        # 提前售罄
AUDIT_NEAR_OVERLAP = "near_overlap"          # 临期重叠
AUDIT_NEAR = "near_expiry"                   # 进入临期
AUDIT_WRONG_OFF = "wrong_off"                # 误下架（撤销）
AUDIT_CLOSE_REMAIN = "close_remain"          # 闭店剩余
AUDIT_EXPIRED = "expired"                    # 到期自动转待下架
AUDIT_TEMP_FAIL = "temp_fail"                # 温度持续不合格
AUDIT_DISCOUNT = "discount"                  # 降价留痕


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def discount_label(rate):
    tenths = rate / 10
    return f"{tenths:g}折"


class BatchService:
    def __init__(self, clock, db_path=None):
        self.clock = clock
        self.db_path = db_path or config.DB_PATH

    # ---------- 基础工具 ----------
    def _conn(self):
        return connect(self.db_path)

    def _audit(self, conn, event_type, batch_id=None, store_id=None,
                detail=None, actor=""):
        conn.execute(
            "INSERT INTO audit_events(batch_id, store_id, event_type, detail,"
            " actor, created_at) VALUES (?,?,?,?,?,?)",
            (batch_id, store_id, event_type, json.dumps(detail or {},
             ensure_ascii=False), actor, iso(self.clock.now())),
        )

    def _get(self, conn, batch_id):
        row = conn.execute("SELECT * FROM batches WHERE id=?",
                           (batch_id,)).fetchone()
        if not row:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        return row

    def _cas_update(self, conn, batch_id, expected_version, sets, params=()):
        """带乐观锁的更新：基于旧版本提交时影响行数为 0 → ConflictError。"""
        if expected_version is not None:
            sql = (f"UPDATE batches SET {sets}, version=version+1, "
                   "updated_at=? WHERE id=? AND version=?")
            cur = conn.execute(sql, params + (iso(self.clock.now()),
                                              batch_id, expected_version))
            if cur.rowcount == 0:
                raise ConflictError(expected_version=expected_version)
        else:
            sql = (f"UPDATE batches SET {sets}, version=version+1, "
                   "updated_at=? WHERE id=?")
            conn.execute(sql, params + (iso(self.clock.now()), batch_id))

    def _minutes_left(self, batch, now=None):
        if not batch["expiry_at"]:
            return None
        now = now or self.clock.now()
        return int((parse_iso(batch["expiry_at"]) - now).total_seconds() // 60)

    def _expired(self, batch, now=None):
        """效期边界：当前时刻到达 expiry_at 即到期。"""
        if not batch["expiry_at"]:
            return False
        return (now or self.clock.now()) >= parse_iso(batch["expiry_at"])

    def to_dto(self, row, product=None):
        now = self.clock.now()
        d = dict(row)
        ml = self._minutes_left(row, now)
        d["minutes_left"] = ml
        d["near_expiry"] = (row["status"] == STATUS_ON_SALE and ml is not None
                            and 0 <= ml <= config.NEAR_EXPIRY_MINUTES)
        d["expired"] = self._expired(row, now)
        if product:
            keys = product.keys()

            def pick(a, b):
                return product[a] if a in keys else (
                    product[b] if b in keys else None)

            d["product_name"] = pick("product_name", "name")
            d["product_sku"] = pick("product_sku", "sku")
            d["category"] = product["category"] if "category" in keys else None
            d["unit_price"] = product["unit_price"] if "unit_price" in keys else None
            d["temp_min"] = product["temp_min"] if "temp_min" in keys else None
            d["temp_max"] = product["temp_max"] if "temp_max" in keys else None
            if d["unit_price"] is not None:
                cur_rate = d["discount_rate"] or 100
                d["current_price"] = round(d["unit_price"] * cur_rate / 100, 2)
        return d

    # ---------- 出炉批次 ----------
    def create_batch(self, store_id, product_id, qty, actor="", batch_no=None):
        if not isinstance(qty, int) or qty <= 0:
            raise ValidationError("上柜数量必须为正整数")
        now = self.clock.now()
        with write_lock(), self._conn() as conn:
            if not conn.execute("SELECT 1 FROM stores WHERE id=?",
                                (store_id,)).fetchone():
                raise NotFoundError("门店不存在")
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (product_id,)).fetchone()
            if not prod:
                raise NotFoundError("商品不存在")
            if not batch_no:
                n = conn.execute(
                    "SELECT COUNT(*) c FROM batches WHERE produced_at LIKE ?",
                    (now.strftime("%Y-%m-%d") + "%",)).fetchone()["c"]
                batch_no = f"B{now.strftime('%Y%m%d')}{n + 1:03d}"
            cur = conn.execute(
                "INSERT INTO batches(batch_no, store_id, product_id,"
                " produced_at, initial_qty, remaining, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?, 'draft', ?,?)",
                (batch_no, store_id, product_id, iso(now), qty, qty,
                 iso(now), iso(now)))
            conn.commit()
            return self.get_batch(cur.lastrowid)

    def check_temp(self, batch_id, temp, attachment=None, actor="",
                   source="check", note=""):
        """记录出炉抽检温度。temp=None 表示读数缺失，留痕但阻止上柜。

        attachment: (filename, bytes) 或已保存的路径字符串。
        """
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] not in (STATUS_DRAFT,):
                raise StateError("仅待测温批次可记录抽检温度")
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (b["product_id"],)).fetchone()
            now_s = iso(self.clock.now())
            stored = None
            if attachment:
                stored = self._save_attachment(batch_id, attachment)

            if temp is None:
                conn.execute(
                    "INSERT INTO temp_readings(batch_id, temp_c, source, ok,"
                    " note, created_at) VALUES (?,?,?,?,?,?)",
                    (batch_id, None, source, 0, note or "读数缺失", now_s))
                conn.execute(
                    "UPDATE batches SET check_temp=NULL, check_ok=0,"
                    " check_attachment=COALESCE(?, check_attachment),"
                    " updated_at=? WHERE id=?",
                    (stored, now_s, batch_id))
                self._audit(conn, AUDIT_TEMP_MISSING, batch_id,
                            b["store_id"], {"stage": "出炉抽检", "note": note},
                            actor)
                conn.commit()
                return self.get_batch(batch_id)

            ok = prod["temp_min"] <= float(temp) <= prod["temp_max"]
            conn.execute(
                "INSERT INTO temp_readings(batch_id, temp_c, source, ok, note,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (batch_id, float(temp), source, 1 if ok else 0, note, now_s))
            if ok:
                conn.execute(
                    "UPDATE batches SET check_temp=?, check_ok=1,"
                    " checked_at=?, check_attachment=COALESCE(?,"
                    " check_attachment), status='checked', updated_at=?"
                    " WHERE id=?",
                    (float(temp), now_s, stored, now_s, batch_id))
            else:
                conn.execute(
                    "UPDATE batches SET check_temp=?, check_ok=0,"
                    " check_attachment=COALESCE(?, check_attachment),"
                    " updated_at=? WHERE id=?",
                    (float(temp), stored, now_s, batch_id))
            conn.commit()
            return self.get_batch(batch_id)

    def _save_attachment(self, batch_id, attachment):
        config.ensure_dirs()
        if isinstance(attachment, str):
            return attachment
        filename, data = attachment
        safe = os.path.basename(filename).replace(os.sep, "_")
        name = f"batch{batch_id}_{uuid.uuid4().hex[:8]}_{safe}"
        path = os.path.join(config.ATTACH_DIR, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    # ---------- 分层位 / 开始计时 ----------
    def assign_shelf(self, batch_id, shelf_id, actor="", expected_version=None):
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] != STATUS_CHECKED or not b["check_ok"]:
                raise StateError("批次需先完成合格的抽检温度才能上柜")
            shelf = conn.execute("SELECT * FROM shelves WHERE id=?",
                                 (shelf_id,)).fetchone()
            if not shelf:
                raise NotFoundError("层位不存在")
            cabinet = conn.execute("SELECT * FROM cabinets WHERE id=?",
                                   (shelf["cabinet_id"],)).fetchone()
            if cabinet["store_id"] != b["store_id"]:
                self._audit(conn, AUDIT_WRONG_SHELF, batch_id, b["store_id"],
                            {"reason": "跨门店层位", "shelf_id": shelf_id},
                            actor)
                raise ValidationError("该层位不属于本门店，上柜被拒绝")
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (b["product_id"],)).fetchone()
            if shelf["category"] != prod["category"]:
                self._audit(conn, AUDIT_WRONG_SHELF, batch_id, b["store_id"],
                            {"reason": "品类不匹配",
                             "shelf": f"{cabinet['code']}-{shelf['label']}",
                             "shelf_category": shelf["category"],
                             "product_category": prod["category"],
                             "product": prod["name"]}, actor)
                conn.commit()
                raise ValidationError(
                    f"上错层位：{prod['name']}（{prod['category']}）不能放在"
                    f"{shelf['label']}（{shelf['category']}）")
            now = self.clock.now()
            expiry = now + timedelta(minutes=prod["life_minutes"])
            self._cas_update(
                conn, batch_id, expected_version,
                "shelf_id=?, assigned_at=?, expiry_at=?, status='on_sale',"
                " pending_reason=NULL",
                (shelf_id, iso(now), iso(expiry)))
            conn.commit()
            return self.get_batch(batch_id)

    # ---------- 销售扣减 ----------
    def sell(self, batch_id, qty, expected_version=None, actor="",
             source="pos", receipt_no=None):
        if not isinstance(qty, int) or qty <= 0:
            raise ValidationError("销售数量必须为正整数")
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] == STATUS_LOSS:
                raise StateError("该批次已报损，不能恢复销售")
            if b["status"] not in SALEABLE:
                raise StateError(f"批次状态为 {b['status']}，不可销售")
            ml = self._minutes_left(b)
            if self._expired(b):
                raise StateError("批次已到期，请先下架")
            if qty > b["remaining"]:
                raise StateError(
                    f"扣减数量 {qty} 超过剩余份数 {b['remaining']}")
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (b["product_id"],)).fetchone()
            rate = b["discount_rate"] or 100
            price = round(prod["unit_price"] * rate / 100, 2)
            now = self.clock.now()
            new_remaining = b["remaining"] - qty
            receipt_no = receipt_no or f"R{now.strftime('%H%M%S')}-" \
                                       f"{uuid.uuid4().hex[:6]}"
            conn.execute(
                "INSERT INTO sales(batch_id, qty, unit_price, amount,"
                " receipt_no, source, created_at) VALUES (?,?,?,?,?,?,?)",
                (batch_id, qty, price, round(price * qty, 2), receipt_no,
                 source, iso(now)))
            if new_remaining == 0:
                self._cas_update(
                    conn, batch_id, expected_version,
                    "remaining=0, sold=sold+?, status='sold_out'", (qty,))
                self._audit(conn, AUDIT_EARLY_SOLDOUT, batch_id,
                            b["store_id"],
                            {"remaining_before": b["remaining"],
                             "qty": qty, "minutes_to_expiry": ml,
                             "receipt": receipt_no}, actor)
            else:
                self._cas_update(
                    conn, batch_id, expected_version,
                    "remaining=remaining-?, sold=sold+?", (qty, qty))
            conn.commit()
            return self.get_batch(batch_id)

    # ---------- 降价 / 折扣权限 ----------
    def preset_options(self, batch_id):
        """返回店员当前可执行的预设折扣。"""
        with self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] != STATUS_ON_SALE:
                return []
            ml = self._minutes_left(b)
            if ml is None or ml < 0:
                return []
            return [{"rate": rate, "label": label}
                    for from_min, rate, label in config.PRESET_DISCOUNTS
                    if ml <= from_min]

    def apply_discount(self, batch_id, rate, role, actor="",
                       expected_version=None, label=None):
        if not isinstance(rate, int) or not (1 <= rate < 100):
            raise ValidationError("折扣率须为 1-99 的整数")
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] != STATUS_ON_SALE:
                raise StateError("仅销售中批次可降价")
            ml = self._minutes_left(b)
            preset_rates = {p[1] for p in config.PRESET_DISCOUNTS
                            if ml is not None and ml <= p[0]}
            is_preset = rate in preset_rates
            if role != config.ROLE_MANAGER:
                if not is_preset:
                    if ml is not None and any(rate == p[1]
                                              for p in config.PRESET_DISCOUNTS):
                        raise AuthError("未到该预设折扣的临期时点，店员无权提前执行")
                    raise AuthError("店员只能执行预设折扣，例外价格请店长处理")
            label = label or discount_label(rate)
            kind = "preset" if is_preset else "exception"
            now_s = iso(self.clock.now())
            self._cas_update(
                conn, batch_id, expected_version,
                "discount_rate=?, discount_label=?, discount_role=?,"
                " discount_by=?, discounted_at=?",
                (rate, label, role, actor, now_s))
            self._audit(conn, AUDIT_DISCOUNT, batch_id, b["store_id"],
                        {"rate": rate, "label": label, "kind": kind,
                         "role": role, "minutes_left": ml}, actor)
            conn.commit()
            return self.get_batch(batch_id)

    # ---------- 温度适配器喂数 ----------
    def ingest_temp(self, batch_id, temp, source="adapter", note=""):
        """模拟温度适配器回传；temp=None 为读数缺失，连续不合格转待下架。"""
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] != STATUS_ON_SALE:
                return self.get_batch(batch_id)
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (b["product_id"],)).fetchone()
            now = self.clock.now()
            now_s = iso(now)
            if temp is None:
                conn.execute(
                    "INSERT INTO temp_readings(batch_id, temp_c, source, ok,"
                    " note, created_at) VALUES (?,?,?,?,?,?)",
                    (batch_id, None, source, 0, note or "探头读数缺失", now_s))
                self._audit(conn, AUDIT_TEMP_MISSING, batch_id,
                            b["store_id"], {"stage": "保温中", "note": note},
                            "adapter")
                conn.commit()
                return self.get_batch(batch_id)
            ok = prod["temp_min"] <= float(temp) <= prod["temp_max"]
            conn.execute(
                "INSERT INTO temp_readings(batch_id, temp_c, source, ok, note,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (batch_id, float(temp), source, 1 if ok else 0, note, now_s))
            if ok:
                conn.execute("UPDATE batches SET bad_streak=0, updated_at=?"
                             " WHERE id=?", (now_s, batch_id))
            else:
                new_streak = b["bad_streak"] + 1
                if new_streak >= config.TEMP_BAD_LIMIT:
                    self._cas_update(
                        conn, batch_id, None,
                        "bad_streak=?, status='pending_off', pending_reason="
                        "'temp'", (new_streak,))
                    self._audit(conn, AUDIT_TEMP_FAIL, batch_id,
                                b["store_id"], {"streak": new_streak,
                                                "last_temp": float(temp),
                                                "range": [prod["temp_min"],
                                                          prod["temp_max"]]},
                                "adapter")
                else:
                    conn.execute(
                        "UPDATE batches SET bad_streak=?, updated_at=?"
                        " WHERE id=?", (new_streak, now_s, batch_id))
            conn.commit()
            return self.get_batch(batch_id)

    # ---------- 临期 / 到期进程内任务 ----------
    def tick_expiry(self):
        """扫描销售中批次：临期标记（含重叠留痕）、到期自动转待下架。"""
        moved, near = [], []
        with write_lock(), self._conn() as conn:
            now = self.clock.now()
            rows = conn.execute(
                "SELECT * FROM batches WHERE status='on_sale'").fetchall()
            for b in rows:
                ml = self._minutes_left(b, now)
                if ml is None:
                    continue
                if self._expired(b, now):
                    conn.execute(
                        "UPDATE batches SET status='pending_off',"
                        " pending_reason='expired', version=version+1,"
                        " updated_at=? WHERE id=?", (iso(now), b["id"]))
                    self._audit(conn, AUDIT_EXPIRED, b["id"], b["store_id"],
                                {"expiry_at": b["expiry_at"]}, "system")
                    moved.append(b["id"])
                elif ml <= config.NEAR_EXPIRY_MINUTES and not b["near_logged"]:
                    overlap = conn.execute(
                        "SELECT id, batch_no FROM batches WHERE product_id=?"
                        " AND status='on_sale' AND id<>? AND remaining>0"
                        " AND near_logged=1 AND expiry_at<=?",
                        (b["product_id"], b["id"],
                         iso(now + timedelta(minutes=config.NEAR_EXPIRY_MINUTES)))
                    ).fetchall()
                    conn.execute(
                        "UPDATE batches SET near_logged=1, updated_at=?"
                        " WHERE id=?", (iso(now), b["id"]))
                    self._audit(conn, AUDIT_NEAR, b["id"], b["store_id"],
                                {"minutes_left": ml}, "system")
                    if overlap:
                        self._audit(
                            conn, AUDIT_NEAR_OVERLAP, b["id"], b["store_id"],
                            {"minutes_left": ml,
                             "overlap_with": [r["batch_no"] for r in overlap]},
                            "system")
                    near.append(b["id"])
            conn.commit()
        return {"moved_to_pending": moved, "near_marked": near}

    # ---------- 人工下架 / 误下架撤销 ----------
    def mark_pending_manual(self, batch_id, actor="", expected_version=None):
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] != STATUS_ON_SALE:
                raise StateError("仅销售中批次可人工下架")
            self._cas_update(conn, batch_id, expected_version,
                             "status='pending_off', pending_reason='manual'")
            conn.commit()
            return self.get_batch(batch_id)

    def restore_wrong_off(self, batch_id, actor="", expected_version=None):
        """误下架撤销：待下架且尚未到期的人工下架批次恢复销售，留痕。"""
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] != STATUS_PENDING:
                raise StateError("仅待下架批次可撤销")
            if b["pending_reason"] != "manual":
                raise StateError("到期/温度/闭店批次不能撤销，必须清点报损")
            if self._expired(b):
                raise StateError("批次已到期，不能恢复，请完成报损")
            self._cas_update(conn, batch_id, expected_version,
                             "status='on_sale', pending_reason=NULL")
            self._audit(conn, AUDIT_WRONG_OFF, batch_id, b["store_id"],
                        {"action": "恢复销售"}, actor)
            conn.commit()
            return self.get_batch(batch_id)

    # ---------- 清点报损（不可逆） ----------
    def confirm_loss(self, batch_id, counted_qty, actor="",
                     expected_version=None, reason=None):
        if not isinstance(counted_qty, int) or counted_qty < 0:
            raise ValidationError("清点数量须为非负整数")
        with write_lock(), self._conn() as conn:
            b = self._get(conn, batch_id)
            if b["status"] == STATUS_LOSS:
                raise StateError("该批次已报损，报损不可逆、不可恢复销售")
            if b["status"] != STATUS_PENDING:
                raise StateError("仅待下架批次可清点报损")
            if counted_qty > b["remaining"]:
                raise ValidationError(
                    f"清点数量 {counted_qty} 超过账上剩余 {b['remaining']}")
            reason = reason or b["pending_reason"] or "manual"
            diff = b["remaining"] - counted_qty
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (b["product_id"],)).fetchone()
            now_s = iso(self.clock.now())
            self._cas_update(
                conn, batch_id, expected_version,
                "status='loss', counted_qty=?, loss_qty=?, remaining=?,"
                " confirmed_by=?, loss_reason=?, loss_at=?",
                (counted_qty, counted_qty, diff, actor, reason, now_s))
            conn.execute(
                "INSERT INTO loss_orders(batch_id, remaining_before,"
                " counted_qty, qty, reason, confirmed_by, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (batch_id, b["remaining"], counted_qty, counted_qty, reason,
                 actor, now_s))
            if diff != 0:
                self._audit(conn, AUDIT_QTY_DIFF, batch_id, b["store_id"],
                            {"expected_remaining": b["remaining"],
                             "counted": counted_qty, "difference": diff,
                             "note": "账实数量差异，请核查销售扣减"}, actor)
            conn.commit()
            return self.get_batch(batch_id), {
                "remaining_before": b["remaining"],
                "counted_qty": counted_qty,
                "difference": diff,
                "loss_cost": round(counted_qty * prod["unit_cost"], 2)}

    # ---------- 闭店 ----------
    def close_store(self, store_id, actor="manager", batch_ids=None):
        """闭店：销售中批次全部转待下架（闭店剩余留痕），等待清点。

        batch_ids 给定时只闭指定批次（用于按营业班次闭店）。
        """
        ids, qty_total = [], 0
        with write_lock(), self._conn() as conn:
            now_s = iso(self.clock.now())
            if batch_ids:
                marks = ",".join("?" * len(batch_ids))
                rows = conn.execute(
                    f"SELECT * FROM batches WHERE store_id=? AND status="
                    f"'on_sale' AND id IN ({marks})",
                    [store_id, *batch_ids]).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM batches WHERE store_id=? AND status='on_sale'",
                    (store_id,)).fetchall()
            for b in rows:
                conn.execute(
                    "UPDATE batches SET status='pending_off',"
                    " pending_reason='close', version=version+1, updated_at=?"
                    " WHERE id=?", (now_s, b["id"]))
                self._audit(conn, AUDIT_CLOSE_REMAIN, b["id"], store_id,
                            {"remaining": b["remaining"]}, actor)
                ids.append(b["id"])
                qty_total += b["remaining"]
            conn.execute(
                "INSERT INTO store_closes(store_id, closed_at, batch_count,"
                " qty) VALUES (?,?,?,?)",
                (store_id, now_s, len(ids), qty_total))
            conn.commit()
        return {"batch_ids": ids, "qty": qty_total}

    # ---------- 查询 ----------
    def get_batch(self, batch_id):
        with self._conn() as conn:
            row = self._get(conn, batch_id)
            prod = conn.execute("SELECT * FROM products WHERE id=?",
                                (row["product_id"],)).fetchone()
            shelf = None
            if row["shelf_id"]:
                shelf = conn.execute("SELECT * FROM shelves WHERE id=?",
                                     (row["shelf_id"],)).fetchone()
            dto = self.to_dto(row, prod)
            if shelf:
                cab = conn.execute("SELECT * FROM cabinets WHERE id=?",
                                   (shelf["cabinet_id"],)).fetchone()
                dto["shelf_label"] = shelf["label"]
                dto["shelf_position"] = shelf["position"]
                dto["cabinet_code"] = cab["code"]
            return dto

    def list_batches(self, store_id=None, status=None, date=None):
        sql = ("SELECT b.*, p.name product_name, p.sku product_sku,"
               " p.category category, p.unit_price unit_price,"
               " p.temp_min temp_min, p.temp_max temp_max,"
               " p.unit_cost unit_cost, s.label shelf_label, c.code cabinet_code"
               " FROM batches b JOIN products p ON p.id=b.product_id"
               " LEFT JOIN shelves s ON s.id=b.shelf_id"
               " LEFT JOIN cabinets c ON c.id=s.cabinet_id WHERE 1=1")
        args = []
        if store_id:
            sql += " AND b.store_id=?"
            args.append(store_id)
        if status:
            sql += " AND b.status=?"
            args.append(status)
        if date:
            sql += " AND b.produced_at LIKE ?"
            args.append(date + "%")
        sql += " ORDER BY b.produced_at DESC, b.id DESC"
        with self._conn() as conn:
            return [self.to_dto(r, r) for r in conn.execute(sql, args)]

    def near_queue(self, store_id):
        """效期队列：销售中且有剩余，按到期紧迫度升序（不做任何闪烁动效）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT b.*, p.name product_name, p.sku product_sku,"
                " p.category category, p.unit_price unit_price,"
                " p.temp_min temp_min, p.temp_max temp_max, s.label"
                " shelf_label, c.code cabinet_code"
                " FROM batches b JOIN products p ON p.id=b.product_id"
                " LEFT JOIN shelves s ON s.id=b.shelf_id"
                " LEFT JOIN cabinets c ON c.id=s.cabinet_id"
                " WHERE b.store_id=? AND b.status='on_sale' AND b.remaining>0"
                " ORDER BY b.expiry_at ASC", (store_id,)).fetchall()
            return [self.to_dto(r, r) for r in rows]

    def pending_list(self, store_id):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT b.*, p.name product_name, p.sku product_sku,"
                " p.category category, p.unit_price unit_price,"
                " p.temp_min temp_min, p.temp_max temp_max, s.label"
                " shelf_label, c.code cabinet_code"
                " FROM batches b JOIN products p ON p.id=b.product_id"
                " LEFT JOIN shelves s ON s.id=b.shelf_id"
                " LEFT JOIN cabinets c ON c.id=s.cabinet_id"
                " WHERE b.store_id=? AND b.status='pending_off'"
                " ORDER BY b.updated_at ASC", (store_id,)).fetchall()
            return [self.to_dto(r, r) for r in rows]

    def cabinet_view(self, store_id):
        """热柜陈列图数据：层板（横向陈列带）→ 其上批次。"""
        with self._conn() as conn:
            cabs = conn.execute(
                "SELECT * FROM cabinets WHERE store_id=? ORDER BY id",
                (store_id,)).fetchall()
            result = []
            for cab in cabs:
                shelves = conn.execute(
                    "SELECT * FROM shelves WHERE cabinet_id=?"
                    " ORDER BY position", (cab["id"],)).fetchall()
                shelf_dtos = []
                for sh in shelves:
                    batches = conn.execute(
                        "SELECT b.*, p.name product_name, p.sku product_sku,"
                        " p.category category, p.unit_price unit_price,"
                        " p.temp_min temp_min, p.temp_max temp_max"
                        " FROM batches b JOIN products p ON p.id=b.product_id"
                        " WHERE b.shelf_id=? AND b.status IN ('on_sale')"
                        " ORDER BY b.assigned_at", (sh["id"],)).fetchall()
                    shelf_dtos.append({
                        "id": sh["id"], "position": sh["position"],
                        "label": sh["label"], "category": sh["category"],
                        "batches": [self.to_dto(r, r) for r in batches]})
                result.append({"id": cab["id"], "code": cab["code"],
                               "name": cab["name"], "shelves": shelf_dtos})
            return result

    def temp_history(self, batch_id, limit=50):
        with self._conn() as conn:
            self._get(conn, batch_id)
            rows = conn.execute(
                "SELECT * FROM temp_readings WHERE batch_id=?"
                " ORDER BY id DESC LIMIT ?", (batch_id, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def sales_list(self, store_id, date=None, limit=100):
        date = date or self.clock.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.*, b.batch_no, p.name product_name, p.sku sku"
                " FROM sales s JOIN batches b ON b.id=s.batch_id"
                " JOIN products p ON p.id=b.product_id"
                " WHERE b.store_id=? AND s.created_at LIKE ?"
                " ORDER BY s.id DESC LIMIT ?",
                (store_id, date + "%", limit)).fetchall()
            return [dict(r) for r in rows]

    def loss_list(self, store_id, date=None):
        date = date or self.clock.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT l.*, b.batch_no, p.name product_name, p.sku sku,"
                " p.unit_cost unit_cost"
                " FROM loss_orders l JOIN batches b ON b.id=l.batch_id"
                " JOIN products p ON p.id=b.product_id"
                " WHERE b.store_id=? AND l.created_at LIKE ?"
                " ORDER BY l.id DESC", (store_id, date + "%")).fetchall()
            return [dict(r) for r in rows]

    def audit_list(self, store_id=None, batch_id=None, event_type=None,
                   limit=200):
        sql = ("SELECT a.*, b.batch_no FROM audit_events a"
               " LEFT JOIN batches b ON b.id=a.batch_id WHERE 1=1")
        args = []
        if store_id:
            sql += " AND (a.store_id=? OR (a.store_id IS NULL AND b.store_id=?))"
            args += [store_id, store_id]
        if batch_id:
            sql += " AND a.batch_id=?"
            args.append(batch_id)
        if event_type:
            sql += " AND a.event_type=?"
            args.append(event_type)
        sql += " ORDER BY a.id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            out = []
            for r in conn.execute(sql, args):
                d = dict(r)
                d["detail"] = json.loads(d["detail"] or "{}")
                out.append(d)
            return out

    # ---------- 日报 ----------
    def daily_report(self, store_id, date=None):
        date = date or self.clock.today().isoformat()
        with self._conn() as conn:
            store = conn.execute("SELECT * FROM stores WHERE id=?",
                                 (store_id,)).fetchone()
            if not store:
                raise NotFoundError("门店不存在")
            batches = conn.execute(
                "SELECT b.*, p.name product_name, p.unit_price unit_price,"
                " p.unit_cost unit_cost FROM batches b JOIN products p"
                " ON p.id=b.product_id WHERE b.store_id=? AND"
                " b.produced_at LIKE ?", (store_id, date + "%")).fetchall()
            sales = conn.execute(
                "SELECT s.* FROM sales s JOIN batches b ON b.id=s.batch_id"
                " WHERE b.store_id=? AND s.created_at LIKE ?",
                (store_id, date + "%")).fetchall()
            losses = conn.execute(
                "SELECT l.*, p.unit_cost unit_cost, p.name product_name"
                " FROM loss_orders l JOIN batches b ON b.id=l.batch_id"
                " JOIN products p ON p.id=b.product_id WHERE b.store_id=?"
                " AND l.created_at LIKE ?", (store_id, date + "%")).fetchall()
            events = conn.execute(
                "SELECT * FROM audit_events WHERE (store_id=? OR store_id IS NULL)"
                " AND created_at LIKE ? ORDER BY id",
                (store_id, date + "%")).fetchall()

            produced_qty = sum(b["initial_qty"] for b in batches)
            sold_qty = sum(s["qty"] for s in sales)
            revenue = round(sum(s["amount"] for s in sales), 2)
            loss_qty = sum(l["qty"] for l in losses)
            loss_cost = round(sum(l["qty"] * l["unit_cost"] for l in losses), 2)
            discount_given = round(
                sum((_sale_orig(conn, s) - s["unit_price"]) * s["qty"]
                    for s in sales), 2)
            by_status = {}
            for b in batches:
                by_status[b["status"]] = by_status.get(b["status"], 0) + 1
            type_counts = {}
            for e in events:
                type_counts[e["event_type"]] = \
                    type_counts.get(e["event_type"], 0) + 1
            return {
                "store": dict(store), "date": date,
                "batch_count": len(batches),
                "produced_qty": produced_qty,
                "sold_qty": sold_qty,
                "revenue": revenue,
                "loss_qty": loss_qty,
                "loss_cost": loss_cost,
                "discount_given": discount_given,
                "waste_rate": round(loss_qty / produced_qty, 3)
                if produced_qty else 0,
                "by_status": by_status,
                "event_counts": type_counts,
                "batches": [self.to_dto(b, b) for b in batches],
                "losses": [dict(l) for l in losses],
            }


def _sale_orig(conn, sale):
    row = conn.execute(
        "SELECT p.unit_price FROM products p JOIN batches b ON b.product_id=p.id"
        " WHERE b.id=?", (sale["batch_id"],)).fetchone()
    return row["unit_price"]
