"""Invariants that keep the suite's result independent of file ordering.

Several test files load an integration module a second time, by path, under a
synthetic ``cap_alerts.<name>`` package — that is how the provider tests run
without Home Assistant installed (see ``conftest``). It is safe only for
modules that are inert on import.

``config_flow.py`` is not one of them: ``class CAPAlertsFlowHandler(ConfigFlow,
domain=DOMAIN)`` registers itself in Home Assistant's process-global
``config_entries.HANDLERS``, so a second copy re-registers a different class
for the same domain and the last file imported at collection time wins. When
the copy wins, every flow the suite starts runs code that no
``custom_components.cap_alerts.config_flow`` patch can reach — a test that
patched its fetch makes real HTTP calls instead, and only under some file
orderings. These assertions fail immediately and say why.
"""

from __future__ import annotations

import sys

import pytest

from tests.conftest import REAL_HA

pytestmark = pytest.mark.skipif(
    not REAL_HA, reason="stub mode has no HANDLERS registry to hijack"
)


def test_the_registered_flow_handler_is_the_real_module_s():
    from homeassistant.config_entries import HANDLERS

    from custom_components.cap_alerts.config_flow import CAPAlertsFlowHandler

    assert HANDLERS["cap_alerts"] is CAPAlertsFlowHandler


def test_config_flow_is_never_loaded_a_second_time():
    """The registry check above only catches a copy that registered *last*.
    This catches one that exists at all, whichever order it was imported in."""
    assert "cap_alerts.config_flow" not in sys.modules
