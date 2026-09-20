"""
MicroRave Music Player  —  our build
====================================
Microwave-shell countdown music-box on Raspberry Pi 5.

Hardware:
  USB keypad (HID keyboard) — all input, no GPIO buttons
  HDMI display — fullscreen virtual 7-segment clock face via pygame
  HDMI audio → TV speakers
  dcttech USB-HID relay board(s) (16c0:05df) — "cooking" indicator lamp

Behavior: microwave-oven UX (no door)
  Idle          Shows 24-hour clock, colon blinks on the half-second
  Digit press   Enters countdown time (shifts in from right)
  Start         Begins countdown + music (shared shuffled playlist)
  Popcorn       One-touch: plays presets/popcorn.* once, then finishes
  Potato        One-touch: plays presets/potato.* once, then finishes
  Next track    Skips to the next shuffled track — shared-shuffle countdowns
                only; no-op during Popcorn/Potato (nothing to skip to) or
                outside a countdown. Never touches the timer.
  +30s          Adds 30 seconds during entry or a normal countdown (may
                exceed the 5:00 cap while running); no-op during Popcorn/
                Potato (a curated track's length isn't meant to be adjusted)
  Stop          1st press cancels + parks on 0000; 2nd press returns to the clock
  (countdown)   Ends only when it reaches 0:00 → microwave "ding"

Typed times are clamped to MAX_ENTRY_SECONDS (5:00) when the countdown starts.
While a countdown runs, all relay channels are ON; they switch OFF on
finish / stop / idle.

Run from desktop terminal:
  sudo venv/bin/python microrave.py

Run headless (no desktop session):
  sudo SDL_VIDEODRIVER=kmsdrm venv/bin/python microrave.py
"""

from __future__ import annotations

import json
import logging
import os
import queue
import random
import subprocess
import threading
import time
from datetime import datetime
from enum import Enum, auto

try:
    import hid as _hid
    _HID_AVAILABLE = True
except ImportError:
    _HID_AVAILABLE = False

import pygame

# =============================================================================
# LOGGING
# =============================================================================

_temp_cache: dict = {"val": "?°C", "ts": 0.0}

def _read_temp() -> str:
    now = time.monotonic()
    if now - _temp_cache["ts"] >= 30.0:
        try:
            out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True)
            # "temp=52.3'C" → "52.3°C"
            _temp_cache["val"] = out.strip().replace("temp=", "").replace("'C", "°C")
        except Exception:
            _temp_cache["val"] = "?°C"
        _temp_cache["ts"] = now
    return _temp_cache["val"]

class _TempFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.temp = _read_temp()
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s [%(temp)s]: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("microrave.log"),
    ],
)
_temp_filter = _TempFilter()
for _h in logging.getLogger().handlers:
    _h.addFilter(_temp_filter)

log = logging.getLogger("MicroRave")

# =============================================================================
# USB KEYPAD MAP  (edit to match your keypad)
# =============================================================================
#
# All input comes from a USB keypad that acts as a USB keyboard. pygame
# delivers each keypress as a KEYDOWN event; the key constant is looked up here
# and turned into one of these logical labels:
#
#   "0".."9"    digit entry
#   "START"     begin countdown / (with empty buffer) flash prompt
#   "STOP"      1st press cancel+park, 2nd press back to clock
#   "ADD30"     add 30 seconds
#   "POPCORN"   one-touch preset — plays presets/popcorn.* once
#   "POTATO"    one-touch preset — plays presets/potato.* once
#   "NEXTTRACK" skip to the next shared-shuffle track (no-op during a
#               Popcorn/Potato session, or outside a countdown)
#
# Function keys are plain letters (no keypad symbols like +, /, *). Digits
# 0-9 and the numpad equivalents both work; Return starts, Backspace stops.
# Edit the pygame.K_x on the left to move a function to a different key.

KEYPAD_MAP = {
    pygame.K_0: "0", pygame.K_1: "1", pygame.K_2: "2", pygame.K_3: "3",
    pygame.K_4: "4", pygame.K_5: "5", pygame.K_6: "6", pygame.K_7: "7",
    pygame.K_8: "8", pygame.K_9: "9",
    pygame.K_KP0: "0", pygame.K_KP1: "1", pygame.K_KP2: "2", pygame.K_KP3: "3",
    pygame.K_KP4: "4", pygame.K_KP5: "5", pygame.K_KP6: "6", pygame.K_KP7: "7",
    pygame.K_KP8: "8", pygame.K_KP9: "9",

    pygame.K_RETURN:    "START",
    pygame.K_KP_ENTER:  "START",
    pygame.K_BACKSPACE: "STOP",

    pygame.K_a: "POPCORN",
    pygame.K_b: "POTATO",
    pygame.K_c: "ADD30",
    pygame.K_d: "NEXTTRACK",
}

# =============================================================================
# SETTINGS
# =============================================================================

# dcttech USB-HID relay board — "cooking" indicator lamp
RELAY_VID = 0x16c0
RELAY_PID = 0x05df

MUSIC_ROOT        = "music"          # single shared playlist folder
SOUNDS_DIR        = "sounds"
PLAYCOUNTS_FILE   = "playcounts.json"
BEEP_SOUND        = os.path.join(SOUNDS_DIR, "beep.mp3")
DING_SOUND        = os.path.join(SOUNDS_DIR, "ding.mp3")
VOLUME_DEFAULT    = 70    # 0–100

