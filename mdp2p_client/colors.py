"""ANSI color utilities for the MDP2P terminal interface."""

import os
import sys


def _supports_color() -> bool:
    """Detect if the terminal supports ANSI colors."""
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_COLOR_ENABLED = _supports_color()


def _code(code: str) -> str:
    """Return ANSI code if colors are enabled, else empty string."""
    return code if _COLOR_ENABLED else ""


# Styles
RESET = _code("\033[0m")
BOLD = _code("\033[1m")
DIM = _code("\033[2m")

# Foreground colors
RED = _code("\033[31m")
GREEN = _code("\033[32m")
YELLOW = _code("\033[33m")
BLUE = _code("\033[34m")
MAGENTA = _code("\033[35m")
CYAN = _code("\033[36m")

# Bright foreground
BRIGHT_RED = _code("\033[91m")
BRIGHT_GREEN = _code("\033[92m")
BRIGHT_YELLOW = _code("\033[93m")
BRIGHT_BLUE = _code("\033[94m")
BRIGHT_CYAN = _code("\033[96m")
