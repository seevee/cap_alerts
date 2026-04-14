"""Alert provider protocol and factory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import aiohttp

from ..model import CAPAlert


class AlertProvider(Protocol):
    """Fetches alerts from a single weather service and returns CAPAlert objects."""

    @property
    def name(self) -> str:
        """Provider identifier for CAPAlert.provider field (e.g. 'nws', 'eccc')."""
        ...

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
    ) -> list[CAPAlert]:
        """Fetch current alerts. Raises UpdateFailed on transient errors."""
        ...


def get_provider(provider_id: str) -> AlertProvider:
    """Return a provider instance by ID."""
    from .eccc import ECCCProvider
    from .nws import NWSProvider

    providers: dict[str, type] = {
        "nws": NWSProvider,
        "eccc": ECCCProvider,
    }
    cls = providers.get(provider_id)
    if cls is None:
        raise ValueError(f"Unknown provider: {provider_id}")
    return cls()