PRESET_SECONDS      = 180   # Popcorn / Potato one-touch time
MAX_ENTRY_SECONDS   = 300   # typed time is clamped to this when the countdown starts
ENTRY_IDLE_TIMEOUT  = 60    # seconds on 0000 screen with no input before returning to clock

# Popcorn / Potato dedicated tracks. Drop a file named "popcorn" and "potato"
# (any extension from Playlist._EXTS) into PRESET_DIR — it plays on a loop for
# the whole preset countdown instead of pulling from the shared playlist.
# Missing file -> falls back to the shared shuffle (logged as a warning).
PRESET_DIR = "presets"

# =============================================================================
# DISPLAY  —  DSEG7 "real 7-segment" font, green on black
# =============================================================================

FONT_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fonts", "DSEG7Classic-Bold.ttf")

COLOR_BG   = (  0,   0,   0)   # black background
COLOR_ON   = ( 74, 255, 106)  # lit segments — soft green
COLOR_GLOW = ( 20, 130,  50)  # halo bloomed around the digits (kept dim)
COLOR_DIM  = (  0,  22,   8)  # unlit "ghost" segments behind the value (~4%)
# For a classic amber readout:  COLOR_ON = (255,185,55)  COLOR_GLOW = (120,70,10)  COLOR_DIM = (26,15,0)

SHOW_DIM_SEGS = True   # faint 88:88 behind the value (authentic look); False = value only
GLOW_ENABLED  = True   # soft bloom around the lit digits
GLOW_STACK    = 2      # RGB-add passes of the blurred halo (0 disables, higher = brighter)
GLOW_SPREAD   = 5      # blur radius: halo scaled to 1/N and back (higher = softer/wider)

DISPLAY_FILL  = 0.86   # fraction of the screen the "88:88" readout spans


# =============================================================================
# DISPLAY
# =============================================================================

