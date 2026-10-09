"""RelayController (background worker + helper process), the stall tracer,
and USB presence."""
import os
import time

import pytest

import microrave

FAKE_HID_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_hid")
ON, OFF = microrave.RelayController._ALL_ON, microrave.RelayController._ALL_OFF


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def fast(monkeypatch):
    """Short timers so the worker's retry/reassert behaviour can be observed."""
    for name, value in dict(RELAY_RESCAN_INTERVAL=0.2, RELAY_RETRY_BASE=0.05,
                            RELAY_RETRY_MAX=0.2, RELAY_REASSERT_SECONDS=0.15,
                            RELAY_RECOVER_SETTLE=0.0, RELAY_RECOVER_MIN_GAP=5.0, RELAY_RECOVER_AFTER=0.05,
                            RELAY_CLOSE_WAIT=0.3, RELAY_OFF_HOLD=0.02,
                            RELAY_SETTLE_AFTER_OFF=0.02, RELAY_CHANNELS=None).items():
        monkeypatch.setattr(microrave, name, value)


@pytest.fixture
def make_relay(request):
    def make(**kw):
        relay = microrave.RelayController(**kw)
        request.addfinalizer(relay.close if kw.get("start_worker", True) else (lambda: None))
        return relay
    return make


@pytest.fixture
def scripted(monkeypatch, fast):
    """Stub out the helper process; the script says what the board does."""
    class Script:
        def __init__(self):
            self.calls = []
            self.boards = []
            self.send_ok = True
            self.send_hangs = False   # _run_helper returns None, as after a timeout
            self.fail_sends = 0       # fail this many sends, then behave
            self.delay = 0.0
            self.times = []           # (command, monotonic time) of each send

        def scans(self):
            return sum(1 for c in self.calls if c[0] == "scan")

        def sends(self):
            return [c[1] for c in self.calls if c[0] == "send"]

    sc = Script()

    def fake_run(self, *args):
        sc.calls.append(args)
        if args[0] == "scan":
            return {"boards": [{"path": p, "product": "USBRelay4", "serial": ""}
                               for p in sc.boards]}
        sc.times.append((args[1], time.monotonic()))
        time.sleep(sc.delay)
        if sc.send_hangs:
            return None
        if sc.fail_sends > 0:
            sc.fail_sends -= 1
            return None
        paths = args[3:] if args[0] == "sendch" else args[2:]
        return {"results": [{"path": p, "ok": sc.send_ok} for p in paths]}

    monkeypatch.setattr(microrave.RelayController, "_run_helper", fake_run)
    return sc


# ── _apply: scan/backoff bookkeeping, run synchronously ───────────────────────

