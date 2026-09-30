"""模拟适配器：持续返回温度读数与销售回执（确定性伪随机，可复现样例）。"""
import random


class TempAdapter:
    """模拟热柜探头。正常时温度落在商品温区中部，可注入故障批次持续低温，
    并以小概率回传 None（读数缺失）。"""

    def __init__(self, seed=20260930, missing_rate=0.06):
        self.rng = random.Random(seed)
        self.missing_rate = missing_rate
        self.faulty = set()   # batch_id -> 持续不合格
        self.faulty_temp = {}

    def mark_faulty(self, batch_id, temp=52.0):
        self.faulty.add(batch_id)
        self.faulty_temp[batch_id] = temp

    def clear_faulty(self, batch_id):
        self.faulty.discard(batch_id)
        self.faulty_temp.pop(batch_id, None)

    def read(self, batch):
        if batch["id"] in self.faulty:
            return round(self.faulty_temp[batch["id"]]
                         + self.rng.uniform(-1.5, 1.0), 1)
        if self.rng.random() < self.missing_rate:
            return None
        lo, hi = batch["temp_min"], batch["temp_max"]
        mid = (lo + hi) / 2
        return round(mid + self.rng.uniform(-(hi - lo) / 4,
                                            (hi - lo) / 4), 1)


class SalesAdapter:
    """模拟 POS 销售回执。每个节拍按概率对在售批次扣减 1-3 份，
    临期批次（有折扣）走量更快；指定批次会被快速买空（提前售罄样例）。"""

    def __init__(self, seed=7788):
        self.rng = random.Random(seed)
        self.hot = set()        # 高动销批次
        self.boost = set()      # 高峰：大概率售出

    def mark_hot(self, batch_id):
        self.hot.add(batch_id)

    def peak(self, batch_ids):
        self.boost |= set(batch_ids)

    def end_peak(self):
        self.boost.clear()

    def next_receipts(self, batches):
        """返回 [(batch_id, qty), ...]。"""
        out = []
        for b in batches:
            p = 0.12
            if b.get("near_expiry"):
                p = 0.55
            if b["id"] in self.hot:
                p = 0.75
            if b["id"] in self.boost:
                p = 0.9
            if self.rng.random() < p:
                if b["id"] in self.hot or b["id"] in self.boost:
                    qty = self.rng.choice([1, 2, 2, 3])
                else:
                    qty = self.rng.choice([1, 1, 2])
                if qty <= b["remaining"]:
                    out.append((b["id"], qty))
        return out
