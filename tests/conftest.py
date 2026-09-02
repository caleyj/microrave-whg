"""
Hardware mocks and pytest fixtures for MicroRave tests.

Mocks are installed at module scope so they are in place before any test file
imports microrave (which has a top-level `import pygame`).
"""
import sys
import os
import tempfile
from unittest.mock import MagicMock
import pytest

# ── pygame mock ────────────────────────────────────────────────────────────────
_pygame = MagicMock()

_pygame.FULLSCREEN = 0x00000001
_pygame.NOFRAME    = 0x00000020
_pygame.QUIT       = 256
_pygame.KEYDOWN    = 768
_pygame.K_ESCAPE   = 27
_pygame.USEREVENT  = 24   # must be int so USEREVENT + 1 works
_pygame.error      = Exception   # so `except pygame.error:` is a valid clause

_display_info            = MagicMock()
_display_info.current_w  = 1920
_display_info.current_h  = 1080
_pygame.display.Info.return_value     = _display_info
_pygame.display.set_mode.return_value = MagicMock()
_pygame.event.get.return_value        = []
_pygame.mixer.Channel.return_value    = MagicMock()
_pygame.mixer.Sound.return_value      = MagicMock()

# Font mock: .size() must return a real (w, h) tuple and .get_height() an int
# so Display._fit_font can do arithmetic on them.
_font = MagicMock()
_font.size.return_value       = (1200, 400)
_font.get_height.return_value = 400
_font.render.return_value     = MagicMock()
_pygame.font.Font.return_value = _font
_pygame.font.init             = lambda: None

# Install before any test file is imported
sys.modules['pygame'] = _pygame

# ── Now safe to import microrave ──────────────────────────────────────────────
import microrave as _mr

# hidapi is not installed on the test host, so RelayController self-disables
# (_HID_AVAILABLE is False). Its all_on()/all_off()/close() are then safe no-ops.

# Redirect playcounts.json to /tmp so tests don't need write access to the
# production file (which may be root-owned when the systemd service has run).
_mr.PLAYCOUNTS_FILE = os.path.join(tempfile.gettempdir(), "mr_test_playcounts.json")


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    """
    Create a MicroRaveApp, drain the init queue, yield it, then shut down cleanly.
    Tests get a freshly initialised app in IDLE state.
    """
    a = _mr.MicroRaveApp()
    a._drain()
    yield a
    a._shutdown()
    if a._dispatch_thread and a._dispatch_thread.is_alive():
        a._dispatch_thread.join(timeout=2.0)


@pytest.fixture
def pygame_mock():
    return _pygame
