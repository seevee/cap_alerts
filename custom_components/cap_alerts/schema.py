"""The schema library, under one name.

Home Assistant 2026.9 replaced voluptuous with probatio, a clean-room rewrite
with the same public API, and 2026.10 types ``data_schema`` and the websocket
command schema as probatio's. Core aliases ``voluptuous`` to probatio at
startup, so the old import keeps working at runtime; what it cannot do is
satisfy mypy, which sees voluptuous's own types against core's probatio
annotations. Nothing else in the package names either library.
"""

from __future__ import annotations

import probatio as vol

__all__ = ["vol"]
