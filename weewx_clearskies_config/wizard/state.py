"""WizardState dataclass and in-memory session store.

The wizard collects configuration across 8 steps. Data accumulates in a
WizardState keyed by session ID. The store is backed by disk so progress
survives tool restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WizardState:
    """Accumulated configuration across all wizard steps."""

    # Step 1: API connection (ADR-038)
    api_address: str | None = None
    api_session_id: str | None = None
    cert_fingerprint: str | None = None

    # Database connection
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_password: str | None = None
    db_name: str = "weewx"
    # Database kind, detected from the API's GET /setup/db-defaults response
    # ("sqlite" or "mysql").  db_path holds the SQLite database file path when
    # db_kind == "sqlite"; the db_host/db_port/db_user/db_name fields above are
    # used only when db_kind == "mysql".
    db_kind: str = "mysql"
    db_path: str = ""

    # Column mapping — key=db_column_name, value=canonical_name or None (unmapped/skip)
    column_mapping: dict[str, str | None] = field(default_factory=dict)

    # Confirmed unit assignments — key=db_column_name, value=unit string
    # (e.g. "degree_F", "microgram_per_meter_cubed").  Populated in step 3
    # from auto-detected / heuristic / operator-entered values.  Sent to the
    # API as part of the apply payload so it can write [column_units] in api.conf.
    column_units: dict[str, str] = field(default_factory=dict)

    # Processed schema data cached between step 2 POST and step 3 GET.
    # Shape: {"stock_columns": [...], "unmapped_columns": [...], "total_columns": int, "stock_mapped": int}
    # Set by step 2 POST after calling ApiClient.get_schema(); cleared after step 3 advances.
    schema_data: dict[str, Any] | None = None

    # Station identity
    station_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_meters: float | None = None
    # Unit the operator used when entering altitude ("meters" or "feet").
    # Stored so the review page can display altitude in the same unit the user chose.
    altitude_unit: str = "meters"
    timezone: str | None = None

    # Provider selections — key=domain (forecast/alerts/aqi/earthquakes/radar), value=provider_id
    providers: dict[str, str] = field(default_factory=dict)

    # API keys — key=provider_id, value=dict of credential field names → values
    api_keys: dict[str, dict[str, str]] = field(default_factory=dict)

    # Topology
    topology: str = "same-host"  # "same-host" or "cross-host"
    proxy_secret: str | None = None

    # Bind addresses
    api_bind_host: str = "127.0.0.1"
    api_bind_port: int = 8765

    # Navigation: True when step 3 (schema) was skipped due to all-stock columns.
    # Used by step 4 to render the correct Previous button target.
    schema_skipped: bool = False

    # Default locale for the dashboard UI (ADR-021).  One of the 13 supported
    # BCP-47 tags.  Sent to the API via POST /setup/apply and written to
    # [station] default_locale in api.conf by the API.
    default_locale: str = "en"

    # Webcam configuration
    webcam_enabled: bool = False
    webcam_image_url: str = "/webcam/weather_cam.jpg"
    webcam_video_url: str = "/webcam/weewx_timelapse.mp4"
    webcam_refresh_interval: int = 60

    # EULA acceptance — empty string means not yet accepted.
    # Set to a UTC ISO-8601 timestamp (e.g. "2026-06-10T12:34:56.789012Z")
    # when the operator accepts the Operator License Agreement in step 3.
    # Used to skip re-display if already accepted in the same wizard run.
    eula_accepted_at: str = ""

    # Step 0: skin.conf import — None means no import was attempted.
    # When populated, subsequent steps pre-fill from the imported data.
    imported_config: dict[str, Any] | None = None

    # Source skin name detected during step 0 import (e.g. "Belchertown").
    # Used by image resolution to locate files under /etc/weewx/skins/<skin>/.
    source_skin: str | None = None

    # Converted charts.conf content (from graphs.conf migration, T4.1).
    # Empty string means no graphs.conf was uploaded / migration was not
    # attempted or failed. Written to /etc/weewx-clearskies/charts.conf on
    # apply when non-empty.
    charts_conf_text: str = ""

    # Image resolution results from step 0 import (ADR-043).
    # Shape: {key: {"status": "local"|"api"|"unresolved"|"missing", "dest": str|None, "original": str}}
    # None means no image detection has been attempted.
    imported_images: dict[str, Any] | None = None

    # Unit configuration (step inserted after station identity).
    # Key = weewx unit group name (e.g. "group_temperature"),
    # Value = selected unit string (e.g. "degree_F").
    # None means the step has not been completed; defaults to US units on first visit.
    units: dict[str, str] | None = None

    # Branding (step 8)
    site_title: str = ""
    copyright_entity: str = ""
    logo_light_url: str = ""
    logo_dark_url: str = ""
    logo_alt: str = ""
    favicon_url: str = ""
    # Custom background image (optional) — URL to an operator-uploaded image
    # that replaces the dashboard's built-in day/night scene backgrounds.
    # Empty string means "use the built-in backgrounds." Written to
    # branding.json as customBackgroundUrl; the dashboard reads it directly.
    custom_background_url: str = ""

    # Appearance / theme (step 8)
    # accent: one of "blue", "teal", "indigo", "purple", "green", "amber"
    accent: str = ""
    # default_theme_mode: one of "light", "dark", "auto-os", "auto-sunrise-sunset"
    default_theme_mode: str = ""
    # Analytics (step 8) — Phase 4 API fields; saved to state/stack.conf only
    # GA4 Measurement ID (format: G-XXXXXXXXXX)
    google_analytics_id: str = ""

    # Privacy regions (step 8) — Phase 4 API field; saved to state/stack.conf only
    # Comma-separated list of continent slugs, e.g. "north-america,europe" or "global"
    privacy_regions: str = ""

    # Seismic config (inline in provider step, earthquakes domain)
    earthquake_radius_km: float = 100.0
    earthquake_min_magnitude: float = 2.0
    earthquake_default_days: int = 7

    # Legal content overrides (T4.2) — optional Markdown that replaces the
    # default templates on the dashboard's Legal page.  Empty string means
    # "use the default template."  Written to
    # /etc/weewx-clearskies/content/terms.md and privacy.md on apply.
    custom_terms_md: str = ""
    custom_privacy_md: str = ""

    # About This Station content (FIX-008) — optional Markdown displayed on
    # the dashboard's About page.  Empty string means "use the default template."
    # Written to /etc/weewx-clearskies/content/about.md on apply.
    about_content: str = ""

    # Station photo (optional) — displayed on the About page.
    # station_photo_url: served URL (e.g. /wizard/branding/photo.jpg) or empty.
    # station_photo_alt: WCAG-required alt text for the station photo image.
    station_photo_url: str = ""
    station_photo_alt: str = ""

    # Aeris forecast model selection (ADR-063)
    # aeris_forecast_model: one of xcast|standard
    # Persisted in wizard state and sent to the API via POST /setup/apply under
    # the providers.forecast entry so the API can write it to [forecast]
    # aeris_forecast_model in api.conf.
    aeris_forecast_model: str = "xcast"

    # LibreWxR radar configuration
    librewxr_endpoint: str = "https://api.librewxr.net"
    librewxr_bounds: str = ""  # "south,west,north,east" or empty for global

    # AQI regional configuration (ADR-059) — provider-specific scale selectors.
    # Persisted in wizard state and sent to the API via POST /setup/apply under
    # the providers.aqi entry so the API can write them to [providers.aqi] in
    # api.conf.
    #
    # aeris_aqi_filter: one of airnow|china|india|eaqi|caqi|uk|de|cai
    aeris_aqi_filter: str = "airnow"
    # openmeteo_aqi_index: one of us_aqi|european_aqi
    openmeteo_aqi_index: str = "us_aqi"
    # iqair_aqi_scale: one of us|cn
    iqair_aqi_scale: str = "us"

    # TLS configuration (step 14)
    # tls_mode: one of "acme_http01", "acme_dns01", "behind_proxy"
    tls_mode: str = ""
    tls_domain: str = ""
    tls_acme_email: str = ""
    # tls_dns_provider: one of cloudflare|route53|googlecloud|digitalocean|namecheap
    tls_dns_provider: str = ""
    # tls_dns_api_token is stored in state for session recovery but written
    # only to secrets.env (mode 0600), never to stack.conf.
    tls_dns_api_token: str = ""
    # Manual mode: filesystem paths to the operator-supplied certificate and
    # private key (PEM format).  Populated either by typing a path directly
    # or by uploading a file (see tls_cert_uploaded/tls_key_uploaded below).
    # Written to stack.conf [tls] cert_path/key_path when tls_mode == "manual".
    tls_cert_path: str = ""
    tls_key_path: str = ""
    # True when the corresponding path above was populated by a file upload
    # (saved under {config_dir}/tls/) rather than an operator-typed path.
    # Used by the TLS step template to show "Using uploaded certificate/key"
    # instead of an editable path field on re-run.
    tls_cert_uploaded: bool = False
    tls_key_uploaded: bool = False

    # Marine features (T6.1) — per-location boating/surf/fishing/beach-safety
    # configuration, configured on the marine wizard step (step 13).
    marine_enabled: bool = False
    # Key = location slug (derived from the operator-entered name), value =
    # location config dict. Shape:
    #   {"name": str, "lat": float, "lon": float, "activities": [str, ...],
    #    "ndbc_station_ids": [str, ...], "coops_station_ids": [str, ...],
    #    "nws_marine_zone_id": str,
    #    "surf": {"segment_start_lat": float, "segment_start_lon": float,
    #             "segment_end_lat": float, "segment_end_lon": float,
    #             "bottom_type": str,
    #             "topographic_feature": str,
    #             # directional_exposure: fan-derived (Auto) by default; an
    #             # empty/absent list here means Auto -- the marine service
    #             # measures exposure from the coastline fan. A non-empty
    #             # list is a manual OVERRIDE the operator explicitly chose
    #             # (C9b; parity with marine_config.py's
    #             # directional_exposure_is_override).
    #             "directional_exposure": [str, ...]},
    #    # directional_exposure_is_override (bool, optional, location-level --
    #    # NOT inside "surf"): display-only, set only when this location was
    #    # restored from an already-applied config (see
    #    # _merge_from_api_current_config in wizard/routes.py). Records that
    #    # source config's own directional_exposure key-presence, for the
    #    # step_marine.html mode toggle's default state (C9b). Kept outside
    #    # "surf" so build_marine_payload()'s explicit surf-key whitelist
    #    # (config_writer.py) can never forward it to the API.
    #    "fishing": {"target_category": str, "species": [str, ...]},
    #    "beach_safety": {"external_links": [{"label": str, "url": str}, ...]}}
    # "surf"/"fishing"/"beach_safety" keys are present only when that
    # activity was selected for the location.
    marine_locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Global refresh intervals applied to all marine locations. Hours: 1|3|6.
    marine_forecast_ttl_hours: int = 3
    # Minutes: 15|30|60.
    marine_observation_ttl_minutes: int = 30

    # Marine alert radius (T6.4) — miles from the station within which NWS
    # marine zones are discovered for the alerts provider. 0 disables marine
    # alert zone discovery. Sent to the API under providers.alerts when an
    # alerts provider is selected.
    marine_alert_radius_miles: int = 0

    # Marine companion service connection (T7.2).
    # The marine service is an add-on to the API: the API is the only thing
    # that talks to it (ARCHITECTURE.md, "The marine service is an add-on
    # reached only through the API — INVARIANT").  The wizard only records
    # where it lives and how to authenticate; the API does the talking.
    #
    # Base URL of the marine service, including scheme and port — e.g.
    # https://localhost:8780 (same host) or https://192.0.2.10:8780.
    # IPv6 literals must be bracketed: https://[2001:db8::1]:8780.
    # Written by the API to api.conf [providers] marine_service_url.
    # Empty string means no marine service is connected.
    marine_service_url: str = ""
    # Shared bearer token for the marine service.  Written by the API to
    # secrets.env as MARINE_SERVICE_SECRET, never to api.conf.
    marine_service_secret: str = ""
    # Whether the API verifies the marine service's TLS certificate.
    # Written by the API to api.conf [providers] marine_verify_tls.
    marine_verify_tls: bool = True

    # SWAN+TruShore nearshore model tuning (T4.4).
    # Shown only when swan-check passes — that is, when the marine service
    # reports a usable SWAN binary on its own host (C-54/C-66).  It is not a
    # pip-extra check on this host, and never was one on the API host either.
    # Where SWAN runs is no longer configured here (T7.2): SWAN lives in the
    # marine service, reached at marine_service_url above.  These are the
    # model's tuning knobs only.
    # OpenMP thread count for the SWAN subprocess. 0 = use all CPU cores;
    # positive values limit SWAN to that many threads (useful on multi-tenant
    # hosts where SWAN should not monopolize all cores).
    trushore_omp_num_threads: int = 0
    # SWAN computational grid resolution in metres (50–1000). Lower = finer
    # resolution = more accurate but slower. 200 m is the recommended default.
    # Kept for backward-compatibility with serialized wizard states written
    # before the nested-grid migration (T7.4). New code writes
    # trushore_outer_grid_resolution_km and trushore_inner_nest_resolution_m.
    trushore_swan_grid_resolution_m: int = 200
    # Nested grid fields (T7.4): two-level SWAN grid architecture.
    # Outer (coarse) grid covers the continental shelf; inner (fine) nest
    # is focused on surf spot coordinates.
    trushore_outer_grid_resolution_km: float = 3.0
    trushore_inner_nest_resolution_m: int = 200
    # True once POST /wizard/trushore has been submitted this session (or a
    # resumed session that previously submitted it). Implements the
    # 2026-08-03 operator ruling: the apply payload's "swan" block is sent
    # only when the operator entered it this session, so a wizard re-run
    # can't clobber persisted [swan] values on disk with rendered defaults
    # (WIZARD-STUDY-AREA-RESET-INVESTIGATION-2026-08-03.md C9).
    swan_step_completed: bool = False

    # Registry-keyed values for wizard steps that delegate field rendering
    # to the config registry macros.  Key = registry config_key (e.g. "enabled",
    # "image_url"), value = current value.  Populated by step GET handlers and
    # read by step templates via render_section_fields / render_field.
    registry_values: dict[str, Any] = field(default_factory=dict)

    # Restart-status polling counter (T4.5).  Incremented on every
    # GET /wizard/restart-status call made while the API is still down;
    # reset to 0 once the API responds healthy, or when the operator clicks
    # Retry after a timeout is reached. Ephemeral — not written to disk,
    # since it only matters for the few minutes right after Apply.
    restart_poll_count: int = 0


# ---------------------------------------------------------------------------
# In-memory session store
# ---------------------------------------------------------------------------

_store: dict[str, WizardState] = {}

# Set once at app startup via configure_state_persistence().
_config_dir: Path | None = None


def configure_state_persistence(config_dir: Path) -> None:
    """Register the config directory used for disk persistence.

    Called by create_wizard_router() so the module knows where to read/write
    progress files without having to thread config_dir through every call site.
    """
    global _config_dir  # noqa: PLW0603
    _config_dir = config_dir
    from weewx_clearskies_config.wizard.state_persistence import cleanup_stale_progress
    cleanup_stale_progress(config_dir)


def get_wizard_state(session_id: str) -> WizardState:
    """Return the WizardState for *session_id*.

    Lookup order:
      1. In-memory _store (fastest, already loaded this request).
      2. Disk progress file (survives restarts).
      3. Fresh WizardState (first visit).
    """
    if session_id in _store:
        return _store[session_id]
    if _config_dir is not None:
        from weewx_clearskies_config.wizard.state_persistence import load_progress
        loaded = load_progress(session_id, _config_dir)
        if loaded is not None:
            _store[session_id] = loaded
            return loaded
    _store[session_id] = WizardState()
    return _store[session_id]


def save_wizard_state(session_id: str, state: WizardState) -> None:
    """Persist *state* for *session_id* in memory and on disk."""
    _store[session_id] = state
    if _config_dir is not None:
        from weewx_clearskies_config.wizard.state_persistence import save_progress
        save_progress(session_id, state, _config_dir)


def clear_wizard_state(session_id: str) -> None:
    """Remove the WizardState for *session_id* (called after apply or cancel)."""
    _store.pop(session_id, None)
    if _config_dir is not None:
        from weewx_clearskies_config.wizard.state_persistence import delete_progress
        delete_progress(session_id, _config_dir)
