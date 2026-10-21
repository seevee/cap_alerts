"""The schema library, under one name on every core this integration supports.

Home Assistant 2026.9 replaced voluptuous with probatio, a clean-room rewrite
with the same public API, and 2026.10 types ``data_schema`` and the websocket
command schema as probatio's. Core aliases ``voluptuous`` to probatio at
startup, so the old import keeps working at runtime; what it cannot do is
satisfy mypy, which sees voluptuous's own types against core's probatio
annotations. The HACS floor (2026.4.3) ships voluptuous only.

So: probatio where it exists, voluptuous where it doesn't, and probatio's
types for the checker. Nothing else in the package names either library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import probatio as vol
else:
    try:
        import probatio as vol
    except ImportError:  # pragma: no cover - core before 2026.9
        import voluptuous as vol

__all__ = ["vol"]
