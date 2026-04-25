# Roadmap

Future work tracked for context — none of these are in scope until prioritized.

For the design of shipped behavior, see [`architecture.md`](architecture.md).

---

## Idempotent merge logic (AlertStore)

For WMO feeds that do full-message replacement without explicit Cancel messages. The current store diffs by ID presence — if a provider recomputes IDs differently across message replacements, it causes entity churn (remove + re-create).

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

Relevant when WMO providers land with weaker lifecycle guarantees where `stable_event` collapsing is harder to justify.

---

## Parameter extraction policy

Selective extraction of `<parameter>` fields with size budgeting. WMO feeds can have heavy parameter usage that risks the 16 KB attribute limit — the exact problem this integration was created to avoid. Needs either:

- a provider-declared allowlist of parameter keys to retain, or
- a per-alert size cap that drops low-priority parameters when exceeded.

---

## MeteoAlarm EMMA_ID region selector

MeteoAlarm v1 supports country-wide and GPS-mode filtering. Each `<cap:area>` carries an `EMMA_ID` geocode (e.g. `DE006` for Bavaria) that names a stable sub-country region. A future enhancement adds a per-country region selector populated from a static `EMMA_ID → label` mapping so users can subscribe to "Bavaria only" without specifying GPS coordinates. Out of scope for v1 because each member country ships its own `EMMA_ID` table — maintaining 35+ tables is the scaling concern, not the parsing.

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

Breaking internal change — best done alongside a new provider that actually needs it. The card adapter and attribute shape shouldn't need to change if resolved flat fields are kept.
