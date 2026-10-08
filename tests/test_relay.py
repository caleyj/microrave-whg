"""RelayController bus-scan backoff, against a fake hidapi."""
import time
import pytest
import microrave


class FakeHid:
    def __init__(self, present):
        self.present = present
        self.scans = 0
        self.reports = []

    def enumerate(self, vid, pid):
        self.scans += 1
        if not self.present:
            return []
        return [{"path": b"1-2:1.0", "product_string": "USBRelay4", "serial_number": ""}]

    def device(self):
        return FakeDevice(self)


class FakeDevice:
    def __init__(self, hid):
        self._hid = hid

    def open_path(self, path):
        if not self._hid.present:
            raise OSError("open failed")

    def send_feature_report(self, report):
        self._hid.reports.append(report)

    def close(self):
        pass


@pytest.fixture
def fake_hid(monkeypatch):
    def make(present):
        fake = FakeHid(present)
        monkeypatch.setattr(microrave, "_HID_AVAILABLE", True)
        monkeypatch.setattr(microrave, "_hid", fake, raising=False)
        monkeypatch.setattr(microrave, "RELAY_RESCAN_INTERVAL", 0.2)
        return fake
    return make


class TestRescanBackoff:

    def test_missing_board_is_not_rescanned_on_every_command(self, fake_hid):
        hid = fake_hid(present=False)
        relay = microrave.RelayController()
        for _ in range(5):
            relay.all_on()
        assert hid.scans == 1   # just the startup scan

    def test_missing_board_is_rescanned_after_the_interval(self, fake_hid):
        hid = fake_hid(present=False)
        relay = microrave.RelayController()
        time.sleep(0.25)
        relay.all_on()
        assert hid.scans == 2

    def test_board_that_appears_later_is_picked_up_and_used(self, fake_hid):
        hid = fake_hid(present=False)
        relay = microrave.RelayController()
        hid.present = True
        time.sleep(0.25)
        relay.all_on()
        assert len(hid.reports) == 1
        relay.all_off()
        relay.all_on()
        assert hid.scans == 2   # found, so no further scans
        assert len(hid.reports) == 3

    def test_lost_board_is_rescanned_immediately_then_backs_off(self, fake_hid):
        hid = fake_hid(present=True)
        relay = microrave.RelayController()
        hid.present = False
        relay.all_on()          # send fails, path dropped
        relay.all_on()          # first command after the loss rescans at once
        assert hid.scans == 2
        relay.all_on()
        relay.all_on()
        assert hid.scans == 2   # empty scan -> backed off


class TestUsbPresence:
    """_usb_presence reads sysfs to say whether the board is on the bus and
    whether the kernel bound the HID driver to it."""

    def _device(self, base, name="1-2", vid="16c0", pid="05df"):
        dev = base / name
        dev.mkdir(parents=True)
        (dev / "idVendor").write_text(vid + "\n")
        (dev / "idProduct").write_text(pid + "\n")
        return dev

    def test_present_and_bound_with_hidraw(self, tmp_path):
        dev = self._device(tmp_path)
        iface = dev / "1-2:1.0"
        (iface / "0003:16C0:05DF.0003" / "hidraw" / "hidraw2").mkdir(parents=True)
        drivers = tmp_path / "drivers" / "usbhid"
        drivers.mkdir(parents=True)
        (iface / "driver").symlink_to(drivers)
        msg = microrave._usb_presence(0x16c0, 0x05df, base=str(tmp_path))
        assert "IS on the USB bus at 1-2" in msg
        assert "driver=usbhid" in msg and "hidraw=yes" in msg

    def test_present_but_hid_never_bound(self, tmp_path):
        dev = self._device(tmp_path)
        (dev / "1-2:1.0").mkdir()
        msg = microrave._usb_presence(0x16c0, 0x05df, base=str(tmp_path))
        assert "IS on the USB bus at 1-2" in msg
        assert "driver=none" in msg and "hidraw=no" in msg

    def test_absent_from_the_bus(self, tmp_path):
        self._device(tmp_path, name="1-1", vid="2341", pid="8037")   # a different device
        msg = microrave._usb_presence(0x16c0, 0x05df, base=str(tmp_path))
        assert msg == "device NOT on the USB bus"

    def test_no_sysfs_does_not_raise(self, tmp_path):
        msg = microrave._usb_presence(0x16c0, 0x05df, base=str(tmp_path / "missing"))
        assert msg == "USB sysfs unavailable"
