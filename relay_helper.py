#!/usr/bin/env python3
"""USB-HID relay I/O for microrave.py, run as a short-lived child process.

A relay board stuck mid-USB-reset can block a kernel call for ~20 seconds and
stall every thread of the process making it. Doing the I/O here lets only
this process hang; microrave.py gives it a deadline and kills it.

  relay_helper.py scan
  relay_helper.py send <cmd> <path> [<path> ...]

Prints one JSON object on stdout.
"""
import json
import sys

VID = 0x16C0
PID = 0x05DF


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
        report = bytes([0, int(argv[2]), 0, 0, 0, 0, 0, 0, 0])
        results = []
        for path in argv[3:]:
            # Fresh open + write + close per command: these boards ignore
            # later writes to a handle that is kept open.
            dev = hid.device()
            try:
                dev.open_path(path.encode("latin-1"))
                dev.send_feature_report(report)
                results.append({"path": path, "ok": True})
            except Exception as exc:
                results.append({"path": path, "ok": False, "error": str(exc)})
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
        _finish({"results": results})

    _finish({"error": "usage: relay_helper.py scan | send <cmd> <path>..."}, 2)


if __name__ == "__main__":
    main(sys.argv)
