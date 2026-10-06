"""The provider-neutral half stays neutral (issue #216).

The modules that would move into a hub — the model, normalization, the
convention mechanism, icons, the store, the payload budget, the geometry store,
the entity platform and the coordinator — dispatch on nothing provider-shaped.
A provider is a module under ``providers/`` plus a convention row it registers;
adding one touches none of the modules below.

The gate is scoped to *dispatch*: string literals naming a provider, identifiers
carrying a provider prefix, and imports reaching into a provider module.
Docstrings and comments are free to say where a number was measured, and
``CAPAlert`` keeps the field names it publishes (``vtec``, ``event_code_nws``)
because those are attribute names on the wire.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from custom_components.cap_alerts.conventions import CONVENTIONS
from custom_components.cap_alerts.providers import PROVIDER_IDS, get_provider

PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "cap_alerts"

NEUTRAL_MODULES = (
    "model.py",
    "normalize.py",
    "conventions.py",
    "icons.py",
    "store.py",
    "payload.py",
    "geometry_store.py",
    "sensor.py",
    "views.py",
    "websocket.py",
    "coordinator.py",
)

_PROVIDER_IDS = frozenset(PROVIDER_IDS)

# Provider ids, sender nicknames, and the transports and calendars that belong
# to one source. An identifier starting with one of these is a source leaking
# into shared code.
_PROVIDER_PREFIX = re.compile(
    r"^_?(nws|eccc|meteoalarm|meteofrance|fmi|wmo|gdacs|bbk|au|naad|dwd|tmd|buddhist)_",
    re.IGNORECASE,
)

# The provider package itself (protocols, the factory, the exception) and the
# shared body cache are neutral infrastructure that happens to live there.
_ALLOWED_PROVIDER_IMPORTS = frozenset({".providers", ".providers.cap_content_cache"})


def _module(name: str) -> ast.Module:
    return ast.parse((PACKAGE / name).read_text(), filename=name)


def _is_docstring(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    return isinstance(parent, ast.Expr) and isinstance(node, ast.Constant)


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


@pytest.mark.parametrize("name", NEUTRAL_MODULES)
def test_neutral_module_names_no_provider_in_code(name: str):
    tree = _module(name)
    parents = _parents(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _PROVIDER_IDS and not _is_docstring(node, parents):
                offenders.append(f"{name}:{node.lineno} literal {node.value!r}")
        elif isinstance(node, ast.Name) and _PROVIDER_PREFIX.match(node.id):
            offenders.append(f"{name}:{node.lineno} name {node.id}")
        elif isinstance(node, ast.Attribute) and _PROVIDER_PREFIX.match(node.attr):
            offenders.append(f"{name}:{node.lineno} attribute {node.attr}")
        elif isinstance(node, ast.alias) and _PROVIDER_PREFIX.match(node.name):
            offenders.append(f"{name}:{node.lineno} import {node.name}")
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize("name", NEUTRAL_MODULES)
def test_neutral_module_imports_no_provider_module(name: str):
    tree = _module(name)
    offenders = [
        f"{name}:{node.lineno} from {'.' * node.level}{node.module or ''} import …"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and (node.module or "").startswith("providers")
        and f".{node.module}" not in _ALLOWED_PROVIDER_IMPORTS
    ]
    assert offenders == [], "\n".join(offenders)


def test_providers_raise_their_own_exception():
    """No provider module imports Home Assistant's ``UpdateFailed``.

    The coordinator translates ``ProviderError`` in one place; a provider that
    reached for the coordinator's exception would tie itself to this host.
    """
    offenders = []
    for path in sorted((PACKAGE / "providers").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=path.name)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "homeassistant.helpers.update_coordinator"
            ):
                offenders.append(f"providers/{path.name}:{node.lineno}")
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize("provider_id", PROVIDER_IDS)
def test_every_shipped_provider_declares_its_own_row(provider_id: str):
    """A provider's rows carry a row under its own id.

    ``conventions_for`` returns the empty row for an unknown source by design,
    so a provider that declared rows under the wrong key would fail silently —
    severity, icons and absence policy would all fall back to pure CAP.
    """
    rows = get_provider(provider_id).conventions
    assert provider_id in rows
    assert all(key == provider_id or key.startswith(f"{provider_id}/") for key in rows)


def test_rows_in_force_cover_every_shipped_provider():
    """What ``conftest`` registers is what a coordinator would: every id resolves."""
    assert _PROVIDER_IDS <= set(CONVENTIONS)
