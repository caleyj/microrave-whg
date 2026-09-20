# Changelog

All notable changes to the MicroRave project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2026-09-20

### Fixed
- **A second source of "main loop stall": bad audio files.** A malformed file
  in `music/` (confirmed: `mpg123_getformat: ... I am done with this track.`,
  the exact message reported) made `pygame.mixer.music.load()` do real,
  slow decode/resync work before failing — a genuine blocking call, and one
  that repeated every time the shuffle's bag rotated back to that file. The
  previous fix (caching `Display` glyphs) addressed one cause of these
  stalls; this is another, independent one.
  `Playlist` now test-loads every candidate file through the mixer once, at
  startup — after `AudioEngine` (reordered in `MicroRaveApp.__init__`, since
  the mixer has to exist first) — and excludes anything that fails, logging
  the exact path so it's obvious which file to fix or remove. A bad file now
  costs one slow load at boot instead of a live-countdown stall every time
  it comes up in rotation.
  Added a matching safety net in `AudioEngine` for failures that only show
  up at runtime regardless (a transient read error, a file that goes bad
  mid-session): a failed `_play()` now retries after a short pause instead
  of hammering the next track immediately, and gives up after 5 consecutive
  failures rather than spinning forever.

## [Unreleased] — 2026-09-19

### Fixed
- **"Main loop stall" warnings during any countdown.** `Display` cached its
  DSEG7 render by the whole 5-character string ("12:34"). That's fine for
  the idle clock (only a couple of distinct minutes shown per hour), but
  during a countdown the string is different every second, so it was a
  100% cache miss doing a full font render + glow blur on the main thread
  on every single tick (~40-60ms measured on a much faster machine than the
  Pi — comfortably over the 150ms stall threshold once you add real Pi
  hardware and desktop-compositor load). Fixed by pre-rendering every
  glyph this display can ever show — '0'-'9', a blank digit, colon on/off —
  once at startup (DSEG7 is fixed-pitch, so cells blit together seamlessly),
  and having `render()` just blit up to 5 of them. Per-tick cost dropped
  from ~40-60ms to under a millisecond in the same measurement.
- Fixed a rendering bug this surfaced: the colon cell is narrower than its
  own glow padding, so the next digit's opaque blit was overwriting part of
  it. Each cell now colour-keys its background transparent so neighbouring
  cells' padding never overwrites each other's glow.

### Changed
- **Idle clock colon blinks twice as fast.** On for the first half of each
  second, off for the second half, instead of a full second each way.
- **+30s is now a no-op during Popcorn/Potato**, same as Next Track already
  was — a curated preset track's length isn't meant to be adjusted.

## [Unreleased] — 2026-09-03

### Changed
- **Keypad map: letters instead of symbols.** Function keys are now plain
  letters — `A`=Popcorn, `B`=Potato, `C`=+30s, `D`=Next track — instead of the
  numpad `/ * +` keys. Start stays `Enter`, Stop stays `Backspace`. Edit
  `KEYPAD_MAP` at the top of `microrave.py` to move a function to a different key.
- **Popcorn / Potato play a dedicated track once.** Each preset plays one fixed
  file — `presets/popcorn.*` / `presets/potato.*` (any of the usual audio
  extensions) — through a single time, no loop, instead of pulling from the
  shared shuffle. The cosmetic countdown is seeded from the track's measured
  length; the session actually ends (ding, then idle) the moment playback
  finishes naturally, via a new `AudioEngine.start(..., on_complete=)` hook —
  not when that countdown reaches zero, so the track is never cut short and
  never runs on past its own end. Falls back to the shared shuffle for a fixed
  3:00 (logged) if the file is missing.
- **`presets/` is now version-controlled** (previously gitignored like `music/`)
  — `popcorn.mp3` / `potato.mp3` ship in the repo.

### Added
- **Next track button.** Skips to the next track in the shared shuffle. Only
  does anything during a normal (shared-shuffle) countdown; never touches the
  timer. It's a no-op during a Popcorn/Potato session — there's no "next" for
  a single dedicated track — and outside `COUNTING_DOWN` generally.

## [Unreleased] — 2026-08-28 — "our build"

Re-target `microrave.py` from the 22-switch event panel to a simpler, literal
microwave: a USB keypad, no door, one-touch presets, and a USB-HID cooking lamp.

### Added
- **New display look.** The readout uses the DSEG7 Classic 7-segment font
  (`fonts/DSEG7Classic-Bold.ttf`, SIL OFL 1.1) rendered green-on-black with a
  faint `88:88` ghost behind the value and a soft bloom around the lit digits,
  replacing the hand-drawn segment polygons. Colour and glow are set by the
  `COLOR_*` / `GLOW_*` / `SHOW_DIM_SEGS` constants near the top of `microrave.py`
  (a commented amber palette is included); it falls back to the default font if
  the DSEG file is missing.
- **USB numeric keypad input.** All input now comes from a USB HID keypad; keypresses
  are read from the pygame event loop and mapped through an editable `KEYPAD_MAP`
  (digits `0`–`9`, `Enter`=Start, `.`=Stop, `+`=+30s, `/`=Popcorn, `*`=Potato).
  The fullscreen window grabs keyboard focus so presses don't leak to the desktop.
