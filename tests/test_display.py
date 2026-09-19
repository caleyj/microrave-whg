"""
Structural regression tests for Display's glyph-caching strategy.

An earlier version cached rendered surfaces by the whole 4-character string.
That's a 100% cache miss on every tick of any countdown (the string is
different every second), which did a full font render + glow blur on the
main thread each time — the actual cause of "main loop stall" warnings.
These tests guard against that coming back, using the mocked pygame from
conftest.py (no real rendering, just checking the object shape).
"""
from microrave import Display


class TestGlyphCache:

    def test_builds_a_cell_for_every_digit_and_blank(self):
        d = Display()
        assert set(d._digit_cell.keys()) == set("0123456789 ")

    def test_builds_both_colon_states(self):
        d = Display()
        assert set(d._colon_cell.keys()) == {True, False}

    def test_no_leftover_whole_string_cache(self):
        # The old per-string cache attribute must be gone, not just unused —
        # its presence would mean the miss-every-tick path is still reachable.
        d = Display()
        assert not hasattr(d, "_cache")
        assert not hasattr(d, "_value_surface")

    def test_render_does_not_grow_any_cache(self):
        """Showing a run of distinct values (as a countdown does, once a
        second) must not allocate new surfaces — everything it needs was
        already built in __init__."""
        d = Display()
        before = (dict(d._digit_cell), dict(d._colon_cell))
        for secs in range(20, 0, -1):
            m, s = divmod(secs, 60)
            d.show("%02d%02d" % (m, s), colon=(secs % 2 == 0))
            d.render()
        assert d._digit_cell.keys() == before[0].keys()
        assert d._colon_cell.keys() == before[1].keys()
