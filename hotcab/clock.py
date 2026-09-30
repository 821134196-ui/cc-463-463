"""可注入的时间源，测试用 FakeClock，种子用 OffsetClock。"""
import datetime as _dt


class SystemClock:
    def now(self):
        return _dt.datetime.now()

    def today(self):
        return self.now().date()


class FakeClock:
    """测试时钟：手工推进，冻结在某个时刻。"""

    def __init__(self, start=None):
        self._now = start or _dt.datetime(2026, 9, 30, 12, 0, 0)

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def advance(self, minutes=0, seconds=0):
        self._now += _dt.timedelta(minutes=minutes, seconds=seconds)
        return self._now

    def set(self, dt):
        self._now = dt


class OffsetClock:
    """种子时钟：在真实时间上加固定偏移，便于把历史样例落在今天。"""

    def __init__(self, base: SystemClock, offset: _dt.timedelta = None):
        self._base = base
        self.offset = offset or _dt.timedelta(0)

    def now(self):
        return self._base.now() + self.offset

    def today(self):
        return self.now().date()

    def set_offset(self, offset):
        self.offset = offset
