"""Tests for _fmt_countdown display logic."""
import pytest


class TestFmtCountdown:

    @pytest.fixture(autouse=True)
    def _setup(self, app):
        self.app = app

    def fmt(self, remaining: int) -> str:
        return self.app._fmt_countdown(remaining)

    def test_normal_2m30s(self):
        assert self.fmt(150) == "0230"

    def test_normal_1m00s(self):
        assert self.fmt(60) == "0100"

    def test_zero(self):
        assert self.fmt(0) == "0000"

    def test_seconds_only(self):
        assert self.fmt(59) == "0059"

    def test_five_minute_cap_value(self):
        assert self.fmt(300) == "0500"

    def test_large_remaining_minutes_clamped_at_99(self):
        # 6000s / 60 = 100 min — min(m, 99) should clamp so the string stays 4 chars
        result = self.app._fmt_countdown(6000)
        assert len(result) == 4
        assert int(result[:2]) <= 99
