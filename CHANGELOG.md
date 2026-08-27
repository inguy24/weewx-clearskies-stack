# Changelog

All notable changes to weewx-clearskies-stack are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0: minor version bumps may include breaking changes. Read this file before upgrading.

**Cross-repo compatibility matrix** — which api/dashboard/realtime versions work together — is in [README.md](README.md).

---

## [Unreleased]

### Removed

**Geographic Features admin section (MARINE-AND-MAPS-PLAN M5, ADR-078 Amendment 2)**

- Admin "Geographic Features" section — `_fetch_geographic_features_status()` helper,
  the `admin/routes.py` landing card entry and landing status rows, and the two routes
  `GET /admin/geographic-features` / `POST /admin/geographic-features/update` — removed.
- `templates/admin/geographic_features.html` removed.
- Help content `help.admin.geographic_features.*` (3 keys) and the 5 orphaned
  literal-string translation entries the deleted template used (`"Geographic Features"`,
  `"Geographic features data updated successfully."`, and 3 longer descriptive strings)
  removed from all 13 locale files.
- `templates/admin/marine.html` header comment's reference to the deleted template's
  name reworded.
- The single-file OpenStreetMap PMTiles overlay this section managed is superseded by
  the Basemap section's three-tier extraction (MARINE-AND-MAPS-PLAN M1); Basemap is
  unaffected by this round.

**Imagery provider admin section + wizard field (MARINE-AND-MAPS-PLAN M4-B, Q10-6)**

- Admin "Imagery" config section (`/admin/config/api/imagery`) and its dedicated
  `imagery_section.html` template, removed.
- Setup wizard step 6 (Providers & API Keys) "Imagery" fieldset — provider select
  (auto/naip/esri) and API key field — removed.
- `wizard/state.py` `WizardState.imagery_api_key` field removed.
- Help content `help.admin.imagery-provider.*` / `help.wizard.imagery.*` removed
  from `en.json` (the only locale that carried them).
- The API's `/imagery/config` endpoint (product basemap for the surf height map)
  and the wizard marine-step Esri satellite toggle (operator-only, direct browser
  URL) are unaffected — this round removes only the now-unreachable provider
  selection UI (PA9 extended; operator: "if we dont need it then get rid of it").

### Added

**Basemap admin section (MARINE-AND-MAPS-PLAN M1-STACK)**

- `GET /admin/basemap` + `POST /admin/basemap/update` — status table (world/local/radar
  tiers: availability, size, zoom range, last updated) and a one-action "Update Basemap"
  trigger against the API's `/api/v1/basemap/status` and `POST /setup/basemap/update`.
  Async: 202 shows a "started" flash and the page polls every 10s while `updating` is
  true; 409 shows an "already running" flash; the update button is disabled while an
  extract is in progress; a `last_error` renders as an alert.
- `templates/admin/basemap.html`, help content `help.admin.basemap.*` in all 13 locales,
  `docs/OPERATOR-MANUAL.md` "Basemap" subsection.
- No operator-typed bounds or zoom fields — the extract's extent comes from the station,
  earthquake radius, and marine locations already in the API's config (directive 14).

## [0.1.0] — 2026-05-19

First public release.

### Added

**Docker Compose stack**

- `docker-compose.yaml` — clearskies-api, clearskies-realtime, clearskies-dashboard, and Caddy reverse proxy as a single `docker compose up -d` command
- `.env.example` with all configurable variables documented
- Automatic Let's Encrypt TLS via Caddy when `CADDY_HOST` is set to a public domain

**Documentation**

- `README.md` — architecture diagram, component table, cross-repo compatibility matrix, quick start, dev/test stack pointer
- `INSTALL.md` — single-host Docker Compose, cross-host, bare-metal/native, Raspberry Pi, update procedure, Home Assistant REST and MQTT examples, site password protection, troubleshooting guide
- `CONFIG.md` — full `.env` variable reference
- `SECURITY.md` — trust model, secrets management, TLS, network exposure, dependency auditing

**Development/test infrastructure (`dev/`)**

- MariaDB 10.11 service with seed loader (already present from Phase 1)
- Redis 7 service profile for provider response cache integration tests
- `dev/.env.example` with dev-stack variables
- `dev/mariadb-init/01-clearskies-ro.sql` — idempotent SELECT-only user creation

**Example Home Assistant configs**

- `examples/home-assistant/sensors-rest.yaml` — REST sensor definitions
- `examples/home-assistant/sensors-mqtt.yaml` — MQTT sensor definitions

### Updating (this release)

This is the first release; no upgrade steps apply.

[0.1.0]: https://github.com/inguy24/weewx-clearskies-stack/releases/tag/v0.1.0
