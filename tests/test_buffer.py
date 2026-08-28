"""Unit tests for TimeEntryBuffer."""
from microrave import TimeEntryBuffer


class TestTimeEntryBuffer:

    def setup_method(self):
        self.buf = TimeEntryBuffer()

    # ── Initial state ──────────────────────────────────────────────────────────

    def test_initial_is_zero(self):
        assert self.buf.to_seconds() == 0
        assert self.buf.is_zero()
        assert self.buf.display_str() == "0000"

    # ── Digit shifting ─────────────────────────────────────────────────────────

    def test_single_digit_lands_in_seconds(self):
        self.buf.push(5)
        assert self.buf.display_str() == "0005"
        assert self.buf.to_seconds() == 5

    def test_two_digits_shift_correctly(self):
        self.buf.push(3)
        self.buf.push(0)
        assert self.buf.display_str() == "0030"
        assert self.buf.to_seconds() == 30

    def test_three_digits_130(self):
        for d in [1, 3, 0]:
            self.buf.push(d)
        assert self.buf.display_str() == "0130"
        assert self.buf.to_seconds() == 90   # 1m30s

    def test_four_digits_fills_display(self):
        for d in [1, 2, 3, 0]:
            self.buf.push(d)
        assert self.buf.display_str() == "1230"
        assert self.buf.to_seconds() == 12 * 60 + 30

    # ── Overflow rejection ─────────────────────────────────────────────────────

    def test_overflow_beyond_9959_rejected(self):
        # [9,9,9] → _d=[0,9,9,9]  (9*60+99=639s — valid)
        for d in [9, 9, 9]:
            self.buf.push(d)
        assert self.buf.display_str() == "0999"
        # Push 9 → would shift to [9,9,9,9] = 99*60+99 = 6039s > 5999s — must reject
        self.buf.push(9)
        assert self.buf.display_str() == "0999"   # unchanged

    def test_zero_push_accepted_when_valid(self):
        self.buf.push(0)
        self.buf.push(3)
        self.buf.push(0)
        assert self.buf.to_seconds() == 30

    # ── Clear ──────────────────────────────────────────────────────────────────

    def test_clear_resets_all(self):
        for d in [1, 2, 3, 4]:
            self.buf.push(d)
        self.buf.clear()
        assert self.buf.to_seconds() == 0
        assert self.buf.display_str() == "0000"

    # ── Raw display (no normalisation) ────────────────────────────────────────

    def test_069_displays_raw_not_normalised(self):
        """Entering 0-6-9 must show 0069 on screen, not 1:09."""
        for d in [0, 6, 9]:
            self.buf.push(d)
        assert self.buf.display_str() == "0069"
        assert self.buf.to_seconds() == 69   # to_seconds() does normalise

    def test_raw_mm_and_ss(self):
        for d in [2, 3, 4, 5]:
            self.buf.push(d)
        assert self.buf.raw_mm() == 23
        assert self.buf.raw_ss() == 45

    # ── set_from_seconds / _from_add30 ────────────────────────────────────────

    def test_set_from_seconds_marks_from_add30(self):
        self.buf.set_from_seconds(30)
        assert self.buf.to_seconds() == 30
        assert self.buf.display_str() == "0030"
        assert self.buf._from_add30 is True

    def test_push_clears_from_add30_flag(self):
        self.buf.set_from_seconds(30)
        self.buf.push(5)
        assert self.buf._from_add30 is False

    def test_set_from_seconds_clamps_to_max(self):
        self.buf.set_from_seconds(999999)
        assert self.buf.to_seconds() == TimeEntryBuffer._MAX
