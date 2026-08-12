"""Invariants that keep the suite's result independent of file ordering.

Every test file imports the integration through ``custom_components.cap_alerts``,
so each module is executed once and there is one module object per file. That
was not always true: files used to load modules a second time by path, under a
synthetic ``cap_alerts.<name>`` package, so provider tests could run without
Home Assistant. Two bugs came out of it, both order-dependent (#136), and the
scheme was retired in #137.

``config_flow.py`` is the one that bites hardest, because it is not inert on
import: ``class CAPAlertsFlowHandler(ConfigFlow, domain=DOMAIN)`` registers
itself in Home Assistant's process-global ``config_entries.HANDLERS``. A second
copy re-registers a different class for the same domain and the file imported
last at collection time wins. When the copy wins, every flow the suite starts
runs code that no ``custom_components.cap_alerts.config_flow`` patch can reach:
a test that patched its fetch makes real HTTP calls instead, in some file
orderings and not others. These assertions fail immediately and say why.
"""

from __future__ import annotations

import sys


def test_the_registered_flow_handler_is_the_real_module_s():
    from homeassistant.config_entries import HANDLERS

    from custom_components.cap_alerts.config_flow import CAPAlertsFlowHandler

    assert HANDLERS["cap_alerts"] is CAPAlertsFlowHandler


def test_no_integration_module_is_loaded_twice():
    """The registry check above only catches a copy that registered *last*.
    This catches a second copy of anything, whichever order it was imported in.
    """
    duplicates = [name for name in sys.modules if name.startswith("cap_alerts")]
    assert duplicates == [], duplicates
