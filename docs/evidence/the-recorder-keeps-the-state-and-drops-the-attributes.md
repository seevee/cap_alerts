# The recorder keeps the state and drops every attribute on overflow

| | |
| :-- | :-- |
| Supports | RFC §1.1 |
| Source | `homeassistant/components/recorder/db_schema.py`, Home Assistant 2026.9.3 |
| Sample | the code path, not a feed |
| Observed | 2026-10-03 (first checked against 2026.7.3 on 2026-08-07) |
| Reproduce | `grep -n "MAX_STATE_ATTRS_BYTES" .venv/lib/python*/site-packages/homeassistant/components/recorder/db_schema.py` |

```python
MAX_STATE_ATTRS_BYTES = 16384
…
    def shared_attrs_bytes_from_event(
        event: Event[EventStateChangedData],
        dialect: SupportedDialect | None,
    ) -> bytes:
        """Create shared_attrs from a state_changed event."""
        …
        else:
            exclude_attrs = ALL_DOMAIN_EXCLUDE_ATTRS
        encoder = json_bytes_strip_null if dialect == PSQL_DIALECT else json_bytes
        bytes_result = encoder(
            {k: v for k, v in state.attributes.items() if k not in exclude_attrs}
        )
        if len(bytes_result) > MAX_STATE_ATTRS_BYTES:
            _LOGGER.warning(
                "State attributes for %s exceed maximum size of %s bytes. "
                "This can cause database performance issues; Attributes "
                "will not be stored",
                state.entity_id,
                MAX_STATE_ATTRS_BYTES,
            )
            return b"{}"
        return bytes_result
```

| Step | What happens |
| :-- | :-- |
| Measure | `state.attributes` minus `ALL_DOMAIN_EXCLUDE_ATTRS` and the entity's own `unrecorded_attributes`, serialized with the recorder's JSON encoder |
| Over 16,384 bytes | one warning per state write, `{}` returned in place of the payload |
| State row | committed as normal, with the state value intact |
| History | keeps the state, loses every attribute, with nothing marking the row |

## Reading

The ceiling is not a write failure, which is why it goes unnoticed. A
packed-attribute alert sensor keeps its count in history and loses the alerts
the count was about. The measured set is smaller than `to_attributes()`
returns, which is what lets `payload.py` declare `parameters` and `geocodes`
unrecorded and take them out of the bound (§7.2).

## Caveats

- Line numbers move between releases. The constant and the warning text have
  been stable from 2026.7.3 to 2026.9.3.
- The PostgreSQL dialect strips nulls before measuring, so the same state can
  measure a few bytes smaller there.
