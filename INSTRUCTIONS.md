# MicroRave — Quick Reference

## What Is This?
MicroRave is a microwave-shell countdown music-box running on a Raspberry Pi 5.
Enter a time on the keypad, press Start, music plays while it counts down, and
it "dings" when the time runs out. There is **no door** — Start begins the
countdown, and it ends only when it reaches 0:00.

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

Input is a **USB numeric keypad** (plugged into any USB port — it acts as a
keyboard). Default key mapping (edit `KEYPAD_MAP` at the top of `microrave.py`
to change it):

| Key on the keypad | Function |
|-------------------|----------|
| `0`–`9`           | Enter countdown time (digits shift in from the right) |
| `Enter`           | **Start** |
| `.` / `Del`       | **Stop** (1st press cancels, 2nd press returns to clock) |
| `+`               | **+30s** |
| `/`               | **Popcorn** — 3:00, starts immediately |
| `*`               | **Potato** — 3:00, starts immediately |

15 logical buttons total: ten digits, Start, Stop, +30s, Popcorn, Potato.

---

## How to Use

1. **Enter time** — press digits; they shift in from the right like a real microwave
2. **Start** — press Start (`Enter`)
3. **One-touch** — press Popcorn or Potato for an instant 3:00 countdown
4. **Add time** — press +30s at any time (this *can* push a running countdown past 5:00)
5. **Stop** — 1st press cancels the countdown and shows `0000`; 2nd press goes back to the clock
6. **Finish** — at 0:00 the machine plays a microwave "ding" and returns to the clock

**5-minute cap:** whatever time you type, the countdown is clamped to **5:00** when
it starts. `+30s` presses *while it is running* are not capped.

Idle shows a 12-hour clock. After ~60 s of no input on the entry screen it returns
to the clock automatically.

---

## Music & Audio Files

```
/home/pi/MicroRave/
  music/                 ← one shared playlist (any folder layout; searched recursively)
  sounds/
    beep.mp3
    ding.mp3
  playcounts.json        ← play history (auto-updated)
```

Supported formats: `.mp3  .wav  .ogg  .flac  .m4a`

Every countdown bag-shuffles the `music/` folder: each track plays once before any
repeat, and the bag carries over between countdowns.

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
