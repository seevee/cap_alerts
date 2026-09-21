"""Property-based tests over the shared CAP parser.

The XML and JSON readers must yield the same ``CAPDoc`` for the same document,
and neither may raise on feed text that is not a CAP document at all: a host
answering with an error page is a skipped alert, not a crash. Hypothesis
builds documents from the schema's field set, renders them both ways, and
checks the two readers agree with the source and with each other. The chain
resolver and the language selection rules get the same treatment.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.cap_alerts.providers.cap import (
    CAPDoc,
    alternate_info_index,
    cap_doc_from_json,
    language_matches,
    parse_cap_alert,
    resolve_chain_leaves,
    select_info,
)

settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

# ---------------------------------------------------------------------------
# Strategies: a CAP document as plain data, rendered to XML or JSON on demand
# ---------------------------------------------------------------------------

# Element text as both readers hand it back: XML-legal characters, stripped,
# non-empty. Interior whitespace is preserved by both parsers.
text = (
    st.text(
        alphabet=st.characters(exclude_categories=("Cc", "Cs", "Co", "Cn")), max_size=40
    )
    .map(str.strip)
    .filter(bool)
)
# Names used as dict keys: also free of ", " so joined lists stay comparable.
name = text.filter(lambda s: ", " not in s)
lats = st.integers(-9_000_000, 9_000_000).map(lambda i: i / 100_000)
lons = st.integers(-18_000_000, 18_000_000).map(lambda i: i / 100_000)
ring_text = st.lists(st.tuples(lats, lons), min_size=3, max_size=6).map(
    lambda pairs: " ".join(f"{lat!r},{lon!r}" for lat, lon in pairs)
)
circle_text = st.tuples(lats, lons, st.floats(0, 50, allow_nan=False)).map(
    lambda c: f"{c[0]!r},{c[1]!r} {c[2]!r}"
)
pairs = st.lists(st.tuples(name, text), max_size=3)

areas = st.fixed_dictionaries(
    {
        "areaDesc": name,
        "geocode": pairs,
        "polygon": st.lists(ring_text, max_size=2),
        "circle": st.lists(circle_text, max_size=2),
    }
)
infos = st.fixed_dictionaries(
    {
        "language": st.sampled_from(
            ["", "en", "en-US", "de", "de-LS", "fr-CA", "zh-Hans"]
        ),
        "category": st.lists(name, max_size=2),
        "event": text,
        "responseType": st.lists(text, max_size=2),
        "urgency": text,
        "severity": st.sampled_from(
            ["Extreme", "Severe", "Moderate", "Minor", "Unknown"]
        ),
        "certainty": text,
        "effective": text,
        "onset": text,
        "expires": text,
        "senderName": text,
        "headline": text,
        "description": text,
        "instruction": text,
        "web": text,
        "eventCode": pairs,
        "parameter": pairs,
        "area": st.lists(areas, max_size=2),
    }
)
docs = st.fixed_dictionaries(
    {
        "identifier": text,
        "sender": text,
        "sent": text,
        "status": text,
        "msgType": text,
        "scope": text,
        "incidents": st.lists(
            text.filter(lambda s: not any(c.isspace() for c in s)), max_size=2
        ),
        # A references token is split on any whitespace, and its sender and
        # sent parts on commas; the identifier in between may carry commas.
        "references": st.lists(
            st.tuples(
                text.filter(lambda s: "," not in s and not any(c.isspace() for c in s)),
                text.filter(lambda s: not any(c.isspace() for c in s)),
                text.filter(lambda s: "," not in s and not any(c.isspace() for c in s)),
            ),
            max_size=2,
        ),
        "info": st.lists(infos, max_size=2),
    }
)


def _el(tag: str, value: str) -> str:
    return f"<{tag}>{escape(value)}</{tag}>"


def _pairs_xml(tag: str, items: list[tuple[str, str]]) -> str:
    return "".join(
        f"<{tag}>{_el('valueName', n)}{_el('value', v)}</{tag}>" for n, v in items
    )


def render_xml(doc: dict[str, Any], namespaced: bool) -> str:
    parts = [
        _el("identifier", doc["identifier"]),
        _el("sender", doc["sender"]),
        _el("sent", doc["sent"]),
        _el("status", doc["status"]),
        _el("msgType", doc["msgType"]),
        _el("scope", doc["scope"]),
    ]
    if doc["incidents"]:
        parts.append(_el("incidents", " ".join(doc["incidents"])))
    if doc["references"]:
        parts.append(
            _el("references", " ".join(",".join(r) for r in doc["references"]))
        )
    for info in doc["info"]:
        block = [
            _el("language", info["language"]) if info["language"] else "",
            "".join(_el("category", c) for c in info["category"]),
            _el("event", info["event"]),
            "".join(_el("responseType", r) for r in info["responseType"]),
        ]
        for tag in (
            "urgency",
            "severity",
            "certainty",
            "effective",
            "onset",
            "expires",
            "senderName",
            "headline",
            "description",
            "instruction",
            "web",
        ):
            block.append(_el(tag, info[tag]))
        block.append(_pairs_xml("eventCode", info["eventCode"]))
        block.append(_pairs_xml("parameter", info["parameter"]))
        for area in info["area"]:
            block.append(
                "<area>"
                + _el("areaDesc", area["areaDesc"])
                + _pairs_xml("geocode", area["geocode"])
                + "".join(_el("polygon", p) for p in area["polygon"])
                + "".join(_el("circle", c) for c in area["circle"])
                + "</area>"
            )
        parts.append("<info>" + "".join(block) + "</info>")
    xmlns = f' xmlns="{CAP_NS}"' if namespaced else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><alert{xmlns}>'
        + "".join(parts)
        + "</alert>"
    )


def render_json(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "info"}
    out["references"] = [",".join(r) for r in doc["references"]]
    out["info"] = []
    for info in doc["info"]:
        block = dict(info)
        block["eventCode"] = [
            {"valueName": n, "value": v} for n, v in info["eventCode"]
        ]
        block["parameter"] = [
            {"valueName": n, "value": v} for n, v in info["parameter"]
        ]
        block["area"] = [
            {
                **area,
                "geocode": [{"valueName": n, "value": v} for n, v in area["geocode"]],
            }
            for area in info["area"]
        ]
        out["info"].append(block)
    return out


def _dedup(values: list[str]) -> list[str]:
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def _expected_pairs(items: list[tuple[str, str]]) -> dict[str, str]:
    return dict(items)  # last value wins, like both readers


def assert_doc_matches(parsed: CAPDoc, doc: dict[str, Any]) -> None:
    assert parsed.identifier == doc["identifier"]
    assert parsed.sender == doc["sender"]
    assert parsed.sent == doc["sent"]
    assert parsed.status == doc["status"]
    assert parsed.msg_type == doc["msgType"]
    assert parsed.scope == doc["scope"]
    assert parsed.incidents == " ".join(doc["incidents"])
    assert parsed.references == doc["references"]
    assert len(parsed.infos) == len(doc["info"])
    for got, want in zip(parsed.infos, doc["info"], strict=True):
        assert got.language == want["language"]
        assert got.event == want["event"]
        assert got.response_type == want["responseType"]
        for field in (
            "urgency",
            "severity",
            "certainty",
            "effective",
            "onset",
            "expires",
            "headline",
            "description",
            "instruction",
            "web",
        ):
            assert getattr(got, field) == want[field]
        assert got.sender_name == want["senderName"]
        assert got.event_codes == _expected_pairs(want["eventCode"])
        assert got.parameters == _expected_pairs(want["parameter"])
        assert got.area_desc == ", ".join(a["areaDesc"] for a in want["area"])
        assert len(got.areas) == len(want["area"])
        expected_geocodes: dict[str, list[str]] = {}
        expected_polygons: list[list[list[float]]] = []
        expected_circles: list[tuple[float, float, float]] = []
        for area_got, area_want in zip(got.areas, want["area"], strict=True):
            assert area_got.area_desc == area_want["areaDesc"]
            for scheme, value in area_want["geocode"]:
                expected_geocodes.setdefault(scheme, []).append(value)
            per_area: dict[str, list[str]] = {}
            for scheme, value in area_want["geocode"]:
                per_area.setdefault(scheme, []).append(value)
            assert area_got.geocodes == {k: _dedup(v) for k, v in per_area.items()}
            rings = [
                [
                    [float(lon), float(lat)]
                    for lat, lon in (tok.split(",") for tok in p.split())
                ]
                for p in area_want["polygon"]
            ]
            assert area_got.polygons == rings
            expected_polygons.extend(rings)
            for c in area_want["circle"]:
                centre, radius = c.split()
                lat, lon = centre.split(",")
                expected_circles.append((float(lon), float(lat), float(radius)))
        assert got.geocodes == {k: _dedup(v) for k, v in expected_geocodes.items()}
        assert got.polygons == expected_polygons
        assert got.circles == expected_circles


# ---------------------------------------------------------------------------
# Round trips and reader agreement
# ---------------------------------------------------------------------------


@given(docs, st.booleans())
def test_xml_reader_round_trips_a_generated_document(doc, namespaced):
    parsed = parse_cap_alert(render_xml(doc, namespaced))
    assert parsed is not None
    assert_doc_matches(parsed, doc)


@given(docs)
def test_json_reader_round_trips_a_generated_document(doc):
    parsed = cap_doc_from_json(render_json(doc))
    assert parsed is not None
    assert_doc_matches(parsed, doc)


@given(docs)
def test_xml_and_json_readers_agree(doc):
    from_xml = parse_cap_alert(render_xml(doc, namespaced=True))
    from_json = cap_doc_from_json(render_json(doc))
    assert from_xml is not None and from_json is not None
    # Category is the one field the JSON reader joins and the XML reader does
    # not carry as a list; compare everything else structurally.
    for info in (*from_xml.infos, *from_json.infos):
        info.category = ""
    assert from_xml == from_json


# ---------------------------------------------------------------------------
# Hostile input never raises
# ---------------------------------------------------------------------------


@given(st.text())
def test_xml_reader_never_raises_on_arbitrary_text(text):
    parsed = parse_cap_alert(text)
    assert parsed is None or isinstance(parsed, CAPDoc)


@given(
    st.sampled_from(
        [
            '<!DOCTYPE alert [<!ENTITY x "y">]><alert>&x;</alert>',
            '<!DOCTYPE alert SYSTEM "http://example.invalid/cap.dtd"><alert/>',
            '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><alert>&b;</alert>',
        ]
    )
)
def test_xml_reader_never_raises_on_forbidden_constructs(text):
    parsed = parse_cap_alert(text)
    assert parsed is None or isinstance(parsed, CAPDoc)


json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False)
    | st.text(max_size=20)
)
json_values = st.recursive(
    json_scalars,
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.dictionaries(
            st.sampled_from(
                [
                    "identifier",
                    "info",
                    "area",
                    "geocode",
                    "polygon",
                    "circle",
                    "references",
                    "valueName",
                    "value",
                    "x",
                ]
            ),
            inner,
            max_size=5,
        )
    ),
    max_leaves=25,
)


@given(json_values)
def test_json_reader_never_raises_on_arbitrary_json(payload):
    parsed = cap_doc_from_json(payload)
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("identifier"), str)
        and payload["identifier"].strip()
    ):
        assert isinstance(parsed, CAPDoc)
    else:
        assert parsed is None


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------

ids = st.sampled_from(["a", "b", "c", "d", "e", "f"])


@given(
    st.lists(
        st.tuples(ids, st.lists(ids, max_size=3)),
        min_size=1,
        max_size=6,
        unique_by=lambda t: t[0],
    )
)
def test_chain_leaves_are_unreferenced_or_the_whole_input(spec):
    docs_in = [
        CAPDoc(identifier=ident, references=[("s", ref, "t") for ref in refs])
        for ident, refs in spec
    ]
    leaves = resolve_chain_leaves(docs_in)
    assert leaves
    assert all(any(leaf is d for d in docs_in) for leaf in leaves)
    referenced = {ref for _, refs in spec for ref in refs}
    if leaves != docs_in:
        assert not any(leaf.identifier in referenced for leaf in leaves)
    else:
        # Either nothing is referenced, or everything is (the safe fallback).
        assert all(d.identifier not in referenced for d in docs_in) or all(
            d.identifier in referenced for d in docs_in
        )


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------

tags = st.sampled_from(
    [
        "",
        " ",
        "en",
        "EN-us",
        "en-GB",
        "de",
        "de-LS",
        "fr-CA",
        "zh-Hans",
        "zh-mo",
        "sr",
        "sr-Latn",
    ]
)


@given(tags, tags)
def test_language_matches_is_symmetric_and_reflexive_on_real_tags(a, b):
    assert language_matches(a, b) == language_matches(b, a)
    if a.strip():
        assert language_matches(a, a)
    else:
        assert not language_matches(a, b)


def _primary(tag: str) -> str:
    return tag.strip().casefold().split("-", 1)[0]


@given(st.lists(tags, min_size=1, max_size=5), tags)
def test_select_info_follows_the_documented_preference_order(languages, preferred):
    doc = CAPDoc(identifier="x")
    from custom_components.cap_alerts.providers.cap import CAPInfoDoc

    doc.infos = [
        CAPInfoDoc(language=lang, event=str(i)) for i, lang in enumerate(languages)
    ]
    chosen = select_info(doc, preferred)
    assert any(chosen is info for info in doc.infos)
    wanted = preferred.strip().casefold()
    exact = [i for i in doc.infos if i.language.strip().casefold() == wanted]
    loose = [i for i in doc.infos if language_matches(i.language, preferred)]
    english = [i for i in doc.infos if _primary(i.language) == "en"]
    if preferred and exact:
        assert chosen is exact[0]
    elif preferred and loose:
        assert chosen is loose[0]
    elif english:
        assert chosen is english[0]
    else:
        assert chosen is doc.infos[0]


@given(st.lists(tags, min_size=1, max_size=5), st.data())
def test_alternate_info_index_picks_a_different_language_english_first(languages, data):
    primary = data.draw(st.integers(0, len(languages) - 1))
    alt = alternate_info_index(languages, primary)
    primaries = [_primary(t) for t in languages]
    candidates = [
        i for i, p in enumerate(primaries) if i != primary and p != primaries[primary]
    ]
    if not candidates:
        assert alt is None
    else:
        assert alt in candidates
        if any(primaries[i] == "en" for i in candidates):
            assert primaries[alt] == "en"
        else:
            assert alt == candidates[0]