- **Popcorn / Potato one-touch keys.** Either one sets a 3:00 countdown and starts
  it immediately — no Start press (`PRESET_SECONDS`).
- **5-minute entry cap.** However long a time you type, the countdown is clamped to
  `MAX_ENTRY_SECONDS` (5:00) when it starts. `+30s` *during* a countdown is uncapped.
- **Two-stage Stop.** 1st press cancels the countdown and parks the display on
  `0000`; 2nd press returns to the idle clock.
- **USB-HID relay "cooking" lamp.** `RelayController` now drives dcttech USB-HID
  relay boards (`16c0:05df`) via 9-byte HID feature reports: all channels ON while
  a countdown runs, OFF on finish / stop / idle. Auto-detected; disabled gracefully
  if `hidapi` or the board is missing. Needs `libhidapi-hidraw0` + `pip install hidapi`.
- `from __future__ import annotations` so the module imports on Python 3.9+.
- `pytest.ini` limiting collection to `tests/test_*.py`.

### Removed
- **The door.** No door switch, no pause-on-open, no door-open auto-cancel. A
  countdown starts on Start and ends only at 0:00. `PAUSED` and `ANIMATING` states,
  `_pause_countdown` / `_resume_countdown`, and all `_on_door_*` handlers are gone.
- **DJ selection.** `DJ1`–`DJ6` keys, per-DJ playlists, the DJ indicator lamps, and
  the random-DJ race animation. `PlaylistManager` is now `Playlist` — one shared,
  bag-shuffled `music/` folder (searched recursively).
- **Easter-egg digit codes** (`EASTER_EGGS`, the flash + race-around animation, the
  DJ-light chase) and the **hidden WiFi recovery code** (`PANIC_WIFI_*`).
- **Volume keys** (`VolUp` / `VolDn`) — volume is fixed at `VOLUME_DEFAULT`.
- **All GPIO code** — `lgpio`, pin tables, the 50 Hz poll loop — and the `pyserial`
  Arduino relay path.

## [Unreleased] — 2026-05-22

Pre-show prep for the live install. Focus: audio reliability, more easter eggs, better stage UX, emergency-recovery affordances.

### Added
- **Three new easter eggs:**
  - `911` → countdown `9:11` (folder `sounds/easter/911/`)
  - `923` → countdown `9:23` (folder `sounds/easter/923/`)
  - `7734` → countdown `77:34` (folder `sounds/easter/7734/`) — "hELL" on an upside-down calculator; plays multi-track folder
- **DJ-light chase animation during easter eggs.** Lights cycle clockwise around the 2×3 grid, then counterclockwise, repeating until the egg ends. Replaces the previous "all off" behavior with something that visually dances along.
- **Empty-time START prompt.** Pressing START with the buffer at `0:00` (from IDLE or ENTERING_TIME) now flashes `0000` three times to prompt the user to enter a time, then leaves the display ready for input. Previously START was a silent no-op.
- **Hidden WiFi recovery code.** Typing `4108617369` (Mike's phone number) on the keypad — in any state — runs `/usr/local/bin/microrave-wifi-restore-defaults`, which tears down AP mode and re-enables client autoconnect on saved profiles. Lets you recover network access without SSH/console. Display flashes `----` during execution.
- **Scheduling / loop-stall instrumentation.** Three watchdogs that log any time-budget overruns:
  - Main-loop stall (`>150ms` gap, 20fps target)
  - GPIO-poll stall (`>60ms` gap, 50Hz target)
  - Dedicated scheduling watchdog thread (`>60ms` gap, 20ms target) — catches kernel-level CPU starvation that doesn't show up in the Python loops
  Used during diagnosis to distinguish Python-side stalls from kernel/SDL-side stalls.

### Changed
- **Audio buffer 512 → 4096 samples** in `pygame.mixer.pre_init`. The default 12ms of slack was too small for the Pi 5's real-world scheduler stalls (we measured consistent 160ms+ main-loop stalls). 4096 samples = ~93ms slack, which absorbs them cleanly. Fully fixed the brief-dropout audio artifacts we'd been hearing.
- **Volume step 5% → 10%** per Vol Up/Down press. Coarser increments are easier to dial in by ear during live use.
- **Volume floor raised to 10%.** Vol Down can no longer take volume to 0 — silenced audio during a live show is usually a mistake, not an intent.

### Fixed
- **DJ light restored after easter egg.** Added `set_dj(selected)` calls in `_on_stop`, `_on_finish`, and `_on_door_timeout` so the selected DJ's light comes back on after an easter egg session ends by any path. No-op for non-egg sessions.

### Infrastructure (not in repo, but documented for completeness)
- Pi configured as a WiFi access point (`HP-Print-3F-LaserJet` / `robotchicken`) for venues with no usable upstream network. AP profile autoconnect-priority 10 as fallback; venue router (`WhatLiesBeneath` / 50, `BROS` / 40) preferred when in range.
- Pygame audio buffer bump runs on the Pi via `pygame.mixer.pre_init(44100, -16, 2, 4096)`.
- systemd `microrave.service` changed `Restart=always` → `Restart=on-failure` so ESC drops cleanly to the desktop without immediate auto-restart.
- `fake-hwclock` enabled for time persistence across power cycles in the absence of an RTC battery.
