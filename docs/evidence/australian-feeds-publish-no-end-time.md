# The Australian feeds stamp expires as a regeneration TTL, not an end time

| | |
| :-- | :-- |
| Supports | RFC §2.2, §6.3 |
| Source | NSW RFS, QFD, DFES and TasALERT EDXL-DE feeds (`const.AU_FEEDS`) |
| Sample | one alert per state feed, four captures |
| Observed | 2026-09-19 (NSW, QLD, WA), 2026-09-20 (TAS), 2026-09-21 (TAS two products) |
| Reproduce | `tests/fixtures/au_nsw.xml`, `au_qld.xml`, `au_wa.xml`, `au_tas.xml`, `au_tas_two_products.xml` |

Values copied from the first alert of each fixture. The envelope time is the
EDXL `dateTimeSent` of the whole feed document.

| State | Envelope `dateTimeSent` | Alert `sent` | Alert `expires` | `expires − envelope` | `expires − sent` |
| :-- | :-- | :-- | :-- | :-- | :-- |
| NSW | 2026-09-20T09:43:41+10:00 | 2026-09-20T09:05:00+10:00 | 2026-09-21T09:43:41+10:00 | +24 h 0 m | +24 h 38 m |
| QLD | 2026-09-20T09:42:53+10:00 | 2026-09-19T14:30:31+10:00 | 2026-09-20T14:30:31.6+10:00 | +4 h 47 m | +24 h 0.6 s |
| WA | 2026-09-20T07:51:33.586+08:00 | 2026-09-19T09:06:56.523+08:00 | 2026-09-19T09:06:56.523+08:00 | −22 h 44 m | 0 |
| TAS | 2026-09-20T09:51:04+10:00 | 2026-09-20T09:44:40+10:00 | 2026-09-21T09:51:04+10:00 | +24 h 0 m | +24 h 6 m |
| TAS (two products) | 2026-09-21T02:45:57+10:00 | 2026-09-20T19:03:14+10:00 | 2026-09-22T02:45:57+10:00 | +24 h 0 m | +31 h 42 m |

NSW and TAS: `expires` equals the envelope time plus 24 h on every alert, so it
recedes on every poll. QLD: `sent` plus 24 h. WA: `sent` itself, already past
on arrival.

`<incidents>` as the identity, `tests/fixtures/au_tas_two_products.xml`, two
products under one incident number
([#218](https://github.com/seevee/cap_alerts/issues/218)):

```xml
<identifier>036999-20092026-83558</identifier>
…
<sent>2026-09-20T19:03:14+10:00</sent>
…
<incidents>TFS:036999-20092026</incidents>
…
  <eventCode>
    <valueName>https://govshare.gov.au/xmlui/handle/10772/6495</valueName>
    <value>bushFire</value>
…
<identifier>036999-20092026-83484</identifier>
…
<sent>2026-09-20T18:56:46+10:00</sent>
…
<incidents>TFS:036999-20092026</incidents>
…
  <eventCode>
    <valueName>https://govshare.gov.au/xmlui/handle/10772/6495</valueName>
    <value>smoke</value>
```

## Reading

None of the four `expires` values is an end time. Two are the feed's own
generation time plus a day, one is the message time plus a day, and WA's is
zero. Normalized as published, WA's alerts would be expired on first sight
and the others would never expire while listed. The provider drops the field
(`providers/au.py`), and the incident ends when the feed withdraws it, which
is RFC §2.2's open-ended shape. `<incidents>` names the fire a message is
about, and §6.3 reads it as identity, not hierarchy.

## Caveats

- One alert per fixture is shown. The per-state rule was checked across each
  fixture's alerts when the provider shipped, not re-counted here
  ([#127](https://github.com/seevee/cap_alerts/issues/127)).
- Regeneration cadence (NSW every minute or two) comes from the provider
  docstring and is not re-measured. The live WA body fetched 2026-10-03 is
  EDXL-DE with no RSS `ttl` element, so the docstring's `ttl` figure is not
  reproduced here.
- WA's `expires − envelope` is the gap at capture time. It grows with every
  poll.
- The second TAS product in the fixture carries `status=Test`. The #218 table
  does not record status, so the fixture is not checked against the live pair
  on that field.
