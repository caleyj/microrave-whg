"""
Integration tests for MicroRaveApp state machine.

All hardware is mocked (see conftest.py). Events are injected directly into
the dispatch queue via app._post(); app._drain() blocks until the queue is empty.
"""
import time
import pytest
import microrave
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


class TestKeypadMapping:
    """Function keys are plain letters — no keypad symbols. See microrave.py
    KEYPAD_MAP: a=Popcorn, b=Potato, c=+30s, d=Next track."""

    def test_letter_keys_map_to_functions(self):
        assert microrave.KEYPAD_MAP[microrave.pygame.K_a] == "POPCORN"
        assert microrave.KEYPAD_MAP[microrave.pygame.K_b] == "POTATO"
        assert microrave.KEYPAD_MAP[microrave.pygame.K_c] == "ADD30"
        assert microrave.KEYPAD_MAP[microrave.pygame.K_d] == "NEXTTRACK"

    def test_start_and_stop_keys(self):
        assert microrave.KEYPAD_MAP[microrave.pygame.K_RETURN] == "START"
        assert microrave.KEYPAD_MAP[microrave.pygame.K_BACKSPACE] == "STOP"

    def test_no_symbol_keys_mapped(self):
        for k in (microrave.pygame.K_KP_PLUS, microrave.pygame.K_KP_DIVIDE,
                  microrave.pygame.K_KP_MULTIPLY, microrave.pygame.K_KP_PERIOD):
            assert k not in microrave.KEYPAD_MAP


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
    # Isolate from whatever real files a developer may have dropped into the
    # repo's presets/ folder — these tests exercise the no-dedicated-track
    # fallback specifically.

    @pytest.mark.parametrize("label", ["POPCORN", "POTATO"])
    def test_preset_starts_immediately(self, app, monkeypatch, tmp_path, label):
        monkeypatch.setattr(microrave, "PRESET_DIR", str(tmp_path / "empty"))
        app._post(app._on_preset, label)
        app._drain()
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(PRESET_SECONDS, abs=1)

    def test_preset_via_on_key(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(microrave, "PRESET_DIR", str(tmp_path / "empty"))
        app._post(app._on_key, "POPCORN")
        app._drain()
        assert app._state == State.COUNTING_DOWN


class TestPresetTracks:
    """Popcorn/Potato play a dedicated file from PRESET_DIR once (no loop)
    and end the session the moment it finishes; missing file -> falls back
    to the shared shuffle for PRESET_SECONDS."""

    def test_dedicated_track_plays_once(self, app, monkeypatch, tmp_path):
        preset_dir = tmp_path / "presets"
        preset_dir.mkdir()
        track = preset_dir / "popcorn.mp3"
        track.write_bytes(b"\x00")
        monkeypatch.setattr(microrave, "PRESET_DIR", str(preset_dir))

        calls = []
        monkeypatch.setattr(app.audio, "start",
                            lambda tp, on_complete=None: calls.append((tp, on_complete)))
        app._post(app._on_preset, "POPCORN")
        app._drain()

        assert app._state == State.COUNTING_DOWN
        assert len(calls) == 1
        provider, on_complete = calls[0]
        # Returns the track once, then None — no loop.
        assert provider() == str(track)
        assert provider() is None
        assert on_complete is not None

    def test_track_finishing_ends_the_session_immediately(self, app, monkeypatch, tmp_path):
        preset_dir = tmp_path / "presets"
        preset_dir.mkdir()
        (preset_dir / "potato.mp3").write_bytes(b"\x00")
        monkeypatch.setattr(microrave, "PRESET_DIR", str(preset_dir))

        app._post(app._on_preset, "POTATO")
        app._drain()
        assert app._state == State.COUNTING_DOWN

        # Simulate AudioEngine's on_complete firing when the track ends naturally.
        app._post(app._on_preset_track_done)
        app._drain()
        assert app._state == State.FINISHED

    def test_stop_prevents_a_stale_completion_from_resurrecting_the_session(
            self, app, monkeypatch, tmp_path):
        preset_dir = tmp_path / "presets"
        preset_dir.mkdir()
        (preset_dir / "popcorn.mp3").write_bytes(b"\x00")
        monkeypatch.setattr(microrave, "PRESET_DIR", str(preset_dir))

        app._post(app._on_preset, "POPCORN")
        app._drain()
        app._post(app._on_stop)   # 1st press: cancel
        app._drain()
        assert app._state == State.ENTERING_TIME

        app._post(app._on_preset_track_done)   # a late on_complete signal
        app._drain()
        assert app._state == State.ENTERING_TIME   # unchanged — no resurrection

    def test_missing_track_falls_back_to_shared_playlist(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(microrave, "PRESET_DIR", str(tmp_path / "no-such-dir"))

        calls = []
        monkeypatch.setattr(app.audio, "start", lambda tp, on_complete=None: calls.append(tp))
        app._post(app._on_preset, "POTATO")
        app._drain()

        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(PRESET_SECONDS, abs=1)
        assert len(calls) == 1
        assert calls[0] == app.playlists.next_track


# ── Next track ─────────────────────────────────────────────────────────────────

class TestNextTrack:
    """Next Track only does anything while counting down, and never touches
    the timer — it just asks AudioEngine to advance the shared shuffle."""

    def test_noop_outside_countdown(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(app.audio, "start", lambda tp, on_complete=None: calls.append(tp))
        app._post(app._on_next_track)
        app._drain()
        assert calls == []
        assert app._state == State.IDLE

    def test_advances_during_countdown_without_touching_timer(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(app.audio, "start", lambda tp, on_complete=None: calls.append(tp))
        start_countdown(app, 0, 3, 0)
        assert len(calls) == 1   # _begin_countdown's own audio.start
        remaining_before = app.timer.remaining

        app._post(app._on_next_track)
        app._drain()

        assert len(calls) == 2
        assert calls[1] == app.playlists.next_track
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(remaining_before, abs=1)

    def test_via_on_key(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(app.audio, "start", lambda tp, on_complete=None: calls.append(tp))
        start_countdown(app, 0, 3, 0)
        app._post(app._on_key, "NEXTTRACK")
        app._drain()
        assert len(calls) == 2

    def test_noop_during_preset_session(self, app, monkeypatch, tmp_path):
        # There's only one dedicated track in a Popcorn/Potato session — no
        # "next" to skip to — so Next Track must do nothing there.
        preset_dir = tmp_path / "presets"
        preset_dir.mkdir()
        (preset_dir / "popcorn.mp3").write_bytes(b"\x00")
        monkeypatch.setattr(microrave, "PRESET_DIR", str(preset_dir))

        calls = []
        monkeypatch.setattr(app.audio, "start", lambda tp, on_complete=None: calls.append(tp))
        app._post(app._on_preset, "POPCORN")
        app._drain()
        assert len(calls) == 1   # the preset's own start() call
        remaining_before = app.timer.remaining

        app._post(app._on_next_track)
        app._drain()

        assert len(calls) == 1   # unchanged — next track was ignored
        assert app._state == State.COUNTING_DOWN
        assert app.timer.remaining == pytest.approx(remaining_before, abs=1)


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

    def test_add30_updates_display_immediately(self, app):
        # The CountdownTimer's own tick can be up to 1s away — the display
        # must reflect the new remaining time right away, not wait for it.
        start_countdown(app, 0, 1, 0)   # 1:00
        app._post(app._on_add_30)
        app._drain()
        assert app.display._text == app._fmt_countdown(app.timer.remaining)


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

    def test_preset_spam_no_crash(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(microrave, "PRESET_DIR", str(tmp_path / "empty"))
        for _ in range(20):
            app._post(app._on_preset, "POPCORN")
            app._post(app._on_stop)
        app._drain()
        assert app._state in (State.ENTERING_TIME, State.IDLE)


# ── Idle clock ─────────────────────────────────────────────────────────────────

class TestIdleClock:
    """24-hour format: always two real digits for the hour, so there's no
    blank leading digit for the DSEG7 ghost segments to show through."""

    def test_single_digit_hour_is_zero_padded(self, app, monkeypatch):
        import datetime as real_datetime

        class _FixedDateTime:
            @staticmethod
            def now():
                return real_datetime.datetime(2026, 1, 1, 5, 7)   # 05:07

        monkeypatch.setattr(microrave, "datetime", _FixedDateTime)
        app._last_clock = None
        app._show_clock(force=True)
        assert app.display._text == "0507"

    def test_afternoon_hour_stays_24_hour(self, app, monkeypatch):
        import datetime as real_datetime

        class _FixedDateTime:
            @staticmethod
            def now():
                return real_datetime.datetime(2026, 1, 1, 23, 45)   # 23:45

        monkeypatch.setattr(microrave, "datetime", _FixedDateTime)
        app._last_clock = None
        app._show_clock(force=True)
        assert app.display._text == "2345"

    def test_colon_blinks_with_seconds(self, app, monkeypatch):
        import datetime as real_datetime

        def _at(second):
            class _FixedDateTime:
                @staticmethod
                def now():
                    return real_datetime.datetime(2026, 1, 1, 10, 30, second)
            return _FixedDateTime

        monkeypatch.setattr(microrave, "datetime", _at(0))
        app._last_clock = None
        app._show_clock()
        assert app.display._colon is True    # even second -> on

        # Same minute, second parity flips: must redraw even without force=True.
        monkeypatch.setattr(microrave, "datetime", _at(1))
        app._show_clock()
        assert app.display._colon is False   # odd second -> off (down to the ghost dots)

        monkeypatch.setattr(microrave, "datetime", _at(2))
        app._show_clock()
        assert app.display._colon is True
