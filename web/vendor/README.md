# Vendored third-party files

Self-hosted so the app doesn't pull anything from a CDN.

- `leaflet/`: [Leaflet](https://leafletjs.com) 1.9.4, BSD 2-Clause (see `leaflet/LICENSE`). Same files as the
  official npm package (`leaflet.js` sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=).
- `fonts/`: DM Sans and JetBrains Mono (latin subset, woff2) from Google Fonts. Both are under the
  [SIL Open Font License 1.1](https://openfontlicense.org).

Map tiles still come from openstreetmap.org at runtime, and routing/geocoding go through the local server to the
public OSRM and Nominatim services (configurable in `.env`).
