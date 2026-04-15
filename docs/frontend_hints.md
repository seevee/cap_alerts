# Frontend Hints

Copy-paste references for cards/automations consuming `cap_alerts`.

## Fetching geometry by `geometry_ref`

Every alert entity exposes a `geometry_ref` attribute (e.g. `nws:OKX.SV.W.0042.2026`)
and a `bbox: [min_lon, min_lat, max_lon, max_lat]`. Full polygons live
out-of-band — fetch lazily when rendering a map.

### Websocket (preferred for live cards)

```ts
const result = await hass.connection.sendMessagePromise({
  type: "cap_alerts/geometry",
  geometry_ref: attrs.geometry_ref,
});
// result = { type: "FeatureCollection", features: [ { type: "Feature", geometry, properties: { ref } } ] }
```

Unknown refs send an error frame (`not_found`).

### REST

```sh
curl -H "Authorization: Bearer $HA_TOKEN" \
     "http://homeassistant.local:8123/api/cap_alerts/geometry/nws:OKX.SV.W.0042.2026"
```

Returns the same `FeatureCollection`; 404 for unknown refs.
