# Roadmap

Future work tracked for context — none of these are in scope until prioritized.

For the design of shipped behavior, see [`architecture.md`](architecture.md).

---

## Idempotent merge logic (AlertStore)

The current store diffs by ID presence — if a provider recomputes IDs differently across message replacements, it causes entity churn (remove + re-create). The shipped WMO provider sidesteps this by hashing the sender-scoped CAP `<identifier>` (stable across Update/Cancel) and resolving revision chains, so it does not currently churn. The fallback below is still wanted for sources that do full-message replacement *without* a stable identifier or explicit Cancel.

**Proposed**: merge-on-content-match as a fallback when IDs change but the event is the same (same `event` + `area_desc` + close `sent` timestamp).

---

## Provider capability flags

Declare what each provider supports so normalization can branch on capabilities rather than `if provider == "nws"` checks:

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    vtec: bool = False
    multi_language: bool = False
    geometry: Literal["none", "optional", "primary"] = "optional"
    lifecycle: Literal["explicit", "implicit"] = "explicit"
```

Lets the normalization layer stay provider-agnostic as the provider set grows.

---

## Identity strategy config

User-facing option:

- `stable_event` (current default) — one entity per logical event, survives Update/Cancel. Best for automations.
- `message_strict` — one entity per CAP message, exactly what the provider emitted. Best for accuracy-focused users.

Now that WMO has landed (it hashes the sender-scoped CAP `<identifier>`, so `stable_event` holds), this is most relevant for a future source with weaker lifecycle guarantees where `stable_event` collapsing is harder to justify.

---

## Parameter extraction policy

Selective extraction of `<parameter>` fields with size budgeting. WMO feeds can have heavy parameter usage that risks the 16 KB attribute limit — the exact problem this integration was created to avoid. Needs either:

- a provider-declared allowlist of parameter keys to retain, or
- a per-alert size cap that drops low-priority parameters when exceeded.

---

## Integration-level language selection

Promote `CONF_LANGUAGE` from an options-flow setting to a more prominent concept — possibly to the config flow (identity-level) since language affects *which alerts you see* for bilingual providers, not just how they're presented.

---

## Full multi-info CAP model

Phase B of the bilingual work. Current shape uses flat `headline` / `description` / `instruction` with `_alt` sibling fields — sufficient for two languages. For WMO providers with multiple `<info>` blocks (each with its own `xml:lang` and non-duplicated content), refactor to:

```python
@dataclass(frozen=True, slots=True)
class CAPInfo:
    language: str
    headline: str = ""
    description: str = ""
    instruction: str | None = None
    # ... other per-info fields

class CAPAlert:
    # ... identity, classification, timestamps, geography ...
    infos: tuple[CAPInfo, ...] = ()
    # flat fields stay as a "resolved" view for the preferred language
    headline: str = ""  # resolved from infos by coordinator
```

Breaking internal change — best done alongside a new provider that actually needs it. The shipped WMO provider did *not* trigger this: SWIC sources publish one language each, so the flat `_alt`-sibling shape still suffices. The card adapter and attribute shape shouldn't need to change if resolved flat fields are kept.

---

## Periodic WMO source revalidation

`const.py::WMO_UNMIRRORED_SOURCES` is a point-in-time curation (verified 2026-05-24) of the registered SWIC sources that 404 on the `severeweather.wmo.int` mirror. The config-flow dropdown excludes them so users aren't offered broken sources, but the set goes stale: a newly-mirrored source stays hidden until the constant is updated by hand (`custom_value` entry is the escape hatch in the meantime).

**Proposed**: a low-frequency background revalidation (e.g. HEAD-probe the mirror for registry sources and cache the reachable set on `hass.data` with a long TTL) so the exclude set self-heals without a code change. Must stay off the coordinator poll path — it's a config-flow-only concern.
