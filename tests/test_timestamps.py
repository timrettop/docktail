"""
test_timestamps.py — Tests for _ts_epoch(), _fmt_ts(), and _smart_ts().

_smart_ts() computes its output relative to "today", so tests use epoch
offsets from time.time() rather than fixed timestamps.
"""
import time
import pytest
from core import _fmt_ts, _smart_ts, _ts_epoch


# ── _ts_epoch: ISO string → Unix float ───────────────────────────────────────

class TestTsEpoch:
    def test_iso_z_suffix(self):
        epoch = _ts_epoch("2026-05-30T14:58:52.035632Z")
        assert epoch > 0
        # Spot-check the date
        import datetime
        dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        assert dt.year  == 2026
        assert dt.month == 5
        assert dt.day   == 30
        assert dt.hour  == 14

    def test_iso_utc_offset(self):
        epoch = _ts_epoch("2026-05-30T14:58:52+00:00")
        assert epoch > 0

    def test_nanosecond_precision(self):
        """Docker adds nanosecond timestamps — should parse without raising."""
        epoch = _ts_epoch("2026-05-30T14:58:52.035712555Z")
        assert epoch > 0

    def test_python_logging_format(self):
        """YYYY-MM-DD HH:MM:SS as emitted by Python's logging module."""
        epoch = _ts_epoch("2026-05-30 09:19:33")
        assert epoch > 0

    def test_invalid_returns_current_time(self):
        before = time.time()
        epoch = _ts_epoch("not-a-timestamp-at-all")
        after = time.time()
        assert before <= epoch <= after + 1

    def test_empty_string_returns_current_time(self):
        before = time.time()
        epoch = _ts_epoch("")
        after = time.time()
        assert before <= epoch <= after + 1

    def test_two_equal_iso_strings_give_equal_epochs(self):
        a = _ts_epoch("2026-01-15T08:30:00Z")
        b = _ts_epoch("2026-01-15T08:30:00+00:00")
        assert abs(a - b) < 1.0   # same moment, allow float rounding


# ── _fmt_ts: ISO string → HH:MM:SS.mmm ───────────────────────────────────────

class TestFmtTs:
    def test_iso_extracts_time(self):
        result = _fmt_ts("2026-05-30T14:58:52.035632Z")
        assert result == "14:58:52.035"

    def test_python_format_extracts_time(self):
        result = _fmt_ts("2026-05-30 09:19:33")
        assert result.startswith("09:19:33")

    def test_no_date_in_output(self):
        result = _fmt_ts("2026-05-30T14:58:52.035632Z")
        assert "2026" not in result
        assert "05" not in result or result.count(":") >= 2  # colons are from time

    def test_invalid_returns_fallback(self):
        result = _fmt_ts("garbage")
        assert isinstance(result, str)   # must not raise


# ── _smart_ts: epoch float → human-relative string ───────────────────────────

class TestSmartTs:
    """
    _smart_ts formats relative to the current local date:
        same day   → HH:MM:SS.mmm
        yesterday  → yesterday HH:MM:SS
        2–6 days   → DayName HH:MM:SS  (Mon, Tue, ...)
        7–364 days → Mon DD HH:MM:SS   (May 28 ...)
        365+ days  → YYYY-MM-DD HH:MM:SS
    """

    def _ago(self, days: float = 0, hours: float = 0) -> float:
        return time.time() - days * 86400 - hours * 3600

    # ── same day ──────────────────────────────────────────────────────────────

    def test_today_has_milliseconds(self):
        ts = _smart_ts(self._ago(hours=1))
        assert "." in ts
        ms_part = ts.split(".")[1]
        assert len(ms_part) == 3

    def test_today_no_date_words(self):
        ts = _smart_ts(self._ago(hours=2))
        for word in ("yesterday", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
                     "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"):
            assert word not in ts

    def test_today_format_length(self):
        ts = _smart_ts(self._ago(hours=0.5))
        # HH:MM:SS.mmm = exactly 12 chars
        assert len(ts) == 12

    # ── yesterday ────────────────────────────────────────────────────────────

    def test_yesterday_prefix(self):
        ts = _smart_ts(self._ago(days=1, hours=2))
        assert ts.startswith("yesterday ")

    def test_yesterday_contains_time(self):
        ts = _smart_ts(self._ago(days=1, hours=2))
        time_part = ts[len("yesterday "):]
        assert len(time_part) == 8   # HH:MM:SS

    # ── this week ─────────────────────────────────────────────────────────────

    def test_this_week_starts_with_day_abbrev(self):
        ts = _smart_ts(self._ago(days=3))
        days = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
        assert ts[:3] in days

    # ── same year, older ─────────────────────────────────────────────────────

    def test_same_year_contains_month_abbrev(self):
        ts = _smart_ts(self._ago(days=10))
        months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
        assert any(m in ts for m in months)

    # ── different year ───────────────────────────────────────────────────────

    def test_old_format_is_iso_date(self):
        ts = _smart_ts(self._ago(days=400))
        # YYYY-MM-DD HH:MM:SS — exactly 19 chars
        assert len(ts) == 19
        assert ts[4] == "-"
        assert ts[7] == "-"
        assert ts[10] == " "

    # ── robustness ───────────────────────────────────────────────────────────

    def test_always_returns_nonempty_string(self):
        offsets = [0, 3600, 86400, 86400 * 3, 86400 * 10, 86400 * 400]
        for offset in offsets:
            result = _smart_ts(time.time() - offset)
            assert isinstance(result, str)
            assert len(result) > 0

    def test_zero_epoch_does_not_crash(self):
        """Epoch 0 (1970-01-01) is a very old date — should format, not raise."""
        result = _smart_ts(0.0)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_future_epoch_does_not_crash(self):
        """A timestamp slightly in the future (clock skew) should not crash."""
        result = _smart_ts(time.time() + 60)
        assert isinstance(result, str)

    def test_consistency_same_epoch(self):
        """The same epoch called twice should give the same result."""
        epoch = time.time() - 3600
        assert _smart_ts(epoch) == _smart_ts(epoch)
