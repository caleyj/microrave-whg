# MicroRave — Quick Reference

## What Is This?
MicroRave is a microwave-shell countdown music-box running on a Raspberry Pi 5.
Enter a time on the keypad, press Start, music plays while it counts down, and
it "dings" when the time runs out. There is **no door** — Start begins the
countdown, and it ends only when it reaches 0:00.

---

## First-Time Setup (New Pi)

Starting from a blank SD card. This build needs no GPIO wiring — just power,
HDMI, and USB.

1. **Flash the OS.** Raspberry Pi Imager → **Raspberry Pi OS (64-bit), with
   desktop** (a desktop session is required — MicroRave runs fullscreen over
   X11/XWayland). In the imager's settings (gear icon) set username **`pi`**
   (the service files below hardcode `/home/pi/MicroRave`; use a different
   name only if you're happy to edit those paths), enable SSH, and set Wi-Fi
   if needed. Boot it and log in.

2. **Update and install dependencies:**
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y git python3-venv python3-pip python3-pygame \
       libhidapi-hidraw0 x11-xserver-utils
   ```

3. **Clone the repo** into the exact path the service expects:
   ```bash
   git clone https://github.com/caleyj/microrave-whg.git ~/MicroRave
   cd ~/MicroRave
   ```

4. **Create the venv** (`--system-site-packages` so it can see the apt-installed
   pygame — only `hidapi` needs to come from pip):
   ```bash
   python3 -m venv --system-site-packages venv
   venv/bin/pip install hidapi
   ```
   If that fails to build, `sudo apt install -y libhidapi-dev python3-dev` first.

5. **Add your music.** `sounds/` (beep + ding) and `presets/` (popcorn + potato)
   already come with the clone — `music/` doesn't (it's gitignored, personal
   content):
   ```bash
   mkdir -p music
   # copy your tracks into music/ — any folder layout, searched recursively
   ```

6. **Plug in the hardware:** HDMI display/TV, HDMI (or other) audio out, the USB
   keypad, and — optionally — the dcttech USB-HID relay board (`16c0:05df`) for
   the cooking lamp. Everything works with the relay board absent; it just logs
   "no board found".

7. **Test it manually**, from the Pi's own desktop (not SSH — it needs the
   display):
   ```bash
   cd ~/MicroRave
   sudo DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority venv/bin/python microrave.py
   ```
   Confirm the clock shows, digits/Enter/Backspace work, and `A`/`B` play the
   presets. `Esc` quits. If the relay board is plugged in, `lsusb | grep 16c0`
   should show it.

8. **Install as a boot service:**
   ```bash
   sudo cp microrave.service /etc/systemd/system/
   sudo cp rave.sh /usr/local/bin/rave
   sudo chmod +x /usr/local/bin/rave start_microrave.sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now microrave
   sudo systemctl status microrave   # should show "active (running)"
   ```
   Reboot once (`sudo reboot`) to confirm it comes up on its own.

9. **Optional polish:**
   - Boot splash/black screen — see **Boot Display** below.
   - Screen blanking: Bookworm's desktop may blank/dim the display after
     inactivity. If you see that happen, disable it via `raspi-config` →
     **Display Options** → **Screen Blanking**, or the equivalent in your
     desktop's power settings.

From here on, use `rave` / `rave --stats` and the systemd commands below for
day-to-day use — see **Running MicroRave** and **Service Control**.

---

## Running MicroRave

| What | Command |
|------|---------|
| Run manually (with terminal output) | `rave` |
| View play stats | `rave --stats` |
| View live log | `sudo journalctl -u microrave -f` |
| View detailed log file | `tail -f /home/pi/MicroRave/microrave.log` |

MicroRave **starts automatically on boot** via systemd. The `rave` command
stops the background service, runs interactively, then restarts the service on exit.

---

## Keypad Layout

Input is a **USB keypad** (plugged into any USB port — it acts as a keyboard).
Function keys are plain letters, no symbol keys. Default key mapping (edit
`KEYPAD_MAP` at the top of `microrave.py` to move a function to a different key):

| Key | Function |
|-----|----------|
| `0`–`9`     | Enter countdown time (digits shift in from the right) |
| `Enter`     | **Start** |
| `Backspace` | **Stop** (1st press cancels, 2nd press returns to clock) |
| `A`         | **Popcorn** — one-touch preset, plays `presets/popcorn.*` once |
| `B`         | **Potato** — one-touch preset, plays `presets/potato.*` once |
| `C`         | **+30s** |
| `D`         | **Next track** — skips to the next shuffled track (countdown only) |

16 logical buttons total: ten digits, Start, Stop, +30s, Popcorn, Potato, Next track.

---

## How to Use

1. **Enter time** — press digits; they shift in from the right like a real microwave
2. **Start** — press Start (`Enter`)
3. **One-touch** — press Popcorn (`A`) or Potato (`B`): its dedicated track plays
   once, then the machine dings and returns to the clock — no fixed 3:00 wait
4. **Add time** — press +30s (`C`) at any time (this *can* push a running countdown past 5:00)
5. **Skip track** — press Next track (`D`) while counting down to jump to the next
   shuffled track; the timer is untouched
6. **Stop** — 1st press (`Backspace`) cancels the countdown and shows `0000`;
   2nd press goes back to the clock
7. **Finish** — at 0:00 (or when a Popcorn/Potato track ends) the machine plays
   a microwave "ding" and returns to the clock

**5-minute cap:** whatever time you type, the countdown is clamped to **5:00** when
it starts. `+30s` presses *while it is running* are not capped.

Idle shows a 12-hour clock. After ~60 s of no input on the entry screen it returns
to the clock automatically.

---

## Music & Audio Files

```
/home/pi/MicroRave/
  music/                 ← shared playlist (any folder layout; searched recursively)
  presets/
    popcorn.mp3          ← plays once for Popcorn, then done
    potato.mp3           ← plays once for Potato, then done
  sounds/
    beep.mp3
    ding.mp3
  playcounts.json        ← play history (auto-updated)
```

Supported formats: `.mp3  .wav  .ogg  .flac  .m4a`

Every countdown bag-shuffles the `music/` folder: each track plays once before any
repeat, and the bag carries over between countdowns. **Next track** (`D`) skips ahead
in this same shuffle — including out of a Popcorn/Potato session, if pressed (which
also switches that session over to the shared shuffle).

Popcorn and Potato each play one dedicated track **once** — no loop — and the
countdown display is timed to that track's actual length, ending (with the "ding")
the moment playback finishes rather than waiting out a fixed clock. Drop a file
named `popcorn` and `potato` (any supported extension) into `presets/` — if either
is missing, that button falls back to the shared shuffle for a fixed 3:00 instead,
and logs a warning.

---

## Hardware

- **Pi 5** — HDMI display + HDMI audio; no GPIO wiring needed for input
- **Display** — HDMI fullscreen 7-segment clock, green on black (pygame)
- **Audio** — HDMI → TV speakers
- **Input** — USB numeric keypad (HID keyboard)
- **Cooking lamp** — dcttech **USB-HID relay board** (`16c0:05df`): all relay
  channels switch **ON** while a countdown runs and **OFF** when idle / finished /
  stopped. Auto-detected on start; the app runs fine with no board plugged in.

### USB relay setup (Pi)

```bash
sudo apt install libhidapi-hidraw0
venv/bin/pip install hidapi
```

The systemd service runs as root, so it can open the HID device directly. For
running as a non-root user, add a udev rule granting access to `16c0:05df`.

Verify the board is seen: `lsusb | grep 16c0`.

---

## Service Control

| What | Command |
|------|---------|
| Check if running | `sudo systemctl status microrave` |
| Stop service | `sudo systemctl stop microrave` |
| Start service | `sudo systemctl start microrave` |
| Disable autostart | `sudo systemctl disable microrave` |
| Re-enable autostart | `sudo systemctl enable microrave` |

---

## Boot Display (Black Screen)

The Pi is configured for a clean black boot with no splash screens.
Backups of the original boot files are stored on the Pi.

**To restore the original boot splash/text:**
```bash
sudo cp /boot/firmware/config.txt.backup /boot/firmware/config.txt
sudo cp /boot/firmware/cmdline.txt.backup /boot/firmware/cmdline.txt
sudo reboot
```

**To re-apply the black boot (after restoring):**
```bash
echo "disable_splash=1" | sudo tee -a /boot/firmware/config.txt
sudo sed -i 's/$/ quiet splash/' /boot/firmware/cmdline.txt
sudo reboot
```

Note: the black screen only affects the HDMI display — SSH always works
regardless of what the screen shows.

---

## Files

| File | Purpose |
|------|---------|
| `microrave.py` | Main application |
| `playcounts.json` | Persistent play count history |
| `microrave.log` | Detailed application log |
| `microrave.service` | systemd service definition |
| `start_microrave.sh` | Boot launcher script |
| `rave.sh` | Source for the `rave` command |
| `tests/` | Unit + integration tests (`python -m pytest tests/`) |
