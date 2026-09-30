"""领域服务:批次、层位、温度、销售、折扣、效期流转与报损。

所有写操作集中在本模块;关键规则:
- 出炉后必须先记录抽检温度才能上柜;
- 数量扣减用 version 乐观锁,旧版本提交失败;
- 店员只能使用预设折扣,店长可处理例外价格;
- 到期/温度持续不合格 -> pending(待下架),清点后 discarded(已报损);
- 已报损不可恢复销售;pending 状态确认误下架可撤回上柜并留痕。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

NEAR_WINDOW = 25 * 60          # 到期前 25 分钟视为临期
TEMP_BAD_STREAK = 3            # 连续 3 次温度不合格 -> 待下架
MISSING_GRACE = 20 * 60        # 20 分钟无任何温度读数 -> 温度缺失


class DomainError(Exception):
    """业务规则错误。"""


class ConflictError(DomainError):
    """乐观锁冲突:版本过期。"""


def _uid() -> str:
    return uuid.uuid4().hex


class HotFoodService:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time,
                 attachment_dir: Optional[Path] = None):
        self.conn = conn
        self.clock = clock
        self.attachment_dir = attachment_dir
        # HTTP 线程与后台模拟线程共享同一连接,所有写操作以此锁串行化
        self.lock = threading.RLock()
        # 已留痕的临期重叠组合,避免同一批批次反复记事件
        self._overlap_logged: set[frozenset[str]] = set()

    # ------------------------------------------------------------------ #
    # 基础数据
    # ------------------------------------------------------------------ #
    def add_store(self, sid: str, name: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO stores(id,name) VALUES(?,?)", (sid, name))

    def add_product(self, pid: str, store_id: str, code: str, name: str, category: str,
                    base_price: float, temp_min: float, temp_max: float, life_minutes: int,
                    preset_discounts: list[float]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO products
               (id,store_id,code,name,category,base_price,temp_min,temp_max,life_minutes,preset_discounts)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (pid, store_id, code, name, category, base_price, temp_min, temp_max,
             life_minutes, json.dumps(preset_discounts)))

    def add_shelf(self, sid: str, store_id: str, cabinet_name: str, layer_index: int,
                  label: str, zone: str, capacity: int) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO shelves(id,store_id,cabinet_name,layer_index,label,zone,capacity)
               VALUES(?,?,?,?,?,?,?)""",
            (sid, store_id, cabinet_name, layer_index, label, zone, capacity))

    # ------------------------------------------------------------------ #
    # 审计留痕
    # ------------------------------------------------------------------ #
    def audit(self, store_id: str, event_type: str, detail: dict[str, Any],
              batch_id: Optional[str] = None, actor: str = "系统") -> None:
        self.conn.execute(
            """INSERT INTO audit_events(id,ts,store_id,batch_id,event_type,detail,actor)
               VALUES(?,?,?,?,?,?,?)""",
            (_uid(), self.clock(), store_id, batch_id, event_type,
             json.dumps(detail, ensure_ascii=False), actor))

    def events(self, store_id: str, limit: int = 200) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM audit_events WHERE store_id=? ORDER BY ts DESC,id DESC LIMIT ?",
            (store_id, limit)))

    # ------------------------------------------------------------------ #
    # 出炉 / 抽检 / 上柜
    # ------------------------------------------------------------------ #
    def make_batch(self, product_id: str, qty: int, actor: str,
                   produced_at: Optional[float] = None) -> str:
        p = self._product(product_id)
        ts = produced_at if produced_at is not None else self.clock()
        bid = _uid()
        seq = self.conn.execute(
            "SELECT COUNT(*) c FROM batches WHERE store_id=?", (p["store_id"],)).fetchone()["c"] + 1
        batch_no = f"B{time.strftime('%m%d', time.localtime(ts))}-{seq:03d}"
        self.conn.execute(
            """INSERT INTO batches(id,batch_no,store_id,product_id,produced_at,qty_initial,
                   qty_remaining,status,temp_state,created_by,updated_at)
               VALUES(?,?,?,?,?,?,?,'made','unknown',?,?)""",
            (bid, batch_no, p["store_id"], product_id, ts, qty, qty, actor, ts))
        self.audit(p["store_id"], "baked", {"batch_no": batch_no, "product": p["name"], "qty": qty},
                   bid, actor)
        return bid

    def record_probe(self, batch_id: str, temp: float, actor: str,
                     attachment: Optional[bytes] = None, note: str = "") -> dict[str, Any]:
        """出炉后先记录抽检温度;附件保存到本地目录。"""
        b = self._batch(batch_id)
        p = self._product(b["product_id"])
        ok = p["temp_min"] <= temp <= p["temp_max"]
        rel_path: Optional[str] = None
        if attachment is not None:
            if self.attachment_dir is None:
                raise DomainError("未配置附件目录")
            d = self.attachment_dir / b["store_id"]
            d.mkdir(parents=True, exist_ok=True)
            rel_path = f"{b['store_id']}/{_uid()}.bin"
            (self.attachment_dir / rel_path).write_bytes(attachment)
        rid = _uid()
        self.conn.execute(
            """INSERT INTO temp_readings(id,batch_id,ts,temp,source,ok,attachment,note)
               VALUES(?,?,?,?, 'probe', ?,?,?)""",
            (rid, batch_id, self.clock(), temp, 1 if ok else 0, rel_path, note))
        self.conn.execute(
            "UPDATE batches SET probed_at=?, probe_temp=?, temp_state=?, updated_at=? WHERE id=?",
            (self.clock(), temp, "ok" if ok else "bad", self.clock(), batch_id))
        self.audit(b["store_id"], "probe",
                   {"batch_no": b["batch_no"], "temp": round(temp, 1), "ok": ok,
                    "range": [p["temp_min"], p["temp_max"]], "attachment": rel_path},
                   batch_id, actor)
        if not ok:
            # 抽检不合格直接进入待下架,不得上柜
            self._to_pending(batch_id, reason="temp", actor=actor,
                             detail={"temp": round(temp, 1), "stage": "probe"})
        return {"ok": ok, "attachment": rel_path}

    def place_on_shelf(self, batch_id: str, shelf_id: str, actor: str) -> None:
        """分配热柜层位并开始计时。"""
        b = self._batch(batch_id)
        p = self._product(b["product_id"])
        s = self._shelf(shelf_id)
        if b["status"] not in ("made",):
            raise DomainError(f"批次当前状态 {b['status']},不能上柜")
        if b["probed_at"] is None:
            raise DomainError("请先记录抽检温度,再分配热柜层位")
        if b["temp_state"] == "bad":
            raise DomainError("抽检温度不合格,不能上柜")
        if s["store_id"] != b["store_id"]:
            raise DomainError("层位不属于该门店")
        now = self.clock()
        self.conn.execute(
            """UPDATE batches SET shelf_id=?, out_at=?, expires_at=?, status='placed',
                   temp_state=CASE WHEN temp_state='unknown' THEN 'ok' ELSE temp_state END,
                   updated_at=? WHERE id=?""",
            (shelf_id, now, now + p["life_minutes"] * 60, now, batch_id))
        self.audit(b["store_id"], "placed",
                   {"batch_no": b["batch_no"], "shelf": s["label"], "zone": s["zone"],
                    "product_category": p["category"]}, batch_id, actor)
        if s["zone"] != p["category"]:
            # 上错层位:允许但留痕
            self.audit(b["store_id"], "wrong_shelf",
                       {"batch_no": b["batch_no"], "product": p["name"],
                        "product_category": p["category"], "shelf": s["label"],
                        "shelf_zone": s["zone"]}, batch_id, actor)
        used = self.conn.execute(
            "SELECT COALESCE(SUM(qty_remaining),0) u FROM batches WHERE shelf_id=? AND status='placed'",
            (shelf_id,)).fetchone()["u"]
        if used + b["qty_remaining"] > s["capacity"]:
            self.audit(b["store_id"], "shelf_over_capacity",
                       {"batch_no": b["batch_no"], "shelf": s["label"],
                        "used": used, "adding": b["qty_remaining"],
                        "capacity": s["capacity"]}, batch_id, actor)

    # ------------------------------------------------------------------ #
    # 温度读数(模拟传感器持续写入)
    # ------------------------------------------------------------------ #
    def add_sensor_reading(self, batch_id: str, temp: float) -> Optional[str]:
        """写入一次传感器读数,返回新状态(pending 时表示触发待下架)。"""
        b = self._batch(batch_id)
        if b["status"] not in ("placed", "pending"):
            return None
        p = self._product(b["product_id"])
        ok = p["temp_min"] <= temp <= p["temp_max"]
        self.conn.execute(
            "INSERT INTO temp_readings(id,batch_id,ts,temp,source,ok) VALUES(?,?,?,?, 'sensor',?)",
            (_uid(), batch_id, self.clock(), temp, 1 if ok else 0))
        if ok:
            streak = 0
            state = "ok"
        else:
            streak = b["bad_streak"] + 1
            state = "bad"
        self.conn.execute("UPDATE batches SET bad_streak=?, temp_state=?, missing_flagged=0, updated_at=? WHERE id=?",
                          (streak, state, self.clock(), batch_id))
        if not ok:
            self.audit(b["store_id"], "temp_bad",
                       {"batch_no": b["batch_no"], "temp": round(temp, 1),
                        "range": [p["temp_min"], p["temp_max"]], "streak": streak}, batch_id)
        if streak >= TEMP_BAD_STREAK and b["status"] == "placed":
            self._to_pending(batch_id, reason="temp",
                             detail={"streak": streak, "last_temp": round(temp, 1)})
            return "pending"
        return None

    # ------------------------------------------------------------------ #
    # 销售扣减(乐观锁)
    # ------------------------------------------------------------------ #
    def sell(self, batch_id: str, qty: int, actor: str, role: str = "clerk",
            expected_version: Optional[int] = None,
            receipt_no: Optional[str] = None, unit_price: Optional[float] = None,
            ts: Optional[float] = None) -> dict[str, Any]:
        b = self._batch(batch_id)
        p = self._product(b["product_id"])
        if b["status"] == "discarded":
            raise DomainError("该批次已报损,不能恢复销售")
        if b["status"] == "pending":
            raise DomainError("该批次已待下架,不能继续销售")
        if b["status"] != "placed":
            raise DomainError(f"批次状态 {b['status']},尚未上柜")
        if qty <= 0:
            raise DomainError("销售数量必须大于 0")
        if expected_version is not None and expected_version != b["version"]:
            raise ConflictError(
                f"批次数量已被其他人修改(你的版本 v{expected_version},当前 v{b['version']}),请刷新后重试")
        if qty > b["qty_remaining"]:
            raise DomainError(f"库存不足:剩余 {b['qty_remaining']},本次扣减 {qty}")
        price = unit_price
        if price is None:
            price = p["base_price"] * (b["discount"] if b["discount"] else 1.0)
        now = ts if ts is not None else self.clock()
        receipt = receipt_no or f"R{int(now * 1000) % 10_000_000:07d}-{_uid()[:4]}"
        cur = self.conn.execute(
            """UPDATE batches SET qty_remaining=qty_remaining-?, version=version+1, updated_at=?
               WHERE id=? AND version=COALESCE(?, version)""",
            (qty, now, batch_id, expected_version))
        if cur.rowcount == 0:
            raise ConflictError("并发修订失败:批次版本已变更,请刷新后重试")
        self.conn.execute(
            """INSERT INTO sales(id,batch_id,ts,qty,unit_price,receipt_no,created_by)
               VALUES(?,?,?,?,?,?,?)""",
            (_uid(), batch_id, now, qty, round(price, 2), receipt, actor))
        left = b["qty_remaining"] - qty
        result = {"receipt_no": receipt, "qty": qty, "remaining": left, "unit_price": round(price, 2)}
        if left == 0:
            minutes_left = max(0.0, ((b["expires_at"] or now) - now) / 60)
            # 距到期还超过临期窗口就清空 -> 提前售罄(高峰备货不足信号)
            early = b["expires_at"] is not None and minutes_left > NEAR_WINDOW / 60
            self.conn.execute("UPDATE batches SET status='soldout', updated_at=? WHERE id=?", (now, batch_id))
            self.audit(b["store_id"], "early_soldout" if early else "soldout",
                       {"batch_no": b["batch_no"], "sold_qty": qty,
                        "minutes_to_expire": round(minutes_left, 1)},
                       batch_id, actor)
        return result

    def adjust_qty(self, batch_id: str, new_qty: int, expected_version: int, actor: str) -> None:
        """两人同时改数量:显式数量修订同样走乐观锁。"""
        b = self._batch(batch_id)
        if new_qty < 0 or new_qty > b["qty_initial"]:
            raise DomainError("修订数量超出 0 ~ 初始份数范围")
        cur = self.conn.execute(
            "UPDATE batches SET qty_remaining=?, version=version+1, updated_at=? WHERE id=? AND version=?",
            (new_qty, self.clock(), batch_id, expected_version))
        if cur.rowcount == 0:
            raise ConflictError(f"修订失败:批次已到 v{self._batch(batch_id)['version']},你持有的是 v{expected_version}")
        self.audit(b["store_id"], "qty_adjust",
                   {"batch_no": b["batch_no"], "old": b["qty_remaining"], "new": new_qty},
                   batch_id, actor)

    # ------------------------------------------------------------------ #
    # 降价:店员预设折扣 / 店长例外价格
    # ------------------------------------------------------------------ #
    def set_discount(self, batch_id: str, new_discount: float, actor: str, role: str,
                     force: bool = False) -> None:
        b = self._batch(batch_id)
        p = self._product(b["product_id"])
        if b["status"] not in ("placed",):
            raise DomainError("仅上柜销售中的批次可以调价")
        if not (0.05 <= new_discount <= 1.0):
            raise DomainError("折扣需在 0.05 ~ 1.0 之间")
        presets = json.loads(p["preset_discounts"])
        if role != "manager":
            if not b["near_flagged"]:
                raise DomainError("店员仅可对临期批次执行预设折扣")
            if not any(abs(new_discount - d) < 1e-9 for d in presets):
                raise DomainError(f"店员只能使用预设折扣 {presets},例外价格请店长处理")
        elif force is False and not b["near_flagged"]:
            # 店长非临期调价也允许,但要求显式确认 -> 前端二次确认带 force=1
            raise DomainError("非临期调价需店长二次确认")
        old = b["discount"]
        self.conn.execute(
            "INSERT INTO price_changes(id,batch_id,ts,old_discount,new_discount,actor,role) VALUES(?,?,?,?,?,?,?)",
            (_uid(), batch_id, self.clock(), old, new_discount, actor, role))
        self.conn.execute("UPDATE batches SET discount=?, version=version+1, updated_at=? WHERE id=?",
                          (new_discount, self.clock(), batch_id))
        self.audit(b["store_id"], "discount",
                   {"batch_no": b["batch_no"], "old": old, "new": new_discount,
                    "role": role, "exceptional": role == "manager" and
                    not any(abs(new_discount - d) < 1e-9 for d in presets)},
                   batch_id, actor)

    # ------------------------------------------------------------------ #
    # 效期/温度扫描(后台 tick 调用)
    # ------------------------------------------------------------------ #
    def scan_expiry(self, emit_overlap: Callable[[list[str], str], None] | None = None) -> dict[str, int]:
        now = self.clock()
        counts = {"near": 0, "expired": 0, "missing": 0}
        rows = self.conn.execute("SELECT * FROM batches WHERE status='placed'").fetchall()
        near_groups: dict[str, list[sqlite3.Row]] = {}
        for b in rows:
            # 到期
            if b["expires_at"] is not None and now >= b["expires_at"]:
                self._to_pending(b["id"], reason="expired",
                                 detail={"expires_at": b["expires_at"]})
                counts["expired"] += 1
                continue
            # 临期
            if b["expires_at"] is not None and b["expires_at"] - now <= NEAR_WINDOW:
                if not b["near_flagged"]:
                    self.conn.execute("UPDATE batches SET near_flagged=1, updated_at=? WHERE id=?",
                                      (now, b["id"]))
                    p = self._product(b["product_id"])
                    self.audit(b["store_id"], "near_expiry",
                               {"batch_no": b["batch_no"], "product": p["name"],
                                "minutes_left": round((b["expires_at"] - now) / 60, 1)}, b["id"])
                    counts["near"] += 1
                p = self._product(b["product_id"])
                near_groups.setdefault(p["category"], []).append(b)
            # 温度读数缺失
            last = self.conn.execute(
                "SELECT MAX(ts) m FROM temp_readings WHERE batch_id=?", (b["id"],)).fetchone()["m"]
            grace_start = b["out_at"] or b["probed_at"] or now
            ref = last if last is not None else grace_start
            if now - ref >= MISSING_GRACE and not b["missing_flagged"]:
                self.conn.execute(
                    "UPDATE batches SET missing_flagged=1, temp_state='missing', updated_at=? WHERE id=?",
                    (now, b["id"]))
                self.audit(b["store_id"], "temp_missing",
                           {"batch_no": b["batch_no"],
                            "minutes_since_last": round((now - ref) / 60, 1)}, b["id"])
                counts["missing"] += 1
        # 临期重叠:同品类多个临期批次
        if emit_overlap:
            for cat, group in near_groups.items():
                if len(group) >= 2:
                    ids = sorted(g["id"] for g in group)
                    emit_overlap(ids, cat)
        return counts

    def log_overlap(self, batch_ids: list[str], category: str) -> None:
        key = frozenset(batch_ids)
        if key in self._overlap_logged:
            return
        self._overlap_logged.add(key)
        store_id = self._batch(batch_ids[0])["store_id"]
        nos = [self._batch(i)["batch_no"] for i in batch_ids]
        self.audit(store_id, "near_overlap",
                   {"category": category, "batches": nos, "count": len(nos)})

    # ------------------------------------------------------------------ #
    # 待下架 / 误下架撤回 / 报损
    # ------------------------------------------------------------------ #
    def _to_pending(self, batch_id: str, reason: str, actor: str = "系统",
                    detail: dict[str, Any] | None = None) -> None:
        b = self._batch(batch_id)
        if b["status"] in ("pending", "discarded", "soldout"):
            return
        self.conn.execute(
            "UPDATE batches SET status='pending', waste_reason=?, version=version+1, updated_at=? WHERE id=?",
            (reason, self.clock(), batch_id))
        d = {"batch_no": b["batch_no"], "reason": reason}
        d.update(detail or {})
        self.audit(b["store_id"], "to_pending", d, batch_id, actor)

    def restore_from_pending(self, batch_id: str, actor: str, note: str = "") -> None:
        """误下架撤回:仅 pending 可撤回;已报损不可恢复。"""
        b = self._batch(batch_id)
        if b["status"] == "discarded":
            raise DomainError("已报损商品不能恢复销售")
        if b["status"] != "pending":
            raise DomainError("仅待下架批次可撤回")
        now = self.clock()
        if b["waste_reason"] == "expired" and b["expires_at"] and now >= b["expires_at"]:
            raise DomainError("已到期批次不能撤回")
        self.conn.execute(
            "UPDATE batches SET status='placed', waste_reason=NULL, bad_streak=0, temp_state='ok', version=version+1, updated_at=? WHERE id=?",
            (now, batch_id))
        self.audit(b["store_id"], "wrong_pull_restored",
                   {"batch_no": b["batch_no"], "origin_reason": b["waste_reason"], "note": note},
                   batch_id, actor)

    def physical_count(self, batch_id: str, counted_qty: int, actor: str) -> dict[str, Any]:
        """清点实物数量,与系统数量有差异即留痕(不自动改账)。"""
        b = self._batch(batch_id)
        diff = counted_qty - b["qty_remaining"]
        self.audit(b["store_id"], "qty_variance" if diff != 0 else "count_ok",
                   {"batch_no": b["batch_no"], "system_qty": b["qty_remaining"],
                    "counted_qty": counted_qty, "diff": diff}, batch_id, actor)
        return {"diff": diff}

    def confirm_waste(self, batch_id: str, counted_qty: int, actor: str,
                      reason: Optional[str] = None) -> dict[str, Any]:
        """员工确认清点后完成报损。报损不可逆。"""
        b = self._batch(batch_id)
        if b["status"] == "discarded":
            raise DomainError("该批次已报损,不能重复报损")
        if b["status"] != "pending":
            raise DomainError("仅待下架批次可以清点报损")
        if counted_qty < 0 or counted_qty > b["qty_initial"]:
            raise DomainError("清点数量不合法")
        diff = counted_qty - b["qty_remaining"]
        now = self.clock()
        final_reason = reason or b["waste_reason"] or "expired"
        cur = self.conn.execute(
            """UPDATE batches SET status='discarded', wasted_qty=?, wasted_at=?,
                   waste_reason=?, version=version+1, updated_at=? WHERE id=?""",
            (counted_qty, now, final_reason, now, batch_id))
        if cur.rowcount == 0:
            raise ConflictError("批次状态已变更,请刷新后重试")
        self.audit(b["store_id"], "wasted",
                   {"batch_no": b["batch_no"], "reason": final_reason,
                    "system_qty": b["qty_remaining"], "counted_qty": counted_qty,
                    "diff": diff}, batch_id, actor)
        if diff != 0:
            self.audit(b["store_id"], "qty_variance",
                       {"batch_no": b["batch_no"], "system_qty": b["qty_remaining"],
                        "counted_qty": counted_qty, "diff": diff, "stage": "waste_count"},
                       batch_id, actor)
        return {"wasted_qty": counted_qty, "diff": diff, "reason": final_reason}

    def close_store(self, store_id: str, actor: str) -> list[str]:
        """闭店:所有在柜批次转待下架(闭店剩余),随后到下架清点页逐个清点报损。"""
        now = self.clock()
        rows = self.conn.execute(
            "SELECT * FROM batches WHERE store_id=? AND status='placed'", (store_id,)).fetchall()
        ids: list[str] = []
        for b in rows:
            self.conn.execute(
                "UPDATE batches SET status='pending', waste_reason='close_remaining', version=version+1, updated_at=? WHERE id=?",
                (now, b["id"]))
            self.audit(store_id, "close_remaining",
                       {"batch_no": b["batch_no"], "remaining": b["qty_remaining"]}, b["id"], actor)
            ids.append(b["id"])
        return ids

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def _product(self, pid: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise DomainError(f"商品不存在: {pid}")
        return r

    def _batch(self, bid: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if r is None:
            raise DomainError(f"批次不存在: {bid}")
        return r

    def _shelf(self, sid: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM shelves WHERE id=?", (sid,)).fetchone()
        if r is None:
            raise DomainError(f"层位不存在: {sid}")
        return r

    def list_batches(self, store_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT b.*, p.name product_name, p.category, p.base_price, p.temp_min, p.temp_max,
                      p.preset_discounts, s.label shelf_label, s.zone shelf_zone
               FROM batches b JOIN products p ON p.id=b.product_id
               LEFT JOIN shelves s ON s.id=b.shelf_id
               WHERE b.store_id=? ORDER BY b.produced_at DESC""", (store_id,)))

    def batch_detail(self, batch_id: str) -> dict[str, Any]:
        b = self._batch(batch_id)
        p = self._product(b["product_id"])
        temps = self.conn.execute(
            "SELECT * FROM temp_readings WHERE batch_id=? ORDER BY ts DESC LIMIT 30", (batch_id,)).fetchall()
        sales = self.conn.execute(
            "SELECT * FROM sales WHERE batch_id=? ORDER BY ts DESC", (batch_id,)).fetchall()
        return {"batch": b, "product": p, "temps": temps, "sales": sales}

    def list_shelves(self, store_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM shelves WHERE store_id=? ORDER BY cabinet_name, layer_index", (store_id,)))

    def daily_report(self, store_id: str, day_start: float, day_end: float) -> dict[str, Any]:
        B = self.conn.execute  # noqa
        batches = self.conn.execute(
            "SELECT * FROM batches WHERE store_id=? AND produced_at>=? AND produced_at<?",
            (store_id, day_start, day_end)).fetchall()
        sales = self.conn.execute(
            "SELECT * FROM sales s JOIN batches b ON b.id=s.batch_id WHERE b.store_id=? AND s.ts>=? AND s.ts<?",
            (store_id, day_start, day_end)).fetchall()
        revenue = round(sum(r["qty"] * r["unit_price"] for r in sales), 2)
        wasted = [r for r in batches if r["status"] == "discarded"]
        waste_cost = round(sum((r["wasted_qty"] or 0) *
                               self._product(r["product_id"])["base_price"] for r in wasted), 2)
        by_reason: dict[str, int] = {}
        for r in wasted:
            by_reason[r["waste_reason"]] = by_reason.get(r["waste_reason"], 0) + (r["wasted_qty"] or 0)
        return {
            "batch_count": len(batches),
            "sold_qty": sum(r["qty"] for r in sales),
            "revenue": revenue,
            "waste_qty": sum(r["wasted_qty"] or 0 for r in wasted),
            "waste_cost": waste_cost,
            "waste_by_reason": by_reason,
        }

    def commit(self) -> None:
        self.conn.commit()
