"""ASCII art logo, version constant, and shared color constants.

Used across CLI, TUI, and web modules so the visual identity stays consistent.
The version bump to 0.2.0 marks the introduction of dynamic visualization
(Textual TUI + Cytoscape.js web mode).
"""
from __future__ import annotations

VERSION = "0.3.0"

LOGO = r"""
                    _ _       _
   ___ _   _  __| (_)_   _(_)____
  / __| | | |/ _` | \ \ / / |_  /
  \__ \ |_| | (_| | |\ V /| |/ /
  |___/\__,_|\__,_|_| \_/ |_/___|

  X-ray vision for your cloud infrastructure
"""

TAGLINE = "X-ray vision for your cloud infrastructure"


# Shared color palette — kept in one place so terminal/web/PNG outputs match.
class Colors:
    """Centralized color constants used in every output mode."""

    # Health states
    HEALTHY = "#22c55e"      # green-500
    WARNING = "#eab308"      # yellow-500
    UNHEALTHY = "#ef4444"    # red-500
    UNREACHABLE = "#6b7280"  # gray-500
    ORPHAN = "#dc2626"       # red-600 (reserved for unattached resources)

    # Rich/Textual style strings
    RICH_HEALTHY = "green"
    RICH_WARNING = "yellow"
    RICH_UNHEALTHY = "red"
    RICH_UNREACHABLE = "grey50"
    RICH_ORPHAN = "bold red"

    # Edge styles
    EDGE_NORMAL = "solid"
    EDGE_ORPHAN = "dashed"


def banner() -> str:
    """Return logo + version line for CLI startup."""
    return f"{LOGO}\n  v{VERSION}\n"
