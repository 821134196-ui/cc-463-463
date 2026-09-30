"""测试:批次扣减、并发修订、效期边界、折扣权限、报损不可逆,以及各留痕路径。

运行:python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import open_db
from app.service import (ConflictError, DomainError, HotFoodService,
                         MISSING_GRACE, NEAR_WINDOW, TEMP_BAD_STREAK)


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn: sqlite3.Connection = open_db(self.dir / "t.db")
        self.clock = FakeClock()
        self.att = self.dir / "att"
        self.svc = HotFoodService(self.conn, clock=self.clock, attachment_dir=self.att)
        self.svc.add_store("S", "测试店")
        # 效期 100 分钟,保温 60~72,预设折扣 0.8/0.7
        self.svc.add_product("P1", "S", "C1", "照烧便当", "便当", 16.0, 60, 72, 100, [0.8, 0.7])
        self.svc.add_product("P2", "S", "C2", "炸鸡块", "炸物", 10.0, 60, 72, 60, [0.8])
        self.svc.add_shelf("SH1", "S", "柜", 1, "便当区", "便当", 40)
        self.svc.add_shelf("SH2", "S", "柜", 2, "炸物区", "炸物", 2)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---------------- 辅助 ---------------- #
    def bake_place(self, product="P1", qty=10, temp=66.0) -> str:
        bid = self.svc.make_batch(product, qty, "员工")
        self.svc.record_probe(bid, temp, "员工")
        shelf = "SH1" if product == "P1" else "SH2"
        self.svc.place_on_shelf(bid, shelf, "员工")
        return bid

    def event_types(self) -> set[str]:
        return {e["event_type"] for e in self.svc.events("S", 500)}

    def batch(self, bid):
        return self.svc._batch(bid)

    # ---------------- 出炉 -> 抽检 -> 上柜 ---------------- #
    def test_probe_required_before_placement(self):
        bid = self.svc.make_batch("P1", 5, "员工")
        with self.assertRaises(DomainError):
            self.svc.place_on_shelf(bid, "SH1", "员工")
        self.svc.record_probe(bid, 66.0, "员工")
        self.svc.place_on_shelf(bid, "SH1", "员工")
        self.assertEqual(self.batch(bid)["status"], "placed")

    def test_failed_probe_blocks_shelf_and_pending(self):
        bid = self.svc.make_batch("P1", 5, "员工")
        r = self.svc.record_probe(bid, 80.0, "员工")
        self.assertFalse(r["ok"])
        with self.assertRaises(DomainError):
            self.svc.place_on_shelf(bid, "SH1", "员工")
        self.assertEqual(self.batch(bid)["status"], "pending")
        self.assertEqual(self.batch(bid)["waste_reason"], "temp")

    def test_probe_attachment_saved_locally(self):
        bid = self.svc.make_batch("P1", 5, "员工")
        r = self.svc.record_probe(bid, 66.0, "员工", attachment=b"\x00\x01photo")
        self.assertTrue((self.att / r["attachment"]).exists())

    def test_wrong_shelf_leaves_audit_but_allowed(self):
        bid = self.svc.make_batch("P2", 5, "员工")   # 炸物
        self.svc.record_probe(bid, 66.0, "员工")
        self.svc.place_on_shelf(bid, "SH1", "员工")  # 放到便当区
        self.assertEqual(self.batch(bid)["status"], "placed")
        self.assertIn("wrong_shelf", self.event_types())

    # ---------------- 批次扣减 ---------------- #
    def test_sell_deducts_by_batch_and_price(self):
        bid = self.bake_place(qty=10)
        r = self.svc.sell(bid, 3, "员工")
        self.assertEqual(r["remaining"], 7)
        self.assertEqual(r["unit_price"], 16.0)
        self.assertEqual(self.batch(bid)["qty_remaining"], 7)
        self.assertEqual(self.batch(bid)["version"], 1)

    def test_sell_rejects_over_remaining_pending_discarded(self):
        bid = self.bake_place(qty=2)
        with self.assertRaises(DomainError):
            self.svc.sell(bid, 3, "员工")
        self.svc.sell(bid, 2, "员工")
        self.assertEqual(self.batch(bid)["status"], "soldout")
        with self.assertRaises(DomainError):
            self.svc.sell(bid, 1, "员工")  # 已售罄

    def test_normal_soldout_vs_early_soldout(self):
        bid = self.bake_place(qty=2)
        self.clock.advance(100 * 60 - 60)     # 距到期 1 分钟(临期窗口内)
        self.svc.sell(bid, 2, "员工")
        self.assertNotIn("early_soldout", self.event_types())
        bid2 = self.bake_place(qty=2)
        self.clock.advance(10 * 60)           # 距到期还 90 分钟
        self.svc.sell(bid2, 2, "员工")
        self.assertIn("early_soldout", self.event_types())

    # ---------------- 并发修订 ---------------- #
    def test_concurrent_sell_stale_version_fails(self):
        bid = self.bake_place(qty=10)
        self.svc.sell(bid, 2, "甲", expected_version=0)   # 甲先成功 -> v1
        with self.assertRaises(ConflictError):
            self.svc.sell(bid, 3, "乙", expected_version=0)  # 乙的旧版本失败
        # 乙刷新后提交成功
        r = self.svc.sell(bid, 3, "乙", expected_version=1)
        self.assertEqual(r["remaining"], 5)

    def test_concurrent_qty_adjust_stale_version_fails(self):
        bid = self.bake_place(qty=10)
        self.svc.adjust_qty(bid, 9, expected_version=0, actor="甲")
        with self.assertRaises(ConflictError):
            self.svc.adjust_qty(bid, 8, expected_version=0, actor="乙")
        self.svc.adjust_qty(bid, 8, expected_version=1, actor="乙")
        self.assertEqual(self.batch(bid)["qty_remaining"], 8)

    def test_no_version_always_applies_latest(self):
        bid = self.bake_place(qty=10)
        self.svc.sell(bid, 1, "甲")
        self.svc.sell(bid, 1, "乙")
        self.assertEqual(self.batch(bid)["qty_remaining"], 8)

    # ---------------- 效期边界 ---------------- #
    def test_near_boundary_and_expiry_boundary(self):
        bid = self.bake_place(qty=5)
        t0 = self.clock()
        out_at = self.batch(bid)["out_at"]
        expires = self.batch(bid)["expires_at"]
        self.assertEqual(round(expires - out_at), 100 * 60)

        # 还差 25 分钟多:未临期
        self.clock.t = expires - NEAR_WINDOW - 1
        c = self.svc.scan_expiry()
        self.assertEqual(c["near"], 0)
        self.assertEqual(self.batch(bid)["near_flagged"], 0)

        # 恰好进入 25 分钟窗口:标记临期
        self.clock.t = expires - NEAR_WINDOW
        self.svc.scan_expiry()
        self.assertEqual(self.batch(bid)["near_flagged"], 1)
        self.assertIn("near_expiry", self.event_types())

        # 到期前 1 秒:仍在柜
        self.clock.t = expires - 1
        self.svc.scan_expiry()
        self.assertEqual(self.batch(bid)["status"], "placed")

        # 恰好到期:转待下架
        self.clock.t = expires
        self.svc.scan_expiry()
        self.assertEqual(self.batch(bid)["status"], "pending")
        self.assertEqual(self.batch(bid)["waste_reason"], "expired")

    def test_temp_bad_streak_threshold(self):
        bid = self.bake_place(qty=5)
        # 连续 TEMP_BAD_STREAK-1 次不合格:仍在柜
        for _ in range(TEMP_BAD_STREAK - 1):
            self.assertIsNone(self.svc.add_sensor_reading(bid, 80.0))
        self.assertEqual(self.batch(bid)["status"], "placed")
        # 第 3 次:转待下架
        self.assertEqual(self.svc.add_sensor_reading(bid, 80.0), "pending")
        self.assertEqual(self.batch(bid)["status"], "pending")
        self.assertEqual(self.batch(bid)["waste_reason"], "temp")
        # 一次合格清零计数
        # (已 pending,另开批次验证 streak 复位)
        bid2 = self.bake_place(qty=5)
        self.svc.add_sensor_reading(bid2, 80.0)
        self.svc.add_sensor_reading(bid2, 66.0)
        self.svc.add_sensor_reading(bid2, 80.0)
        self.assertEqual(self.batch(bid2)["status"], "placed")

    def test_temp_missing_after_grace(self):
        bid = self.bake_place(qty=5)
        self.clock.advance(MISSING_GRACE - 1)
        self.svc.scan_expiry()
        self.assertEqual(self.batch(bid)["missing_flagged"], 0)
        self.clock.advance(1)
        c = self.svc.scan_expiry()
        self.assertEqual(c["missing"], 1)
        self.assertEqual(self.batch(bid)["temp_state"], "missing")
        self.assertIn("temp_missing", self.event_types())

    def test_near_overlap_logged_once(self):
        b1 = self.bake_place(qty=5)
        b2 = self.bake_place(qty=5)
        exp1 = self.batch(b1)["expires_at"]
        # 把两个便当都推进临期窗口
        self.clock.t = exp1 - 10 * 60
        self.svc.scan_expiry(emit_overlap=self.svc.log_overlap)
        self.svc.scan_expiry(emit_overlap=self.svc.log_overlap)
        overlaps = [e for e in self.svc.events("S", 500)
                    if e["event_type"] == "near_overlap"]
        self.assertEqual(len(overlaps), 1)   # 同一组合不重复留痕

    # ---------------- 折扣权限 ---------------- #
    def test_clerk_preset_discount_near_only(self):
        bid = self.bake_place(qty=10)
        # 未临期:店员任何折扣都不行
        with self.assertRaises(DomainError):
            self.svc.set_discount(bid, 0.8, "店员", "clerk")
        # 推进到临期
        self.clock.t = self.batch(bid)["expires_at"] - 10 * 60
        self.svc.scan_expiry()
        # 非预设折扣不行
        with self.assertRaises(DomainError):
            self.svc.set_discount(bid, 0.5, "店员", "clerk")
        # 预设折扣可以,销售按折扣价
        self.svc.set_discount(bid, 0.8, "店员", "clerk")
        r = self.svc.sell(bid, 1, "店员")
        self.assertEqual(r["unit_price"], 12.8)

    def test_manager_exception_price_with_confirmation(self):
        bid = self.bake_place(qty=10)
        with self.assertRaises(DomainError):
            self.svc.set_discount(bid, 0.5, "店长", "manager")          # 未二次确认
        self.svc.set_discount(bid, 0.5, "店长", "manager", force=True)  # 确认后放行
        r = self.svc.sell(bid, 2, "店长", role="manager")
        self.assertEqual(r["unit_price"], 8.0)

    # ---------------- 报损不可逆 ---------------- #
    def test_waste_irreversible(self):
        bid = self.bake_place(qty=6)
        self.svc._to_pending(bid, "expired")
        r = self.svc.confirm_waste(bid, 5, "员工")      # 实物 5,账上 6 -> 差异留痕
        self.assertEqual(r["diff"], -1)
        self.assertEqual(self.batch(bid)["status"], "discarded")
        self.assertEqual(self.batch(bid)["wasted_qty"], 5)
        for fn in (
            lambda: self.svc.sell(bid, 1, "员工"),
            lambda: self.svc.confirm_waste(bid, 1, "员工"),
            lambda: self.svc.restore_from_pending(bid, "员工"),
        ):
            with self.assertRaises(DomainError):
                fn()
        self.assertIn("qty_variance", self.event_types())
        self.assertIn("wasted", self.event_types())

    def test_wrong_pull_restore_allowed_unless_expired(self):
        # 温度原因待下架:可误下架撤回
        bid = self.bake_place(qty=5)
        self.svc._to_pending(bid, "temp")
        self.svc.restore_from_pending(bid, "员工", note="复温合格")
        self.assertEqual(self.batch(bid)["status"], "placed")
        self.assertIn("wrong_pull_restored", self.event_types())
        # 到期批次:不能撤回
        bid2 = self.bake_place(qty=5)
        self.svc._to_pending(bid2, "expired")
        self.clock.t = self.batch(bid2)["expires_at"] + 1
        with self.assertRaises(DomainError):
            self.svc.restore_from_pending(bid2, "员工")

    def test_physical_count_difference_audited(self):
        bid = self.bake_place(qty=8)
        self.svc._to_pending(bid, "expired")
        self.svc.physical_count(bid, 10, "员工")
        self.assertIn("qty_variance", self.event_types())

    # ---------------- 闭店剩余 ---------------- #
    def test_close_store_marks_remaining(self):
        b1 = self.bake_place(qty=5)
        b2 = self.bake_place(qty=3)
        ids = self.svc.close_store("S", "店长")
        self.assertEqual(set(ids), {b1, b2})
        self.assertEqual(self.batch(b1)["waste_reason"], "close_remaining")
        self.assertEqual(self.batch(b1)["status"], "pending")
        # 闭店剩余清点后报损
        self.svc.confirm_waste(b1, 5, "夜班员工")
        self.assertEqual(self.batch(b1)["status"], "discarded")

    # ---------------- 日报 ---------------- #
    def test_daily_report_aggregates(self):
        bid = self.bake_place(qty=10)
        self.svc.set_discount  # 静音
        self.clock.t = self.batch(bid)["expires_at"] - 10 * 60
        self.svc.scan_expiry()
        self.svc.set_discount(bid, 0.8, "店员", "clerk")
        self.svc.sell(bid, 4, "员工")
        self.svc._to_pending(bid, "expired")
        self.clock.t = self.batch(bid)["expires_at"]
        self.svc.confirm_waste(bid, 6, "员工")
        rep = self.svc.daily_report("S", self.clock.t - 24 * 3600, self.clock.t + 1)
        self.assertEqual(rep["sold_qty"], 4)
        self.assertEqual(rep["waste_qty"], 6)
        self.assertEqual(rep["revenue"], round(4 * 12.8, 2))
        self.assertEqual(rep["waste_by_reason"].get("expired"), 6)


if __name__ == "__main__":
    unittest.main()
