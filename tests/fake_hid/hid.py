"""Stand-in for the hidapi `hid` module, used by relay_helper.py in tests.

FAKE_HID_MODE: present (default) | absent | open_fail | hang_open | missing
FAKE_HID_LOG:  file that every sent feature report is appended to (hex).
"""
import os
import time

if os.environ.get("FAKE_HID_MODE") == "missing":
    raise ImportError("hidapi not installed (simulated)")


def enumerate(vid, pid):
    if os.environ.get("FAKE_HID_MODE") == "absent":
        return []
    return [{"path": b"1-2:1.0", "product_string": "USBRelay4", "serial_number": ""}]


class device:
    def open_path(self, path):
        mode = os.environ.get("FAKE_HID_MODE")
        if mode == "open_fail":
            raise OSError("open failed")
        if mode == "hang_open":
            time.sleep(30)

    def send_feature_report(self, report):
        log = os.environ.get("FAKE_HID_LOG")
        if log:
            with open(log, "a") as f:
                f.write(bytes(report).hex() + "\n")

    def close(self):
        pass