class TestApplyLogic:

    def test_missing_board_is_not_rescanned_within_the_interval(self, scripted, make_relay):
        relay = make_relay(start_worker=False)
        for _ in range(5):
            assert relay._apply(ON) is False
        assert scripted.scans() == 1
        time.sleep(0.25)
        relay._apply(ON)
        assert scripted.scans() == 2

    def test_found_board_is_never_rescanned(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay(start_worker=False)
        for _ in range(6):
            assert relay._apply(ON) is True
            assert relay._apply(OFF) is True
        assert scripted.scans() == 1

    def test_lost_board_is_rescanned_immediately_then_backs_off(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay(start_worker=False)
        assert relay._apply(ON) is True
        scripted.boards = []
        scripted.send_ok = False
        assert relay._apply(ON) is False    # send fails, path dropped
        assert relay._apply(ON) is False    # next attempt rescans at once...
        assert scripted.scans() == 2
        relay._apply(ON)
        relay._apply(ON)
        assert scripted.scans() == 2        # ...and an empty scan backs off

    def test_hung_send_drops_the_path_and_rescans_next_time(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay(start_worker=False)
        scripted.send_hangs = True
        assert relay._apply(ON) is False
        assert relay._paths == []
        scripted.send_hangs = False
        assert relay._apply(ON) is True
        assert scripted.scans() == 2


# ── The worker: app calls never wait, state converges ─────────────────────────

class TestWorker:

    def test_calls_return_immediately_even_when_the_board_is_slow(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        scripted.delay = 0.5
        relay = make_relay()
        t0 = time.monotonic()
        relay.all_on()
        relay.all_off()
        relay.all_on()
        assert time.monotonic() - t0 < 0.1

    def test_latest_request_wins(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        scripted.delay = 0.1
        relay = make_relay()
        for fn in (relay.all_on, relay.all_off, relay.all_on, relay.all_off):
            fn()
        assert wait_for(lambda: relay._applied == OFF)
        assert scripted.sends()[-1] == OFF

    def test_retries_until_the_board_answers(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        scripted.fail_sends = 2
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON)
        assert len(scripted.sends()) >= 3

    def test_lamp_turns_on_when_the_board_returns_mid_countdown(self, scripted, make_relay):
        relay = make_relay()
        relay.all_on()
        time.sleep(0.3)
        assert relay._applied is None       # board still missing
        scripted.boards = ["1-2:1.0"]
        assert wait_for(lambda: relay._applied == ON)

    def test_on_is_reasserted_while_counting_down(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: scripted.sends().count(ON) >= 3)

    def test_off_is_not_resent_when_nothing_changed(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)
        n = len(scripted.sends())
        relay.all_off()
        time.sleep(0.3)
        assert len(scripted.sends()) == n

    def test_close_switches_off_and_stops_the_worker(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON)
        relay.close()
        assert scripted.sends()[-1] == OFF
        assert wait_for(lambda: not relay._thread.is_alive())

    def test_close_does_not_hang_when_there_is_no_board(self, scripted, make_relay):
        relay = make_relay()
        relay.all_off()
        time.sleep(0.1)
        t0 = time.monotonic()
        relay.close()
        assert time.monotonic() - t0 < 0.2


# ── Channel selection, OFF hold-back and the post-OFF quiet period ────────────

class TestGentleSwitching:

    def test_all_channels_by_default(self, scripted, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay(start_worker=False)
        relay._apply(ON)
        assert scripted.calls[-1] == ("send", ON, "1-2:1.0")

    def test_only_the_configured_channels_are_switched(self, scripted, make_relay, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_CHANNELS", (1,))
        scripted.boards = ["1-2:1.0"]
        relay = make_relay(start_worker=False)
        relay._apply(ON)
        assert scripted.calls[-1] == ("sendch", 0xFF, "1", "1-2:1.0")
        relay._apply(OFF)
        assert scripted.calls[-1] == ("sendch", 0xFD, "1", "1-2:1.0")
        monkeypatch.setattr(microrave, "RELAY_CHANNELS", (1, 3))
        relay._apply(ON)
        assert scripted.calls[-1] == ("sendch", 0xFF, "1,3", "1-2:1.0")

    def test_a_quick_stop_then_start_never_sends_the_off(self, scripted, make_relay, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_OFF_HOLD", 0.3)
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON)
        before = len(scripted.sends())
        relay.all_off()
        time.sleep(0.05)
        relay.all_on()
        time.sleep(0.5)
        assert OFF not in scripted.sends()[before:]
        assert relay._applied == ON

    def test_an_off_is_still_sent_when_nothing_cancels_it(self, scripted, make_relay, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_OFF_HOLD", 0.2)
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON)
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)

    def test_the_board_is_left_alone_after_an_off(self, scripted, make_relay, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_SETTLE_AFTER_OFF", 0.4)
        monkeypatch.setattr(microrave, "RELAY_REASSERT_SECONDS", 60.0)
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON)
        off_t = next(t for cmd, t in scripted.times if cmd == OFF)
        on_t = next(t for cmd, t in scripted.times if cmd == ON)
        assert on_t - off_t >= 0.35


# ── Stuck-board recovery (uhubctl port reset) ─────────────────────────────────

class TestPortRecovery:

    @pytest.fixture
    def uhubctl(self, monkeypatch):
        runs = []
        monkeypatch.setattr(microrave.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(microrave.subprocess, "run",
                            lambda cmd, **kw: runs.append(cmd))
        return runs

    def test_split_usb_port(self):
        assert microrave._split_usb_port("1-2") == ("1", "2")
        assert microrave._split_usb_port("1-2.3") == ("1-2", "3")

    def test_stuck_board_gets_its_port_reset_once(self, scripted, uhubctl, make_relay):
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)
        scripted.send_hangs = True
        relay.all_on()
        assert wait_for(lambda: len(uhubctl) >= 1)
        assert uhubctl[0][:7] == ["/usr/bin/uhubctl", "-l", "1", "-p", "2", "-a", "cycle"]
        time.sleep(0.4)                     # many retries, but rate-limited
        assert len(uhubctl) == 1

    def test_the_kernel_gets_a_grace_period_before_any_reset(self, scripted, uhubctl,
                                                            make_relay, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_RECOVER_AFTER", 0.8)
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)
        scripted.send_hangs = True
        relay.all_on()
        time.sleep(0.5)
        assert uhubctl == []                    # still inside the grace period
        assert wait_for(lambda: len(uhubctl) == 1, timeout=3)

    def test_a_recovered_board_resets_the_outage_clock(self, scripted, uhubctl,
                                                      make_relay, monkeypatch):
        monkeypatch.setattr(microrave, "RELAY_RECOVER_AFTER", 0.8)
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)
        scripted.send_hangs = True
        relay.all_on()
        time.sleep(0.3)
        scripted.send_hangs = False             # the kernel recovered it
        assert wait_for(lambda: relay._applied == ON)
        assert relay._outage_since is None
        assert uhubctl == []

    def test_no_reset_for_a_board_that_was_never_seen(self, scripted, uhubctl, make_relay):
        relay = make_relay()
        relay.all_on()
        time.sleep(0.4)
        assert uhubctl == []

    def test_missing_uhubctl_is_harmless(self, scripted, monkeypatch, make_relay):
        monkeypatch.setattr(microrave.shutil, "which", lambda name: None)
        scripted.boards = ["1-2:1.0"]
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF)
        scripted.send_hangs = True
        relay.all_on()
        assert wait_for(lambda: relay._failures >= 1)


# ── The real relay_helper.py, against a fake hid module ───────────────────────

@pytest.fixture
def fake_hid(monkeypatch, tmp_path, fast):
    monkeypatch.setenv("PYTHONPATH", FAKE_HID_DIR)
    monkeypatch.setenv("FAKE_HID_LOG", str(tmp_path / "reports.txt"))

    def mode(name):
        monkeypatch.setenv("FAKE_HID_MODE", name)
    mode("present")
    mode.log = tmp_path / "reports.txt"
    return mode


class TestRelayHelperProcess:

    def test_commands_reach_the_board(self, fake_hid, make_relay):
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON, timeout=5)
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF, timeout=5)
        reports = fake_hid.log.read_text().split()
        assert reports[0] == "00fe" + "00" * 7
        assert reports[-1] == "00fc" + "00" * 7

    def test_configured_channels_reach_the_board(self, fake_hid, monkeypatch, make_relay):
        monkeypatch.setattr(microrave, "RELAY_CHANNELS", (2,))
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._applied == ON, timeout=5)
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF, timeout=5)
        reports = fake_hid.log.read_text().split()
        assert reports[0] == "00ff02" + "00" * 6
        assert reports[-1] == "00fd02" + "00" * 6

    def test_channel_test_command_clicks_each_channel(self, fake_hid):
        import subprocess, sys
        out = subprocess.run([sys.executable, microrave.RELAY_HELPER, "test", "1-2:1.0", "0.01"],
                             capture_output=True, text=True, timeout=10).stdout
        assert out.strip().endswith("done")
        for ch in (1, 2, 3, 4):
            assert "channel %d ON" % ch in out and "channel %d OFF" % ch in out
        reports = fake_hid.log.read_text().split()
        assert reports == [("00ff0%d" % ch) + "00" * 6 if on else ("00fd0%d" % ch) + "00" * 6
                           for ch in (1, 2, 3, 4) for on in (True, False)]

    def test_no_board_present(self, fake_hid, make_relay):
        fake_hid("absent")
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._failures >= 1, timeout=5)
        assert relay._paths == [] and not relay._disabled

    def test_failed_open_leaves_the_state_unconfirmed(self, fake_hid, make_relay):
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF, timeout=5)
        fake_hid("open_fail")
        relay.all_on()
        assert wait_for(lambda: relay._failures >= 1, timeout=5)
        assert relay._applied is None

    def test_missing_hidapi_disables_the_controller(self, fake_hid, make_relay):
        fake_hid("missing")
        relay = make_relay()
        relay.all_on()
        assert wait_for(lambda: relay._disabled, timeout=5)

    def test_a_hung_board_never_delays_the_caller(self, fake_hid, monkeypatch, make_relay):
        monkeypatch.setattr(microrave, "RELAY_SEND_TIMEOUT", 0.8)
        relay = make_relay()
        relay.all_off()
        assert wait_for(lambda: relay._applied == OFF, timeout=5)
        fake_hid("hang_open")

        t0 = time.monotonic()
        relay.all_on()
        assert time.monotonic() - t0 < 0.1          # the app is never held up
        assert wait_for(lambda: relay._stuck is not None, timeout=5)
        relay._stuck.wait(timeout=5)                # the hung helper was killed and reaps

    def test_unkillable_helper_makes_later_calls_fail_fast(self, monkeypatch, make_relay):
        relay = make_relay(start_worker=False)

        class StuckProc:
            def poll(self):
                return None   # still alive, e.g. stuck uninterruptibly in the kernel

        def no_new_helpers(*a, **k):
            raise AssertionError("must not start another helper while one is stuck")

        relay._stuck = StuckProc()
        monkeypatch.setattr(microrave.subprocess, "Popen", no_new_helpers)
        t0 = time.monotonic()
        assert relay._run_helper("scan") is None
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
