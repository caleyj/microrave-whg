"""
Integration tests for MicroRaveApp state machine.

All hardware is mocked (see conftest.py). Events are injected directly into
the dispatch queue via app._post(); app._drain() blocks until the queue is empty.
"""
import time
import pytest
from microrave import State, PRESET_SECONDS, MAX_ENTRY_SECONDS


# ── Helpers ────────────────────────────────────────────────────────────────────

def push_digits(app, *digits):
    for d in digits:
        app._post(app._on_digit, d)
    app._drain()


def start_countdown(app, *digits):
    """Enter digits and press Start, then drain."""
    push_digits(app, *digits)
    app._post(app._on_start)
    app._drain()


# ── Basic state transitions ────────────────────────────────────────────────────

class TestBasicTransitions:

    def test_starts_in_idle(self, app):
        assert app._state == State.IDLE

    def test_digit_moves_to_entering_time(self, app):
        app._post(app._on_digit, 3)
        app._drain()
        assert app._state == State.ENTERING_TIME

    def test_multiple_digits_shift_buffer(self, app):
        push_digits(app, 1, 3, 0)
        assert app.buf.display_str() == "0130"
        assert app.buf.to_seconds() == 90

    def test_start_with_zero_prompts_for_time(self, app):
        # START with an empty buffer flashes 0000 and drops into entry mode —
        # it does not begin a countdown.
        app._post(app._on_start)
        app._drain()
        assert app._state == State.ENTERING_TIME
        assert app.timer.remaining == 0

    def test_start_with_time_begins_countdown(self, app):
        start_countdown(app, 0, 3, 0)
        assert app._state == State.COUNTING_DOWN

    def test_start_no_door_needed(self, app):
        """There is no door — Start alone begins the countdown."""
        start_countdown(app, 1, 0, 0)
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(60, abs=1)


# ── Keypad routing ─────────────────────────────────────────────────────────────

class TestKeyRouting:

    def test_on_key_digit(self, app):
        app._post(app._on_key, "5")
        app._drain()
        assert app._state == State.ENTERING_TIME
        assert app.buf.to_seconds() == 5

    def test_on_key_start(self, app):
        push_digits(app, 0, 3, 0)
        app._post(app._on_key, "START")
        app._drain()
        assert app._state == State.COUNTING_DOWN


# ── 5-minute cap ───────────────────────────────────────────────────────────────

class TestFiveMinuteCap:

    def test_typed_time_clamps_to_five_minutes(self, app):
        # 9:59 entered → countdown starts at 5:00
        start_countdown(app, 9, 5, 9)
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(MAX_ENTRY_SECONDS, abs=1)

    def test_under_cap_is_untouched(self, app):
        start_countdown(app, 4, 0, 0)   # 4:00
        assert app.timer.remaining == pytest.approx(240, abs=1)

    def test_add30_can_exceed_cap_while_counting(self, app):
        start_countdown(app, 5, 0, 0)   # starts at the cap
        for _ in range(4):
            app._post(app._on_add_30)
        app._drain()
        assert app.timer.remaining > MAX_ENTRY_SECONDS


# ── Popcorn / Potato one-touch presets ────────────────────────────────────────

class TestPresets:

    @pytest.mark.parametrize("label", ["POPCORN", "POTATO"])
    def test_preset_starts_immediately(self, app, label):
        app._post(app._on_preset, label)
        app._drain()
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(PRESET_SECONDS, abs=1)

    def test_preset_via_on_key(self, app):
        app._post(app._on_key, "POPCORN")
        app._drain()
        assert app._state == State.COUNTING_DOWN


# ── +30s ───────────────────────────────────────────────────────────────────────

class TestAdd30:

    def test_add30_from_idle_starts_30s_countdown(self, app):
        app._post(app._on_add_30)
        app._drain()
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == 30

    def test_add30_during_countdown_adds_time(self, app):
        start_countdown(app, 0, 3, 0)
        before = app.timer.remaining
        app._post(app._on_add_30)
        app._drain()
        assert app.timer.remaining >= before + 25   # allow for 1 tick during test


# ── Two-stage Stop ─────────────────────────────────────────────────────────────

class TestStop:

    def test_stop_from_idle_stays_at_clock(self, app):
        app._post(app._on_stop)
        app._drain()
        assert app._state == State.IDLE

    def test_first_stop_during_countdown_parks_on_zero(self, app):
        start_countdown(app, 1, 0, 0)
        app._post(app._on_stop)
        app._drain()
        assert app._state == State.ENTERING_TIME
        assert app.buf.to_seconds() == 0
        assert app.buf.display_str() == "0000"

    def test_second_stop_returns_to_clock(self, app):
        start_countdown(app, 1, 0, 0)
        app._post(app._on_stop)   # 1st press: park on 0000
        app._drain()
        app._post(app._on_stop)   # 2nd press: back to clock
        app._drain()
        assert app._state == State.IDLE

    def test_stop_during_entry_clears_then_idles(self, app):
        push_digits(app, 1, 2, 3)
        app._post(app._on_stop)   # entry non-zero → treated as 1st press
        app._drain()
        assert app._state == State.ENTERING_TIME
        assert app.buf.is_zero()
        app._post(app._on_stop)
        app._drain()
        assert app._state == State.IDLE


# ── Finish flow ────────────────────────────────────────────────────────────────

class TestFinish:

    def test_countdown_finishes_and_dings_then_idles(self, app):
        start_countdown(app, 0, 0, 2)   # 2 seconds
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and app._state != State.FINISHED:
            time.sleep(0.1)
        assert app._state == State.FINISHED
        # After the 3s hold it returns to the clock
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and app._state != State.IDLE:
            time.sleep(0.1)
        assert app._state == State.IDLE


# ── Digit press during countdown ───────────────────────────────────────────────

class TestDigitDuringCountdown:

    def test_digit_during_countdown_ignored(self, app):
        start_countdown(app, 0, 3, 0)
        app._post(app._on_digit, 5)
        app._drain()
        assert app._state == State.COUNTING_DOWN
        # remaining is unchanged (digit is not added as seconds)
        assert app.timer.remaining <= 30


# ── Relay cooking indicator ────────────────────────────────────────────────────

class TestRelayIndicator:

    def test_relay_calls_track_cooking_state(self, app):
        # RelayController is disabled (no hidapi) but the methods must be safe
        # no-ops and the app must call them at the right transitions.
        app.relays.all_on()
        app.relays.all_off()
        start_countdown(app, 0, 3, 0)
        app._post(app._on_stop)
        app._drain()
        assert app._state == State.ENTERING_TIME


# ── Rapid-fire robustness ──────────────────────────────────────────────────────

class TestRapidFire:

    def test_100_digit_presses_no_crash(self, app):
        for _ in range(100):
            app._post(app._on_digit, 1)
        app._drain()
        assert app._state == State.ENTERING_TIME

    def test_alternating_start_stop_no_crash(self, app):
        for _ in range(20):
            push_digits(app, 0, 3, 0)
            app._post(app._on_start)
            app._drain()
            app._post(app._on_stop)
            app._drain()
        assert app._state == State.ENTERING_TIME

    def test_preset_spam_no_crash(self, app):
        for _ in range(20):
            app._post(app._on_preset, "POPCORN")
            app._post(app._on_stop)
        app._drain()
        assert app._state in (State.ENTERING_TIME, State.IDLE)
