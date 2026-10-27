"""U.S. Tsunami Warning Center conventions: the cancellation bulletin.

One source's interpretive rules, exposed to the provider as ``CONVENTIONS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..const import REMOVAL_REASON_ENDED
from ..conventions import SourceConventions

# What the provider writes into ``lifecycle_status`` for a bulletin that ends
# its event. The centers end a product with a ``Tsunami Cancellation`` sent as
# ``msgType=Update`` (urgency Past, VTEC ``CAN``), which ``normalize`` would
# otherwise read as a live update.
TWC_CANCELLED = "cancelled"

# Geocodes are withheld from the options flow: the warning bulletins carry UGC
# zones, but information statements and cancellations carry none, and on a
# quiet day every slot holds one of those. The coordinator's prefix filter
# would read that as a source with no geocodes and fail the entry (#298).
# Severity is CAP's own, which the domestic products fill in properly; the
# shared ``tsunami`` needle in ``icons.py`` covers every event name.
TWC_CONVENTIONS = SourceConventions(
    lifecycle_removal_reasons=MappingProxyType({TWC_CANCELLED: REMOVAL_REASON_ENDED}),
    publishes_geocodes=False,
)


# What this provider declares, keyed as ``conventions_for`` resolves it.
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "twc": TWC_CONVENTIONS,
    }
)
