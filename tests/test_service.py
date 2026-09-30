"""核心规则测试：批次扣减、并发修订、效期边界、折扣权限、报损不可逆。

运行：python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import datetime

from hotcab.clock import FakeClock
from hotcab.database import init_db
from hotcab.errors import (AuthError, ConflictError, StateError,
                           ValidationError)
from hotcab.seed import seed_master
from hotcab.service import (AUDIT_NEAR_OVERLAP, AUDIT_QTY_DIFF,
                            AUDIT_TEMP_FAIL, AUDIT_TEMP_MISSING,
                            AUDIT_WRONG_SHELF, BatchService)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        init_db(self.db)
        seed_master(self.db)
        self.clock = FakeClock(datetime(2026, 9, 30, 12, 0, 0))
        self.svc = BatchService(self.clock, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 辅助 ----
    def make_on_sale(self, product_id=3, qty=10, temp=70.0, shelf=None):
        """出炉→测温→上柜，返回批次。炸物(3) 效期 180 分，层位 2。"""
        b = self.svc.create_batch(1, product_id, qty, actor="店员")
        self.svc.check_temp(b["id"], temp, actor="店员")
        if shelf is None:
            shelf = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}[product_id]
        return self.svc.assign_shelf(b["id"], shelf, actor="店员")

    def audit_types(self, batch_id):
        return {e["event_type"] for e in
                self.svc.audit_list(batch_id=batch_id)}

    # ---------- 1. 批次扣减 ----------
    def test_sell_deducts_by_batch(self):
        b = self.make_on_sale(qty=10)
        b = self.svc.sell(b["id"], 3)
        self.assertEqual(b["remaining"], 7)
        self.assertEqual(b["sold"], 3)
        sales = self.svc.sales_list(1, date="2026-09-30")
        self.assertEqual(sales[0]["qty"], 3)
        self.assertEqual(sales[0]["batch_no"], b["batch_no"])

    def test_sell_oversell_rejected(self):
        b = self.make_on_sale(qty=2)
        with self.assertRaises(StateError):
            self.svc.sell(b["id"], 3)
        self.assertEqual(self.svc.get_batch(b["id"])["remaining"], 2)

    def test_sell_to_zero_is_early_soldout_with_audit(self):
        b = self.make_on_sale(qty=4)
        b = self.svc.sell(b["id"], 4)
        self.assertEqual(b["status"], "sold_out")
        types = self.audit_types(b["id"])
        self.assertIn("early_soldout", types)
        # 售罄后不能继续扣减
        with self.assertRaises(StateError):
            self.svc.sell(b["id"], 1)

    def test_invalid_qty(self):
        b = self.make_on_sale(qty=4)
        with self.assertRaises(ValidationError):
            self.svc.sell(b["id"], 0)

    # ---------- 2. 并发修订（乐观锁） ----------
    def test_concurrent_old_version_commit_fails(self):
        b = self.make_on_sale(qty=10)
        v = b["version"]
        # 店员甲先成功扣减
        self.svc.sell(b["id"], 2, expected_version=v)
        # 店员乙拿着旧版本同时提交 → 冲突
        with self.assertRaises(ConflictError):
            self.svc.sell(b["id"], 1, expected_version=v)
        # 刷新到最新版本后可提交，库存只扣减甲的 2 份 + 乙的 1 份
        fresh = self.svc.get_batch(b["id"])
        self.assertEqual(fresh["remaining"], 8)
        self.svc.sell(b["id"], 1, expected_version=fresh["version"])
        self.assertEqual(self.svc.get_batch(b["id"])["remaining"], 7)

    def test_concurrent_discount_and_sell(self):
        b = self.make_on_sale(qty=10)
        v = b["version"]
        self.clock.advance(minutes=170)  # 剩 10 分，可 7 折
        self.svc.apply_discount(b["id"], 70, role="clerk",
                                expected_version=v)
        with self.assertRaises(ConflictError):
            self.svc.sell(b["id"], 1, expected_version=v)

    # ---------- 3. 效期边界 ----------
    def test_near_threshold_boundary(self):
        # 20 分钟阈值：剩 21 分不临期，剩 20 分整进入临期
        b = self.make_on_sale(qty=5)  # 炸物效期 180 分
        self.clock.advance(minutes=159)  # 剩 21 分
        self.svc.tick_expiry()
        self.assertFalse(self.svc.get_batch(b["id"])["near_expiry"])
        self.clock.advance(minutes=1)    # 剩 20 分整（含边界）
        r = self.svc.tick_expiry()
        self.assertIn(b["id"], r["near_marked"])
        self.assertTrue(self.svc.get_batch(b["id"])["near_expiry"])

    def test_expiry_boundary_exactly_at_expiry(self):
        b = self.make_on_sale(qty=5)
        self.clock.advance(minutes=180)  # 恰好到达到期时刻
        # 边界时刻不能再销售
        with self.assertRaises(StateError):
            self.svc.sell(b["id"], 1)
        r = self.svc.tick_expiry()
        self.assertIn(b["id"], r["moved_to_pending"])
        self.assertEqual(self.svc.get_batch(b["id"])["status"], "pending_off")
        self.assertEqual(self.svc.get_batch(b["id"])["pending_reason"],
                         "expired")

    def test_one_second_before_expiry_still_saleable(self):
        b = self.make_on_sale(qty=5)
        self.clock.advance(minutes=179, seconds=59)
        b = self.svc.sell(b["id"], 1)
        self.assertEqual(b["status"], "on_sale")

    def test_near_overlap_audited(self):
        # 同品两批同刻上柜，剩 20 分时同时临期 → 第二批发起重叠留痕
        b1 = self.make_on_sale(product_id=3, qty=6)
        b2 = self.make_on_sale(product_id=3, qty=6)
        self.clock.advance(minutes=160)
        r = self.svc.tick_expiry()
        self.assertIn(b1["id"], r["near_marked"])
        self.assertIn(b2["id"], r["near_marked"])
        self.assertIn(AUDIT_NEAR_OVERLAP, self.audit_types(b2["id"]))

    # ---------- 4. 折扣权限 ----------
    def test_clerk_preset_discount_in_window(self):
        b = self.make_on_sale(qty=8)
        self.clock.advance(minutes=161)  # 剩 19 分 → 9 折窗口
        b = self.svc.apply_discount(b["id"], 90, role="clerk",
                                    actor="小林")
        self.assertEqual(b["discount_rate"], 90)
        # 折后价立即生效：6.9 * 0.9 = 6.21
        b = self.svc.sell(b["id"], 1)
        self.assertEqual(b["current_price"], 6.21)

    def test_clerk_preset_early_rejected(self):
        b = self.make_on_sale(qty=8)
        self.clock.advance(minutes=140)  # 剩 40 分，未到任何窗口
        with self.assertRaises(AuthError):
            self.svc.apply_discount(b["id"], 90, role="clerk")
        # 未到 10 分窗口时 7 折也被拒绝
        self.clock.advance(minutes=21)  # 剩 19 分
        with self.assertRaises(AuthError):
            self.svc.apply_discount(b["id"], 70, role="clerk")

    def test_clerk_non_preset_rate_rejected(self):
        b = self.make_on_sale(qty=8)
        self.clock.advance(minutes=170)  # 已进窗口但 85 不是预设
        with self.assertRaises(AuthError):
            self.svc.apply_discount(b["id"], 85, role="clerk")

    def test_manager_exception_price_allowed(self):
        b = self.make_on_sale(qty=8)
        b = self.svc.apply_discount(b["id"], 50, role="manager",
                                    label="店长特例5折")
        self.assertEqual(b["discount_rate"], 50)
        self.assertEqual(b["discount_label"], "店长特例5折")

    # ---------- 5. 报损不可逆 ----------
    def test_loss_is_irreversible(self):
        b = self.make_on_sale(qty=6)
        self.clock.advance(minutes=180)
        self.svc.tick_expiry()
        b, meta = self.svc.confirm_loss(b["id"], 6, actor="店长")
        self.assertEqual(b["status"], "loss")
        # 不能恢复销售
        with self.assertRaises(StateError):
            self.svc.sell(b["id"], 1)
        # 不能二次报损
        with self.assertRaises(StateError):
            self.svc.confirm_loss(b["id"], 6, actor="店长")
        # 不能撤销
        with self.assertRaises(StateError):
            self.svc.restore_wrong_off(b["id"])

    def test_loss_qty_discrepancy_audited(self):
        b = self.make_on_sale(qty=10)
        self.clock.advance(minutes=180)
        self.svc.tick_expiry()
        b, meta = self.svc.confirm_loss(b["id"], 8, actor="店长")
        self.assertEqual(meta["difference"], 2)
        self.assertIn(AUDIT_QTY_DIFF, self.audit_types(b["id"]))
        self.assertEqual(b["remaining"], 2)  # 差异部分留存账面
        self.assertEqual(b["loss_qty"], 8)

    def test_counted_over_remaining_rejected(self):
        b = self.make_on_sale(qty=3)
        self.svc.mark_pending_manual(b["id"])
        with self.assertRaises(ValidationError):
            self.svc.confirm_loss(b["id"], 4, actor="店长")

    # ---------- 温度规则 ----------
    def test_missing_check_temp_blocks_shelf(self):
        b = self.svc.create_batch(1, 3, 6)
        self.svc.check_temp(b["id"], None)
        with self.assertRaises(StateError):
            self.svc.assign_shelf(b["id"], 2)
        self.assertIn(AUDIT_TEMP_MISSING, self.audit_types(b["id"]))

    def test_bad_check_temp_blocks_shelf(self):
        b = self.svc.create_batch(1, 3, 6)
        b = self.svc.check_temp(b["id"], 40.0)  # 炸物温区 60-80
        self.assertFalse(b["check_ok"])
        with self.assertRaises(StateError):
            self.svc.assign_shelf(b["id"], 2)

    def test_consecutive_bad_temps_move_to_pending(self):
        b = self.make_on_sale(qty=6)
        self.svc.ingest_temp(b["id"], 52.0)
        self.svc.ingest_temp(b["id"], 51.0)
        self.assertEqual(self.svc.get_batch(b["id"])["status"], "on_sale")
        self.svc.ingest_temp(b["id"], 50.0)  # 第 3 次
        b = self.svc.get_batch(b["id"])
        self.assertEqual(b["status"], "pending_off")
        self.assertEqual(b["pending_reason"], "temp")
        self.assertIn(AUDIT_TEMP_FAIL, self.audit_types(b["id"]))
        # 恢复一次合格读数不会清零计数（已下架）
        self.svc.ingest_temp(b["id"], 70.0)
        self.assertEqual(self.svc.get_batch(b["id"])["status"], "pending_off")

    def test_good_temp_resets_bad_streak(self):
        b = self.make_on_sale(qty=6)
        self.svc.ingest_temp(b["id"], 52.0)
        self.svc.ingest_temp(b["id"], 70.0)
        self.svc.ingest_temp(b["id"], 52.0)
        self.assertEqual(self.svc.get_batch(b["id"])["status"], "on_sale")

    # ---------- 层位 / 误下架 / 闭店 ----------
    def test_wrong_shelf_rejected_and_audited(self):
        b = self.svc.create_batch(1, 5, 6)  # 面点
        self.svc.check_temp(b["id"], 64.0)
        with self.assertRaises(ValidationError):
            self.svc.assign_shelf(b["id"], 2)  # 炸物层
        self.assertIn(AUDIT_WRONG_SHELF, self.audit_types(b["id"]))
        # 正确层位可上柜
        self.svc.assign_shelf(b["id"], 3)
        self.assertEqual(self.svc.get_batch(b["id"])["status"], "on_sale")

    def test_wrong_manual_off_can_restore(self):
        b = self.make_on_sale(qty=6)
        v = self.svc.get_batch(b["id"])["version"]
        self.svc.mark_pending_manual(b["id"], expected_version=v)
        b = self.svc.restore_wrong_off(b["id"], actor="小赵")
        self.assertEqual(b["status"], "on_sale")
        self.assertIn("wrong_off", self.audit_types(b["id"]))

    def test_expired_batch_cannot_restore(self):
        b = self.make_on_sale(qty=6)
        self.svc.mark_pending_manual(b["id"])
        self.clock.advance(minutes=181)
        with self.assertRaises(StateError):
            self.svc.restore_wrong_off(b["id"])

    def test_temp_pending_cannot_restore(self):
        b = self.make_on_sale(qty=6)
        for t in (52, 51, 50):
            self.svc.ingest_temp(b["id"], float(t))
        with self.assertRaises(StateError):
            self.svc.restore_wrong_off(b["id"])

    def test_close_store_leaves_remainders(self):
        b1 = self.make_on_sale(qty=6)
        b2 = self.make_on_sale(product_id=5, qty=8, shelf=3)
        r = self.svc.close_store(1, batch_ids=[b1["id"], b2["id"]])
        self.assertEqual(r["qty"], 14)
        for bid in (b1["id"], b2["id"]):
            b = self.svc.get_batch(bid)
            self.assertEqual(b["status"], "pending_off")
            self.assertEqual(b["pending_reason"], "close")
            self.assertIn("close_remain", self.audit_types(bid))


if __name__ == "__main__":
    unittest.main()
