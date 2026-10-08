"""RelayController (subprocess-based), the stall tracer, and USB presence."""
import os
import time

import pytest

import microrave

FAKE_HID_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_hid")


# ── Backoff / bookkeeping, with the helper process stubbed out ────────────────

@pytest.fixture
def scripted(monkeypatch):
    class Script:
        def __init__(self):
            self.calls = []
            self.boards = []
            self.send_ok = True
            self.send_hangs = False

        def scans(self):
            return sum(1 for c in self.calls if c[0] == "scan")

    sc = Script()

    def fake_run(self, *args):
        sc.calls.append(args)
        if args[0] == "scan":
            return {"boards": [{"path": p, "product": "USBRelay4", "serial": ""}
                               for p in sc.boards]}
        if sc.send_hangs:
            return None   # what _run_helper returns after a timeout
        return {"results": [{"path": p, "ok": sc.send_ok} for p in args[2:]]}

    monkeypatch.setattr(microrave.RelayController, "_run_helper", fake_run)
    monkeypatch.setattr(microrave, "RELAY_RESCAN_INTERVAL", 0.2)
    return sc


class TestRescanBackoff:

    def test_missing_board_is_not_rescanned_on_every_command(self, scripted):
        relay = microrave.RelayController()
        for _ in range(5):
            relay.all_on()
        assert scripted.scans() == 1   # just the startup scan

    def test_missing_board_is_rescanned_after_the_interval(self, scripted):
        relay = microrave.RelayController()
        time.sleep(0.25)
        relay.all_on()
        assert scripted.scans() == 2

    def test_board_that_appears_later_is_picked_up_and_used(self, scripted):
        relay = microrave.RelayController()
        scripted.boards = ["1-2:1.0"]
        time.sleep(0.25)
        relay.all_on()
        relay.all_off()
        relay.all_on()
        assert scripted.scans() == 2   # found, so no further scans
        sends = [c for c in scripted.calls if c[0] == "send"]
        assert len(sends) == 3
        assert sends[0][2] == "1-2:1.0"

    def test_found_board_is_never_rescanned(self, scripted):
        scripted.boards = ["1-2:1.0"]
        relay = microrave.RelayController()
        for _ in range(6):
            relay.all_on()
            relay.all_off()
        assert scripted.scans() == 1

    def test_lost_board_is_rescanned_immediately_then_backs_off(self, scripted):
        scripted.boards = ["1-2:1.0"]
        relay = microrave.RelayController()
        scripted.boards = []
        scripted.send_ok = False
        relay.all_on()          # send fails, path dropped
        relay.all_on()          # first command after the loss rescans at once
        assert scripted.scans() == 2
        relay.all_on()
        relay.all_on()
        assert scripted.scans() == 2   # empty scan -> backed off

    def test_hung_send_drops_the_path_and_rescans_next_time(self, scripted):
        scripted.boards = ["1-2:1.0"]
        relay = microrave.RelayController()
        scripted.send_hangs = True
        relay.all_on()
        assert relay._paths == []
        scripted.send_hangs = False
        relay.all_on()
        assert scripted.scans() == 2


# ── The real relay_helper.py, against a fake hid module ───────────────────────

@pytest.fixture
def fake_hid(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", FAKE_HID_DIR)
    monkeypatch.setenv("FAKE_HID_LOG", str(tmp_path / "reports.txt"))
    monkeypatch.setattr(microrave, "RELAY_RESCAN_INTERVAL", 0.2)

    def mode(name):
        monkeypatch.setenv("FAKE_HID_MODE", name)
    mode("present")
    mode.log = tmp_path / "reports.txt"
    return mode


class TestRelayHelperProcess:

    def test_commands_reach_the_board(self, fake_hid):
        relay = microrave.RelayController()
        assert relay._paths == ["1-2:1.0"]
        relay.all_on()
        relay.all_off()
        reports = fake_hid.log.read_text().split()
        assert reports == ["00fe" + "00" * 7, "00fc" + "00" * 7]

    def test_no_board_present(self, fake_hid):
        fake_hid("absent")
        relay = microrave.RelayController()
        assert relay._paths == []
        assert not relay._disabled

    def test_failed_open_drops_the_path(self, fake_hid):
        relay = microrave.RelayController()
        fake_hid("open_fail")
        relay.all_on()
        assert relay._paths == []

    def test_missing_hidapi_disables_the_controller(self, fake_hid):
        fake_hid("missing")
        relay = microrave.RelayController()
        assert relay._disabled
        relay.all_on()   # a safe no-op

    def test_a_hung_board_cannot_block_past_the_deadline(self, fake_hid, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_SEND_TIMEOUT", 0.8)
        relay = microrave.RelayController()
        fake_hid("hang_open")

        for _ in range(2):   # every call is bounded, not just the first
            t0 = time.monotonic()
            relay.all_on()
            assert time.monotonic() - t0 < 2.5
            assert relay._paths == []
            assert relay._stuck is not None
            relay._stuck.wait(timeout=5)   # the hung helper was killed and reaps

    def test_unkillable_helper_makes_later_calls_fail_fast(self, fake_hid, monkeypatch):
        relay = microrave.RelayController()

        class StuckProc:
            def poll(self):
                return None   # still alive, e.g. stuck uninterruptibly in the kernel

        def no_new_helpers(*a, **k):
            raise AssertionError("must not start another helper while one is stuck")

        relay._stuck = StuckProc()
        monkeypatch.setattr(microrave.subprocess, "Popen", no_new_helpers)
        t0 = time.monotonic()
        relay.all_on()
        assert time.monotonic() - t0 < 0.2


# ── Stall tracer ──────────────────────────────────────────────────────────────

class TestStallTracer:

    def test_dumps_stacks_when_the_loop_stops_ticking(self, tmp_path):
        path = tmp_path / "stall.log"
        tracer = microrave.StallTracer(str(path), 0.3, enable_fatal=False)
        tracer.pet()
        time.sleep(0.8)   # no pet() -> the dump fires
        tracer.stop()
        assert "most recent call first" in path.read_text()

    def test_silent_while_the_loop_keeps_ticking(self, tmp_path):
        path = tmp_path / "stall.log"
        tracer = microrave.StallTracer(str(path), 0.4, enable_fatal=False)
        for _ in range(8):
            tracer.pet()
            time.sleep(0.1)
        tracer.stop()
        assert path.read_text() == ""

    def test_unwritable_path_disables_quietly(self, tmp_path):
        tracer = microrave.StallTracer(str(tmp_path / "no" / "dir" / "x.log"), 0.3,
                                       enable_fatal=False)
        tracer.pet()
        tracer.mark("nothing happens")
        tracer.stop()


# ── USB presence (sysfs) ──────────────────────────────────────────────────────

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
