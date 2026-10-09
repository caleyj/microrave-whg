#!/usr/bin/env python3
"""USB-HID relay I/O for microrave.py, run as a short-lived child process.

A relay board stuck mid-USB-reset can block a kernel call for ~20 seconds and
stall every thread of the process making it. Doing the I/O here lets only
this process hang; microrave.py gives it a deadline and kills it.

  relay_helper.py scan
  relay_helper.py send <cmd> <path> [<path> ...]
  relay_helper.py sendch <cmd> <channels> <path> [<path> ...]   channels: "1" or "1,3"
  relay_helper.py test [<path>] [<seconds>]   click each channel on then off, by hand

Everything except `test` prints one JSON object on stdout.
"""
import json
import sys
import time

VID = 0x16C0
PID = 0x05DF


CHANNEL_GAP = 0.1   # seconds between channels when several are switched
ONE_ON, ONE_OFF = 0xFF, 0xFD


def _send_one(hid, path, channel, cmd):
    """One command on a fresh handle: open, write, close. These boards ignore
    later writes to a handle that is kept open."""
    report = bytes([0, cmd, channel, 0, 0, 0, 0, 0, 0])
    dev = hid.device()
    try:
        dev.open_path(path.encode("latin-1"))
        dev.send_feature_report(report)
        return {"path": path, "ok": True}
    except Exception as exc:
        return {"path": path, "ok": False, "error": str(exc)}
    finally:
        try:
            dev.close()
        except Exception:
            pass


def _test(hid, path, hold):
    """Switch each of the four channels on and then off, announcing each, so
    you can see (or hear) which relay is wired to what."""
    if path is None:
        found = hid.enumerate(VID, PID)
        if not found:
            print("No relay board found.")
            return 1
        path = found[0]["path"].decode("latin-1")
    for ch in (1, 2, 3, 4):
        print("%s channel %d ON" % (time.strftime("%T"), ch), flush=True)
        _send_one(hid, path, ch, ONE_ON)
        time.sleep(hold)
        print("%s channel %d OFF" % (time.strftime("%T"), ch), flush=True)
        _send_one(hid, path, ch, ONE_OFF)
        time.sleep(hold / 2)
    print("done", flush=True)
    return 0


def _finish(obj, code=0):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    sys.exit(code)


def main(argv):
    try:
        import hid
    except ImportError:
        _finish({"error": "hidapi not installed"}, 4)

    if len(argv) >= 2 and argv[1] == "scan":
        boards = [{"path": info["path"].decode("latin-1"),
                   "product": info.get("product_string"),
                   "serial": info.get("serial_number")}
                  for info in hid.enumerate(VID, PID)]
        _finish({"boards": boards})

    if len(argv) >= 4 and argv[1] == "send":
        _finish({"results": [_send_one(hid, p, 0, int(argv[2])) for p in argv[3:]]})

    if len(argv) >= 5 and argv[1] == "sendch":
        cmd = int(argv[2])
        channels = [int(c) for c in argv[3].split(",") if c]
        results = []
        for path in argv[4:]:
            for n, ch in enumerate(channels):
                if n:
                    time.sleep(CHANNEL_GAP)   # stagger the coils
                results.append(_send_one(hid, path, ch, cmd))
        _finish({"results": results})

    if argv[1:2] == ["test"]:
        path = argv[2] if len(argv) > 2 else None
        hold = float(argv[3]) if len(argv) > 3 else 1.5
        sys.exit(_test(hid, path, hold))

    _finish({"error": "usage: relay_helper.py scan | send <cmd> <path>..."}, 2)


if __name__ == "__main__":
    main(sys.argv)