class Display:
    """
    Fullscreen pygame window showing MM:SS as a green 7-segment readout in the
    DSEG7 Classic font. A faint "88:88" ghost sits behind the value and a soft
    bloom is drawn around the lit digits.

    Every glyph this display can ever show — '0'-'9', a blank digit, and the
    colon on/off — is pre-rendered (ghost + blurred halo + crisp glyph) once
    at startup into a fixed cell. render() just blits up to 5 of these cells
    side by side. (An earlier version cached by the whole 5-character string
    instead of by glyph — harmless for the idle clock, which only shows a
    couple of distinct minutes at a time, but during any countdown the string
    changes every second, so it was a 100% cache-miss doing a full font
    render + blur on the main thread on every single tick — the actual cause
    of the "main loop stall" warnings.)

    Thread safety:
      show()   — safe from any thread (queues a pending update)
      render() — must be called from the main thread only
    """

    _GHOST = "88:88"

    def __init__(self):
        info = pygame.display.Info()
        self._sw, self._sh = info.current_w, info.current_h

        self._screen = pygame.display.set_mode(
            (self._sw, self._sh), pygame.FULLSCREEN | pygame.NOFRAME
        )
        pygame.display.set_caption("MicroRave")
        pygame.mouse.set_visible(False)

        # Hold exclusive keyboard focus so USB-keypad presses don't leak to the
        # desktop/console behind the fullscreen window. No-op under kmsdrm.
        try:
            pygame.event.set_grab(True)
        except pygame.error:
            pass

        pygame.font.init()
        self._font = self._fit_font(int(self._sw * DISPLAY_FILL), int(self._sh * DISPLAY_FILL))
        self._pad  = max(8, self._font.get_height() // 8) if GLOW_ENABLED else 2

        # DSEG7 is fixed-pitch — every digit is the same width, and so is the
        # colon (confirmed: sum of per-glyph widths == width of the whole
        # string, no kerning) — so pre-rendered cells can be blitted side by
        # side and it's pixel-identical to rendering the whole string.
        self._digit_w, self._cell_h = self._font.size('0')
        self._colon_w = self._font.size(':')[0]
        content_w = 4 * self._digit_w + self._colon_w
        self._ox = (self._sw - content_w) // 2
        self._oy = (self._sh - self._cell_h) // 2

        # Every glyph this display can ever need, pre-rendered once (ghost +
        # blurred halo + crisp glyph baked into one cell) — see the class
        # docstring for why this replaced whole-string-per-tick rendering.
        self._digit_cell = {ch: self._build_glyph(ch, '8', self._digit_w) for ch in '0123456789'}
        self._digit_cell[' '] = self._build_glyph(' ', '8', self._digit_w)
        self._colon_cell = {
            True:  self._build_glyph(':', ':', self._colon_w),
            False: self._build_glyph(' ', ':', self._colon_w),
        }

        self._lock  = threading.Lock()
        self._text  = "    "
        self._colon = False
        self._dirty = True

        log.info("Display ready: %dx%d  font=%dpx", self._sw, self._sh, self._font.get_height())

    # ------------------------------------------------------------------

    def _fit_font(self, max_w: int, max_h: int) -> "pygame.font.Font":
        """Largest DSEG7 size whose '88:88' fits max_w × max_h. Falls back to the
        default pygame font if the DSEG file is missing."""
        try:
            pygame.font.Font(FONT_PATH, 10)
            path = FONT_PATH
        except Exception as exc:
            log.warning("DSEG7 font not found (%s) — using default font.", exc)
            path = None
        size = max(10, max_h)
        for _ in range(12):
            f = pygame.font.Font(path, size)
            w, h = f.size(self._GHOST)
            if w <= max_w and h <= max_h:
                return f
            size = max(10, int(size * min(max_w / w, max_h / h)) - 1)
        return pygame.font.Font(path, size)

    def _build_glyph(self, bright_ch: str, ghost_ch: str, cell_w: int) -> "pygame.Surface":
        """One pre-rendered cell: a dim ghost glyph (e.g. always '8' for a
        digit slot, so unlit segments show faintly), then — if bright_ch is
        a real character, not a blank — a blurred halo and the crisp glyph
        on top. Built once at startup; render() only ever blits these.

        Cells are wider than their glyph (padded so the glow has room to
        bleed past it) and, with no natural gap between characters in this
        font, sit edge to edge — so each cell's padding overlaps its
        neighbour's. COLOR_BG is colour-keyed transparent so that overlap
        never overwrites the neighbour's own glow bleed with black (the
        colon cell in particular is narrower than the padding itself)."""
        pad = self._pad
        size = (cell_w + 2 * pad, self._cell_h + 2 * pad)
        surf = pygame.Surface(size)
        surf.fill(COLOR_BG)

        if SHOW_DIM_SEGS:
            surf.blit(self._font.render(ghost_ch, True, COLOR_DIM), (pad, pad))

        if bright_ch != ' ':
            if GLOW_ENABLED and GLOW_STACK > 0:
                glow = pygame.Surface(size)
                glow.fill(COLOR_BG)
                glow.blit(self._font.render(bright_ch, True, COLOR_GLOW), (pad, pad))
                n = max(2, GLOW_SPREAD)
                glow = pygame.transform.smoothscale(glow, (max(1, size[0] // n), max(1, size[1] // n)))
                glow = pygame.transform.smoothscale(glow, size)
                for _ in range(GLOW_STACK):
                    surf.blit(glow, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
            surf.blit(self._font.render(bright_ch, True, COLOR_ON), (pad, pad))

        surf.set_colorkey(COLOR_BG)
        return surf

    def show(self, text: str, colon: bool = True):
        """Queue a display update (thread-safe). text is up to 4 chars, MMSS."""
        clean = text.replace(":", "").replace(".", "")[:4].ljust(4)
        with self._lock:
            self._text  = clean
            self._colon = colon
            self._dirty = True

    def show_segs(self, segs, colon: bool = False):
        """Retained for API compatibility — segment-set rendering is unused."""
        pass

    def render(self):
        """Flush pending update to screen. Call from the main thread only.
        Just blits up to 5 pre-built cells — no font rendering or blurring
        happens here, so this is cheap regardless of how often the value
        changes (every tick, during a countdown)."""
        with self._lock:
            if not self._dirty:
                return
            text, colon = self._text, self._colon
            self._dirty = False

        self._screen.fill(COLOR_BG)
        pad = self._pad
        x, y = self._ox, self._oy
        for i, ch in enumerate(text):
            if i == 2:
                self._screen.blit(self._colon_cell[colon], (x - pad, y - pad))
                x += self._colon_w
            self._screen.blit(self._digit_cell.get(ch, self._digit_cell[' ']), (x - pad, y - pad))
            x += self._digit_w
        pygame.display.flip()


# =============================================================================
# PLAYLIST
# =============================================================================

class Playlist:
    """Single shared playlist, bag-shuffled.

    All audio files under MUSIC_ROOT (recursively) form one pool. Each track
    plays once per bag before any repeat; the bag survives across countdowns so
    short sessions still rotate through the whole folder. At a bag boundary the
    first track is swapped with the second if it would repeat the just-played
    one, preventing back-to-back duplicates.

    Every candidate is test-loaded through the mixer once here, at startup,
    and dropped if it fails — better to pay for a slow/bad file once at boot
    (logged, so it's obvious which file to fix) than to hit that same cost
    live mid-countdown, which for a badly-encoded file can be a genuine
    multi-hundred-ms blocking call and a real source of main-loop stalls.
    Skipped if the mixer never came up (no audio driver found) — see
    AudioEngine, which must exist before this class does.
    """

    _EXTS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a')

    def __init__(self):
        candidates: list[str] = []
        if os.path.isdir(MUSIC_ROOT):
            for root, _dirs, files in os.walk(MUSIC_ROOT):
                for f in files:
                    if f.lower().endswith(self._EXTS):
                        candidates.append(os.path.join(root, f))
            candidates.sort()

        self._tracks: list[str] = []
        skipped = 0
        can_validate = pygame.mixer.get_init() is not None
        for path in candidates:
            if can_validate:
                try:
                    pygame.mixer.music.load(path)
                except Exception as exc:
                    log.error("Playlist: unplayable file excluded — %s (%s)", path, exc)
                    skipped += 1
                    continue
            self._tracks.append(path)

        if self._tracks:
            log.info("Playlist: %d track(s) under %s/", len(self._tracks), MUSIC_ROOT)
            if skipped:
                log.warning("Playlist: %d file(s) excluded as unplayable — see above", skipped)
        else:
            log.warning("Playlist empty — no playable audio files under %s/", MUSIC_ROOT)
        self._bag: list[str] = []
        self._last: str | None = None

    def next_track(self) -> str | None:
        if not self._tracks:
            return None
        if not self._bag:
            new_bag = list(self._tracks)
            random.shuffle(new_bag)
            if len(new_bag) > 1 and new_bag[0] == self._last:
                new_bag[0], new_bag[1] = new_bag[1], new_bag[0]
            self._bag = new_bag
            log.info("Playlist bag refilled (%d tracks)", len(new_bag))
        track = self._bag.pop(0)
        self._last = track
        log.info("Next: %s (%d left in bag)", os.path.basename(track), len(self._bag))
        return track


def _find_preset_track(name: str) -> str | None:
    """Look for presets/<name>.<ext> (any Playlist._EXTS extension). None if missing."""
    for ext in Playlist._EXTS:
        path = os.path.join(PRESET_DIR, name + ext)
        if os.path.isfile(path):
            return path
    return None


def _single_play_provider(path: str):
    """Track-provider that returns path once, then None forever after — so
    AudioEngine's track manager plays it exactly once and stops."""
    it = iter((path,))
    return lambda: next(it, None)


def _probe_track_seconds(path: str, default: int) -> int:
    """Measured duration of an audio file, rounded up with a 1s safety margin
    so the cosmetic countdown never reaches zero before the track actually
    finishes. Falls back to `default` if the file can't be measured."""
    try:
        return int(pygame.mixer.Sound(path).get_length()) + 1
    except Exception as exc:
        log.warning("Could not measure %s (%s) — using %ds.", os.path.basename(path), exc, default)
        return default


# =============================================================================
# AUDIO ENGINE
# =============================================================================

class AudioEngine:
    _MUSIC_END = pygame.USEREVENT + 1

    # If a track fails to play, retry after a short pause rather than
    # hammering the next one immediately — and give up after enough failures
    # in a row rather than spinning forever (e.g. a flaky SD card / USB drive
    # taking out a whole run of files at once). Playlist already test-loads
    # every file at startup, so in normal operation this path shouldn't fire
    # at all; it's a safety net for failures that only show up at runtime.
    _MAX_CONSECUTIVE_FAILURES = 5
    _RETRY_BACKOFF_S = 0.5

    def __init__(self):
        self._ok      = False
        self._volume  = VOLUME_DEFAULT
        self._provider = None        # callable() -> next track path, or None to stop
        self._on_complete = None     # callable(), fired once when the provider is exhausted
        self._playing = False
        self._paused  = False
        self._failures = 0           # consecutive _play() failures
        self._lock    = threading.Lock()
        self._done    = threading.Event()
        self._counts  = self._load_counts()
        self._save_counter = 0

        for driver in ("pipewire", "pulseaudio", "alsa", "dummy"):
            os.environ["SDL_AUDIODRIVER"] = driver
            try:
                pygame.mixer.quit()
                pygame.mixer.pre_init(44100, -16, 2,4096)
                pygame.mixer.init()
                log.info("Audio driver: %s", driver)
                self._ok = True
                break
            except Exception as exc:
                log.warning("Audio driver '%s' failed: %s", driver, exc)

        if not self._ok:
            log.error("All audio drivers failed — silent mode.")
            return

        pygame.mixer.set_num_channels(8)
        pygame.mixer.music.set_endevent(self._MUSIC_END)
        self._beep_ch  = pygame.mixer.Channel(7)
        self._ding_ch  = pygame.mixer.Channel(6)
        self._beep_snd = self._load(BEEP_SOUND, "beep")
        self._ding_snd = self._load(DING_SOUND, "ding")
        self._apply_volume()

        threading.Thread(target=self._track_manager, name="TrackManager", daemon=True).start()
        log.info("Audio ready (volume=%d%%)", self._volume)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, track_provider, on_complete=None):
        """Start playlist playback.
        track_provider() is called once now (first track) and again on every
        track-end (next track). It returns None to stop playback. This lets
        Playlist bag-shuffle on demand, or a fixed single track play once and
        stop (see _single_play_provider).
        on_complete(), if given, fires once — from the track-manager thread —
        when the provider naturally runs out. It does NOT fire on an explicit
        stop()."""
        if not self._ok or not callable(track_provider):
            return
        first = track_provider()
        if not first:
            return
        with self._lock:
            self._provider     = track_provider
            self._on_complete  = on_complete
            self._playing      = True
            self._paused       = False
        self._failures = 0
        if not self._play(first):
            self._failures = 1
            self._done.set()   # hand off to _track_manager's retry/backoff

    def pause(self):
        if not self._ok:
            return
        with self._lock:
            if not self._playing or self._paused:
                return
            self._paused = True
        pygame.mixer.music.pause()

    def resume(self):
        if not self._ok:
            return
        with self._lock:
            if not self._playing or not self._paused:
                return
            self._paused = False
        pygame.mixer.music.unpause()

    def stop(self):
        with self._lock:
            self._playing     = False
            self._paused      = False
            self._on_complete = None
        if self._ok:
            pygame.mixer.music.stop()

    def beep(self):
        if self._ok and self._beep_snd and self._beep_ch:
            self._beep_ch.stop()
            self._beep_ch.play(self._beep_snd)

    def ding(self):
        self.stop()
        if self._ok and self._ding_snd and self._ding_ch:
            self._ding_ch.play(self._ding_snd)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: str, label: str):
        try:
            snd = pygame.mixer.Sound(path)
            log.info("Loaded %s: %s", label, path)
            return snd
        except Exception as exc:
            log.error("Cannot load %s '%s': %s", label, path, exc)
            return None

    def _load_counts(self) -> dict:
        try:
            with open(PLAYCOUNTS_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_counts(self):
        try:
            with open(PLAYCOUNTS_FILE, 'w') as f:
                json.dump(self._counts, f, indent=2, sort_keys=True)
        except Exception as exc:
            log.warning("Could not save play counts: %s", exc)

    def _play(self, path: str) -> bool:
        """Attempt to play path. Returns False (and logs) on failure — the
        caller decides whether/how to retry; this never blocks on _done
        itself, so it's safe to call from any thread."""
        key = os.path.basename(path)
        self._counts[key] = self._counts.get(key, 0) + 1
        self._save_counter += 1
        if self._save_counter >= 5:
            self._save_counts()
            self._save_counter = 0
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(self._volume / 100)
            pygame.mixer.music.play()
            log.info("Playing: %s (play #%d)", key, self._counts[key])
            return True
        except Exception as exc:
            log.error("Cannot play '%s': %s", path, exc)
            return False

    def notify_music_end(self):
        """Signal that the current track ended. Called from the main thread's event loop."""
        self._done.set()

    def _track_manager(self):
        """Advance the playlist when a track ends, or fire on_complete once
        the provider is naturally exhausted (not on an explicit stop()).

        A failed _play() retries after _RETRY_BACKOFF_S (off this thread —
        never the main loop) instead of hammering the next track immediately,
        and gives up after _MAX_CONSECUTIVE_FAILURES in a row rather than
        spinning forever if, say, a whole run of files turns out to be bad."""
        while True:
            self._done.wait()
            self._done.clear()
            cb = None
            with self._lock:
                if not self._playing or self._paused:
                    continue
                nxt = self._provider() if self._provider else None
                if not nxt:
                    self._playing = False
                    cb = self._on_complete
                    self._on_complete = None
            if cb:
                self._failures = 0
                cb()
            elif nxt:
                if self._play(nxt):
                    self._failures = 0
                else:
                    self._failures += 1
                    if self._failures >= self._MAX_CONSECUTIVE_FAILURES:
                        log.error("AudioEngine: %d tracks in a row failed to play — "
                                  "stopping playback for this session.", self._failures)
                        with self._lock:
                            self._playing = False
                        self._failures = 0
                    else:
                        time.sleep(self._RETRY_BACKOFF_S)
                        self._done.set()

    def shutdown(self):
        """Flush any unsaved play counts — called by app on exit."""
        self._save_counts()

    def _apply_volume(self):
        if self._ok:
            pygame.mixer.music.set_volume(self._volume / 100)


# =============================================================================
# COUNTDOWN TIMER
# =============================================================================

class CountdownTimer:
    """
    Accurate 1-second countdown.
    on_tick(remaining) fires every second.
    on_finish() fires when remaining reaches zero.
    Both callbacks come from the timer thread — callers should post to a queue.
    """

    def __init__(self, on_tick, on_finish):
        self._on_tick   = on_tick
        self._on_finish = on_finish
        self._remaining = 0
        self._lock      = threading.Lock()
        self._stop      = threading.Event()
        self._pause     = threading.Event()
        self._pause.set()
        self._thread: threading.Thread | None = None

    def start(self, seconds: int):
        self.stop()
        with self._lock:
            self._remaining = max(0, seconds)
        self._stop.clear()
        self._pause.set()
        self._thread = threading.Thread(target=self._run, name="Countdown", daemon=True)
        self._thread.start()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    def stop(self):
        self._stop.set()
        self._pause.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def add(self, n: int):
        with self._lock:
            self._remaining = max(0, self._remaining + n)
        log.info("Timer +%ds → %d remaining", n, self._remaining)

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    def _run(self):
        while not self._stop.is_set():
            self._pause.wait()
            if self._stop.is_set():
                break
            with self._lock:
                r = self._remaining
            self._on_tick(r)
            if r <= 0:
                self._on_finish()
                return
            self._stop.wait(timeout=1.0)
            if not self._stop.is_set():
                with self._lock:
                    self._remaining = max(0, self._remaining - 1)


# =============================================================================
# TIME ENTRY BUFFER
# =============================================================================

class TimeEntryBuffer:
    """
    Accumulates digit presses into MM:SS time.
    Digits shift in from the right, exactly like a real microwave keypad.
    Rejects entries that would exceed 99:59 (the countdown itself is later
    clamped to MAX_ENTRY_SECONDS when it starts).
    """

    _MAX = 99 * 60 + 59

    def __init__(self):
        self._d         = [0, 0, 0, 0]
        self._from_add30 = False      # True when buffer was last set by +30 (not manual digits)

    def push(self, digit: int):
        c = self._d[1:] + [digit]
        if (c[0] * 10 + c[1]) * 60 + (c[2] * 10 + c[3]) <= self._MAX:
            self._d          = c
            self._from_add30 = False

    def clear(self):
        self._d          = [0, 0, 0, 0]
        self._from_add30 = False

    def to_seconds(self) -> int:
        return (self._d[0] * 10 + self._d[1]) * 60 + (self._d[2] * 10 + self._d[3])

    def is_zero(self) -> bool:
        return self.to_seconds() == 0

    def raw_mm(self) -> int:
        return self._d[0] * 10 + self._d[1]

    def raw_ss(self) -> int:
        return self._d[2] * 10 + self._d[3]

    def set_from_seconds(self, secs: int):
        """Overwrite buffer from a seconds value — used by +30 and the presets."""
        secs = max(0, min(secs, self._MAX))
        m, s = divmod(secs, 60)
        self._d          = [m // 10, m % 10, s // 10, s % 10]
        self._from_add30 = True

    def display_str(self) -> str:
        """4-char string for the display (raw digits, no normalization)."""
        return "%d%d%d%d" % tuple(self._d)


# =============================================================================
# APPLICATION STATE
# =============================================================================

class State(Enum):
    IDLE          = auto()
    ENTERING_TIME = auto()
    COUNTING_DOWN = auto()
    FINISHED      = auto()


# Sentinel posted to the dispatch queue to signal a clean shutdown
_STOP_SENTINEL = object()


# =============================================================================
# RELAY CONTROLLER  (dcttech USB-HID relay board — "cooking" lamp)
# =============================================================================

class RelayController:
    """Drives dcttech USB-HID relay board(s) (VID 16c0:05df) as a "cooking"
    indicator: all channels ON while a countdown runs, OFF otherwise.

    Opens every matching board that is plugged in. Gracefully disabled if the
    hidapi package or the hardware is missing.

    Protocol (pavel-a/usb-relay-hid): a 9-byte HID feature report
    [reportId=0, cmd, channel, 0*6] where cmd is 0xFE all-on / 0xFC all-off /
    0xFF one-on / 0xFD one-off and channel is 1-based.
    """

    _ALL_ON  = 0xFE
    _ALL_OFF = 0xFC

    def __init__(self):
        self._lock = threading.Lock()
        self._devs: list = []
        if not _HID_AVAILABLE:
            log.warning("hidapi not installed — USB relay disabled.")
            return
        try:
            for info in _hid.enumerate(RELAY_VID, RELAY_PID):
                dev = _hid.device()
                dev.open_path(info["path"])
                self._devs.append(dev)
                log.info("USB relay ready: %s serial=%s",
                         info.get("product_string"), info.get("serial_number"))
            if not self._devs:
                log.warning("No USB relay board found (%04x:%04x).", RELAY_VID, RELAY_PID)
        except Exception as exc:
            log.warning("USB relay init failed: %s", exc)

    def all_on(self) -> None:
        self._send(self._ALL_ON)

    def all_off(self) -> None:
        self._send(self._ALL_OFF)

    def close(self) -> None:
        with self._lock:
            for dev in self._devs:
                try:
                    dev.send_feature_report(bytes([0, self._ALL_OFF, 0, 0, 0, 0, 0, 0, 0]))
                    dev.close()
                except Exception:
                    pass
            self._devs = []

    def _send(self, cmd: int) -> None:
        report = bytes([0, cmd, 0, 0, 0, 0, 0, 0, 0])
        with self._lock:
            for dev in self._devs:
                try:
                    dev.send_feature_report(report)
                except Exception as exc:
                    log.warning("USB relay send error: %s", exc)


# =============================================================================
# APPLICATION
# =============================================================================

class MicroRaveApp:
    """
    All state changes run on a single dispatch thread (via a SimpleQueue).
    GPIO callbacks and timer callbacks post work items to the queue.
    Display rendering and the pygame event pump run on the main thread.
    """

    def __init__(self):
        self._state       = State.IDLE
        self._q           = queue.SimpleQueue()
        self._entry_timer: threading.Timer | None = None
        self._last_clock: tuple | None = None
        self._preset_session = False   # True while a dedicated Popcorn/Potato
                                        # track is playing — no "next track" then

        self._dispatch_thread = threading.Thread(target=self._dispatch, name="Dispatch", daemon=True)
        self._dispatch_thread.start()

        pygame.init()
        self.display   = Display()
        self.audio     = AudioEngine()   # before Playlist: it validates files via the mixer
        self.playlists = Playlist()
        self.timer     = CountdownTimer(
            on_tick   = lambda r: self._post(self._on_tick,   r),
            on_finish = lambda:   self._post(self._on_finish),
        )
        self.buf = TimeEntryBuffer()
        self._keymap = KEYPAD_MAP

        self.relays = RelayController()
        self.relays.all_off()          # known-off at boot
        self._show_clock(force=True)
        log.info("MicroRave ready.")

    # -------------------------------------------------------------------------
    # Dispatch queue
    # -------------------------------------------------------------------------

    def _post(self, fn, *args):
        self._q.put((fn, args))

    def _dispatch(self):
        while True:
            item = self._q.get()
            if item is _STOP_SENTINEL:
                break
            fn, args = item
            try:
                fn(*args)
            except Exception as exc:
                log.error("Dispatch error in %s: %s", fn.__name__, exc, exc_info=True)

    def _drain(self, timeout: float = 1.0) -> bool:
        """Block until all currently-queued dispatch items are processed. Used in tests."""
        done = threading.Event()
        self._q.put((done.set, ()))
        return done.wait(timeout=timeout)

    # -------------------------------------------------------------------------
    # Keypad dispatch
    # -------------------------------------------------------------------------

    def _on_key(self, label: str):
        """Route one keypad label to its handler. Runs on the dispatch thread."""
        if label.isdigit():
            self._on_digit(int(label))
        elif label == "START":
            self._on_start()
        elif label == "STOP":
            self._on_stop()
        elif label == "ADD30":
            self._on_add_30()
        elif label in ("POPCORN", "POTATO"):
            self._on_preset(label)
        elif label == "NEXTTRACK":
            self._on_next_track()

    # -------------------------------------------------------------------------
    # Event handlers  (all run on the dispatch thread)
    # -------------------------------------------------------------------------

    def _on_digit(self, digit: int):
        log.info("Key: %d", digit)
        self.audio.beep()
        if self._state in (State.IDLE, State.ENTERING_TIME):
            was_idle = self._state == State.IDLE
            if self.buf._from_add30:
                # Buffer was set by +30 — add digit as seconds (not digit-shift)
                self.buf.set_from_seconds(self.buf.to_seconds() + digit)
            else:
                self.buf.push(digit)
            self._state = State.ENTERING_TIME
            self.display.show(self.buf.display_str())
            if was_idle:
                self._start_entry_timer()

    def _on_start(self):
        log.info("Key: START")
        self.audio.beep()
        if self._state == State.ENTERING_TIME and not self.buf.is_zero():
            self._begin_countdown()
        elif self._state in (State.IDLE, State.ENTERING_TIME) and self.buf.is_zero():
            # START with no time entered — prompt by flashing 0000
            self._state = State.ENTERING_TIME
            self._flash_zero_prompt()
            self._start_entry_timer()

    def _on_stop(self):
        log.info("Key: STOP")
        self.audio.beep()
        active = self._state in (State.COUNTING_DOWN, State.FINISHED) or \
                 (self._state == State.ENTERING_TIME and not self.buf.is_zero())
        if active:
            # 1st press — cancel everything and park on 0000
            self.timer.stop()
            self.audio.stop()
            self.relays.all_off()
            self.buf.clear()
            self._state = State.ENTERING_TIME
            self.display.show("0000")
            self._start_entry_timer()
            log.info("Stopped — press STOP again for the clock.")
        else:
            # 2nd press (already parked on 0000) or from IDLE — back to the clock
            self._cancel_entry_timer()
            self._go_idle_from_entry()

    def _on_add_30(self):
        log.info("Key: +30s")
        self.audio.beep()
        if self._state in (State.IDLE, State.ENTERING_TIME):
            self.buf.set_from_seconds(self.buf.to_seconds() + 30)
            self._state = State.ENTERING_TIME
            self.display.show(self.buf.display_str())
            self._begin_countdown()
        elif self._state == State.COUNTING_DOWN and not self._preset_session:
            self.timer.add(30)   # uncapped — deliberate
            # Refresh the display immediately — the timer thread's own tick can
            # be up to 1s away, which reads as "did that even register?".
            self.display.show(self._fmt_countdown(self.timer.remaining))

    def _on_preset(self, label: str):
        log.info("Key: %s", label)
        self.audio.beep()
        self._cancel_entry_timer()
        self.timer.stop()
        self.audio.stop()

        name = label.lower()   # "popcorn" / "potato"
        track = _find_preset_track(name)
        if track:
            # Play the dedicated track once; the cosmetic countdown is seeded
            # from its measured length (+1s margin) and _on_preset_track_done
            # ends the session the moment playback actually finishes, rather
            # than waiting for that countdown to reach zero.
            secs = _probe_track_seconds(track, PRESET_SECONDS)
            self.buf.set_from_seconds(secs)
            self._state = State.ENTERING_TIME
            self.display.show(self.buf.display_str())
            log.info("%s: playing %s once (~%ds)", label, os.path.basename(track), secs)
            self._begin_countdown(
                track_provider=_single_play_provider(track),
                on_complete=lambda: self._post(self._on_preset_track_done),
                clamp=False,          # a curated preset track isn't subject to the 5:00 cap
                preset_session=True,  # blocks Next Track — there's nothing to skip to
            )
        else:
            log.warning("No %s track found in %s/ — using the shared playlist.",
                        name, PRESET_DIR)
            self.buf.set_from_seconds(PRESET_SECONDS)
            self._state = State.ENTERING_TIME
            self.display.show(self.buf.display_str())
            self._begin_countdown()   # starts immediately — no START press needed

    def _on_preset_track_done(self):
        """Popcorn/Potato's dedicated track finished playing naturally — end
        the session now rather than waiting for the cosmetic countdown."""
        if self._state == State.COUNTING_DOWN:
            log.info("Preset track finished — ending session.")
            self.timer.stop()
            self._on_finish()

    def _on_next_track(self):
        log.info("Key: NEXT TRACK")
        self.audio.beep()
        if self._state == State.COUNTING_DOWN and not self._preset_session:
            self.audio.start(self.playlists.next_track)

    def _on_tick(self, remaining: int):
        if self._state == State.COUNTING_DOWN:
            self.display.show(self._fmt_countdown(remaining))

    def _on_finish(self):
        log.info("Countdown finished!")
        self._state = State.FINISHED
        self.buf.clear()
        self.audio.ding()
        self.relays.all_off()
        self.display.show("0000")
        t = threading.Timer(3.0, lambda: self._post(self._go_idle))
        t.daemon = True
        t.start()

    def _go_idle(self):
        if self._state == State.FINISHED:
            self._state = State.IDLE
            self.relays.all_off()
            self._show_clock(force=True)

    def _flash_zero_prompt(self) -> None:
        """Flash 0000 three times to prompt the user to enter a time.
        Fired when START is pressed with an empty buffer. Runs in a worker
        thread so the dispatch loop stays responsive. Leaves the display on
        '0000' at the end so the user can immediately start typing."""
        def _flash():
            on_s, off_s = 0.3, 0.2
            for _ in range(3):
                self.display.show("0000")
                time.sleep(on_s)
                self.display.show("    ", colon=False)
                time.sleep(off_s)
            self.display.show("0000")
        threading.Thread(target=_flash, name="ZeroPromptFlash", daemon=True).start()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _fmt_countdown(self, remaining: int) -> str:
        """Format remaining seconds as MM:SS digits for the 4-char display.
        Minutes clamp at 99 so the format never widens past 4 chars (which
        would make the display refresh only every 10 ticks)."""
        m, s = divmod(remaining, 60)
        return "%02d%02d" % (min(m, 99), s)

    def _begin_countdown(self, track_provider=None, on_complete=None, clamp=True,
                         preset_session=False):
        self._cancel_entry_timer()
        secs = self.buf.to_seconds()
        if clamp:
            secs = min(secs, MAX_ENTRY_SECONDS)
            if self.buf.to_seconds() > MAX_ENTRY_SECONDS:
                log.info("Entered time clamped to %ds (5:00 cap).", MAX_ENTRY_SECONDS)
        if secs == 0:
            return
        log.info("Countdown: %ds", secs)
        self._preset_session = preset_session
        self._state = State.COUNTING_DOWN
        self.audio.start(track_provider or self.playlists.next_track, on_complete=on_complete)
        self.timer.start(secs)
        self.relays.all_on()          # cooking indicator

    def _show_clock(self, force: bool = False):
        """24-hour clock — always two real digits for the hour (00-23), so
        there's no blank leading digit for the ghost segments to show through
        (a 12-hour display left that slot blank for 1-9 o'clock). The colon
        blinks on the half-second — on for the first half of each second, off
        (down to the dim ghost dots) for the second half."""
        now = datetime.now()
        h     = now.hour
        m     = now.minute
        colon = now.microsecond < 500_000
        state = (h, m, colon)
        if not force and state == self._last_clock:
            return
        self._last_clock = state
        self.display.show("%02d%02d" % (h, m), colon=colon)

    def _start_entry_timer(self):
        self._cancel_entry_timer()
        if ENTRY_IDLE_TIMEOUT > 0:
            t = threading.Timer(ENTRY_IDLE_TIMEOUT, lambda: self._post(self._go_idle_from_entry))
            t.daemon = True
            t.start()
            self._entry_timer = t

    def _cancel_entry_timer(self):
        if self._entry_timer:
            self._entry_timer.cancel()
            self._entry_timer = None

    def _go_idle_from_entry(self):
        if self._state != State.ENTERING_TIME:
            return
        self.buf.clear()
        self._state = State.IDLE
        self._show_clock(force=True)
        log.info("Returned to clock.")

    # -------------------------------------------------------------------------
    # Main loop  (runs on main thread — owns pygame event pump and rendering)
    # -------------------------------------------------------------------------

    def _start_scheduling_watchdog(self):
        """Background thread that sleeps 20ms in a tight loop and logs any
        scheduling gap > 60ms. Catches OS/kernel stalls that would also
        starve the SDL audio callback — the most likely cause of brief clipping."""
        def _watchdog():
            target = 0.02
            stall = 0.06
            last = time.monotonic()
            while True:
                time.sleep(target)
                now = time.monotonic()
                gap = now - last
                if gap > stall:
                    log.warning("Scheduling watchdog stall: %.0fms (target %.0fms) — possible audio-clip cause",
                                gap * 1000, target * 1000)
                last = now
        threading.Thread(target=_watchdog, name="SchedWatchdog", daemon=True).start()

    def run(self):
        log.info("Running — Ctrl+C or Esc to quit.")
        self._start_scheduling_watchdog()
        last_iter = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                gap = now - last_iter
                # Main loop targets 50ms (20fps). >150ms means we hung — possible audio cause.
                if gap > 0.15:
                    log.warning("Main loop stall: %.0fms gap (target 50ms)", gap * 1000)
                last_iter = now
                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        return
                    if ev.type == AudioEngine._MUSIC_END:
                        self.audio.notify_music_end()
                    elif ev.type == pygame.KEYDOWN:
                        if ev.key == pygame.K_ESCAPE:
                            return
                        label = self._keymap.get(ev.key)
                        if label:
                            self._post(self._on_key, label)

                if self._state == State.IDLE:
                    self._show_clock()

                self.display.render()
                time.sleep(0.05)   # 20 fps — smooth enough for animation

        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self):
        log.info("Shutting down…")
        self._cancel_entry_timer()
        self.timer.stop()
        self.audio.stop()
        self.audio.shutdown()        # flush unsaved play counts
        time.sleep(0.1)
        self._q.put(_STOP_SENTINEL)  # drain dispatch thread cleanly
        self.relays.close()          # all relays off, then release the HID handles
        pygame.quit()
        log.info("Goodbye.")


# =============================================================================
# STARTUP VALIDATION
# =============================================================================

def check_env() -> bool:
    ok = True
    for path, label in [(BEEP_SOUND, "beep"), (DING_SOUND, "ding")]:
        if not os.path.isfile(path):
            log.error("Missing %s sound: %s", label, path)
            ok = False
    if not os.path.isdir(MUSIC_ROOT):
        log.error("Shared playlist folder not found: %s/", MUSIC_ROOT)
        ok = False
    return ok


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    if not check_env():
        log.critical("Environment check failed — fix errors above and restart.")
        raise SystemExit(1)
    try:
        app = MicroRaveApp()
        app.run()
    except Exception as exc:
        log.critical("Fatal: %s", exc, exc_info=True)
        raise SystemExit(1)
