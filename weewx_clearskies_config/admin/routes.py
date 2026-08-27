"""FastAPI router for the admin landing page, domain-organized sections, and
per-component config editing.

Provides the top-level admin UI at /admin — a domain-organized overview of
all configuration areas.  Individual sections load as HTMX fragments into
the content area.  Also handles the per-component configuration pages that
were formerly in config/routes.py.

Route summary (admin landing + domain sections):
  GET  /admin                         — landing page (requires session)
  GET  /admin/status                  — status page: API + marine service health (B4)
  GET  /admin/status/panel            — HTMX fragment: status panel only, polled every 30s
  GET  /admin/pages                   — page visibility edit form fragment
  POST /admin/pages                   — save page visibility
  GET  /admin/branding                — appearance/branding edit form
  POST /admin/branding                — save branding.json appearance fields
  GET  /admin/analytics               — analytics & privacy edit form
  POST /admin/analytics               — save branding.json analytics/privacy fields
  GET  /admin/earthquakes             — earthquake settings edit form
  POST /admin/earthquakes             — save stack.conf [earthquakes]
  GET  /admin/tls                     — TLS settings edit form
  POST /admin/tls                     — save stack.conf [tls]
  GET  /admin/connection              — API connection settings
  POST /admin/connection              — update API URL, caddy.env, reload Caddy
  GET  /admin/section/sky_classification — sky classification calibration form (custom template)
  POST /admin/section/sky_classification — save api.conf [sky_classification] (generic handler)
  GET  /admin/haze-calibration        — haze calibration settings + status
  POST /admin/haze-calibration        — save api.conf [conditions] haze keys
  POST /admin/haze-calibration/reset  — reset calibration data via API
  GET  /admin/now-layout              — card layout editor form
  POST /admin/now-layout              — save now-layout.json to config dir
  GET  /admin/marine                  — marine locations list (T6.2)
  POST /admin/marine/edit             — render add/edit form for one location
  POST /admin/marine/save             — validate + save one location via /setup/apply
  POST /admin/marine/delete           — delete one location via /setup/apply
  POST /admin/marine/test-connectivity — HTMX: NDBC/CO-OPS/NWS zone status for a location
  GET  /admin/marine-service         — marine companion service connection settings (T7.5)
  POST /admin/marine-service         — save marine service URL, TLS flag + optional new secret
  POST /admin/marine-service/test    — HTMX: ask the API to test the marine service (T7.3)
  GET  /admin/surf-scoring           — surf score weights form (Round S, ADR-101 guidance 6)
  POST /admin/surf-scoring           — validate + save top-level [surf_scoring] via /setup/apply

Route summary (config editor — formerly config/routes.py):
  GET  /admin/config                          — config dashboard (all sections)
  GET  /admin/config/{component}/{section}    — section edit form fragment
  POST /admin/config/{component}/{section}    — save section, return result fragment
  GET  /admin/config/column-mapping           — column mapping form
  POST /admin/config/column-mapping           — update column mapping, return result
  POST /admin/config/test-provider            — test provider connectivity
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, NoReturn

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from weewx_clearskies_config.auth import COOKIE_NAME, SessionManager
from weewx_clearskies_config.config.reader import (
    get_all_sections,
    get_column_mapping,
    get_section,
    read_branding,
    read_pages,
)
from weewx_clearskies_config.config.updater import (
    update_column_mapping,
    update_managed_region,
    update_secrets,
)
from weewx_clearskies_config.constants import MARINE_SAME_HOST_URL
from weewx_clearskies_config.i18n import get_current_locale, translate, translate_md
from weewx_clearskies_config.wizard.providers import PROVIDERS, get_provider, test_provider

logger = logging.getLogger(__name__)


def _(key: str) -> str:
    """Translate *key* using the current request's wizard/admin UI locale.

    Python-code counterpart to the Jinja2 ``_()`` global registered in
    app.py. See the identical helper in wizard/routes.py for the full
    rationale — kept as a local copy rather than a shared import so each
    router module has no import-time dependency on the other.
    """
    return translate(key, get_current_locale())

# Haze calibration defaults
_HAZE_DEFAULTS: dict[str, str] = {
    "haze_detection": "true",
    "gamma": "0.45",
    "openaq_sensor_id": "",
}

# Forecast correction defaults
_FC_DEFAULTS: dict[str, str] = {
    "enabled": "false",
    "collection_enabled": "true",
    "retrain_schedule": "daily",
    "retrain_day": "0",
    "min_samples": "500",
    "retention_years": "3",
    "db_path": "/etc/weewx-clearskies/forecast_correction.db",
    "model_path": "/etc/weewx-clearskies/forecast_correction_model.pkl",
}

# Surf score weight defaults (Round S, ADR-101 guidance 6). These are the
# ADR-101 shipped weights — the same code constants the marine service's
# scorer falls back to. Defaults live in code, not config: an absent
# top-level [surf_scoring] section in the API's current-config means these
# values, so this dict is a display/pre-fill fallback and is never written
# anywhere unless the operator saves the form.
_SURF_SCORING_DEFAULTS: dict[str, str] = {
    "weight_size": "0.25",
    "weight_shape": "0.25",
    "weight_conditions": "0.20",
    "weight_power": "0.20",
    "weight_consistency": "0.10",
}

# ---------------------------------------------------------------------------
# Module-level state injected by create_admin_router()
# ---------------------------------------------------------------------------

_templates: Jinja2Templates | None = None
_session_manager: SessionManager | None = None
_config_dir: Path | None = None
_dashboard_root: Path | None = None

router = APIRouter(prefix="/admin", tags=["admin"])


def create_admin_router(
    templates: Jinja2Templates,
    session_manager: SessionManager,
    config_dir: Path,
    dashboard_root: Path,
) -> APIRouter:
    """Configure the admin router with shared app objects and return it."""
    global _templates, _session_manager, _config_dir, _dashboard_root  # noqa: PLW0603
    _templates = templates
    _session_manager = session_manager
    _config_dir = config_dir
    _dashboard_root = dashboard_root
    from weewx_clearskies_config.registry import registry
    manifest_path = dashboard_root / "card-manifest.json"
    registry.load_card_config_fields(str(manifest_path))
    return router


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_session(request: Request) -> None:
    """Raise 401 if the request does not carry a valid session cookie."""
    mgr = _session_manager
    if mgr is None:
        _raise_unauthorized()
    session_id = request.cookies.get(COOKIE_NAME, "")
    if not session_id or not mgr.get_username(session_id):
        _raise_unauthorized()


def _raise_unauthorized() -> NoReturn:
    from starlette.exceptions import HTTPException as StarletteHTTPException

    raise StarletteHTTPException(status_code=401, detail=_("Authentication required"))


def _render(
    request: Request,
    template_name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    assert _templates is not None, "Admin router not initialised"
    return _templates.TemplateResponse(
        request=request,
        name=f"admin/{template_name}",
        context=context,
        status_code=status_code,
    )


def _render_result(
    request: Request,
    *,
    section_slug: str,
    display_name: str,
    success: bool,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "result.html",
        {
            "section_slug": section_slug,
            "display_name": display_name,
            "success": success,
            "error": error,
        },
        status_code=status_code,
    )


def _get_with_defaults(section_values: dict[str, str], defaults: dict[str, str]) -> dict[str, str]:
    """Return section_values merged over defaults (section_values wins)."""
    result = dict(defaults)
    result.update(section_values)
    return result


def _get_api_client():  # type: ignore[return]
    """Build an ApiClient for the known API using proxy-auth mode.

    Returns None if no known API is configured or proxy secret is missing.
    """
    if _config_dir is None:
        return None
    from weewx_clearskies_config.wizard.known_apis import load_known_apis
    from weewx_clearskies_config.wizard.state_persistence import _read_secrets_env
    known = load_known_apis(_config_dir)
    if not known:
        return None
    api_url = next(iter(known))
    secrets = _read_secrets_env(_config_dir)
    proxy_secret = secrets.get("WEEWX_CLEARSKIES_PROXY_SECRET")
    if not proxy_secret:
        return None
    from weewx_clearskies_config.wizard.api_client import ApiClient
    return ApiClient(api_url, proxy_secret=proxy_secret)


def _fetch_api_providers() -> dict[str, dict[str, Any]]:
    """Fetch all provider configs from the API's /setup/current-config.

    Returns a dict keyed by domain (forecast, alerts, aqi, radar, earthquakes),
    each value a flat dict like ``{"provider": "librewxr", "librewxr_endpoint": "..."}``.
    Returns ``{}`` if the API is unreachable.
    """
    client = _get_api_client()
    if client is None:
        return {}
    try:
        config = client.get_current_config()
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch current config from API for provider sections", exc_info=True)
        return {}
    raw_providers = config.get("providers", {})
    if not isinstance(raw_providers, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for domain, pdata in raw_providers.items():
        if not isinstance(pdata, dict):
            continue
        flat: dict[str, Any] = {"provider": str(pdata.get("provider", ""))}
        for key in ("librewxr_endpoint", "librewxr_bounds", "iframe_url",
                    "aeris_forecast_model", "nws_user_agent_contact"):
            val = pdata.get(key)
            if val:
                flat[key] = str(val)
        result[domain] = flat
    return result


def _read_calibration_state() -> dict | None:
    """Fetch calibration state from the API.

    Returns the API's calibration-state response dict, or None if the
    API is unreachable or not configured.
    """
    client = _get_api_client()
    if client is None:
        return None
    try:
        response = client._request("GET", "/setup/calibration-state")
        return response.json()
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch calibration state from API", exc_info=True)
        return None


def _fetch_basemap_status() -> dict | None:
    """Fetch basemap status from the API.

    Returns the API's basemap/status response dict, or None if the API is
    unreachable or not configured.
    """
    client = _get_api_client()
    if client is None:
        return None
    try:
        response = client._request("GET", "/api/v1/basemap/status")
        return response.json()
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch basemap status from API", exc_info=True)
        return None


def _fetch_forecast_correction_status() -> dict | None:
    """Fetch forecast correction status from the API.

    Returns the API's forecast-correction/status response dict, or None if
    the API is unreachable or not configured.
    """
    client = _get_api_client()
    if client is None:
        return None
    try:
        response = client._request("GET", "/setup/forecast-correction/status")
        return response.json()
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch forecast correction status from API", exc_info=True)
        return None


def _safe_float_range(form: Any, key: str, lo: float, hi: float, defaults: dict) -> str:
    """Return a validated float string from form data within [lo, hi], or the default."""
    raw = str(form.get(key, "")).strip()
    if raw:
        try:
            val = float(raw)
            if lo <= val <= hi:
                return raw
        except ValueError:
            pass
    return defaults[key]


# ---------------------------------------------------------------------------
# Config editor helpers (formerly config/routes.py)
# ---------------------------------------------------------------------------


def _render_config(
    request: Request,
    template_name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a config/ template (used by the per-component config editor)."""
    assert _templates is not None, "Admin router not initialised"
    return _templates.TemplateResponse(
        request=request,
        name=f"config/{template_name}",
        context=context,
        status_code=status_code,
    )


# Each entry: (component, section_key, display_name, secret_fields)
# secret_fields: form field names whose values go into secrets.env instead of
# being written directly into the .conf file.
_SECTION_META: list[tuple[str, str, str, tuple[str, ...]]] = [
    # api.conf sections
    ("api", "server", "API Server", ()),
    ("api", "database", "Database Connection", ("password",)),
    ("api", "forecast", "Forecast Provider", ()),
    ("api", "alerts", "Alerts Provider", ()),
    ("api", "aqi", "AQI Provider", ()),
    ("api", "earthquakes", "Earthquakes Provider", ()),
    ("api", "radar", "Radar Provider", ()),
    # stack.conf sections — webcam is a UI concern, written by the wizard to stack.conf
    ("stack", "ui", "UI Settings", ()),
    ("stack", "webcam", "Webcam", ()),
]

# Set of (component, section) pairs that are valid for editing
_VALID_SECTIONS: frozenset[tuple[str, str]] = frozenset(
    (comp, sec) for comp, sec, _name, _secrets in _SECTION_META
)

# Allowed keys per (component, section).  Only these keys are accepted from
# form submissions — any extra keys are silently dropped (input validation at
# trust boundary per coding.md §1).
_SECTION_ALLOWED_KEYS: dict[tuple[str, str], frozenset[str]] = {
    ("api", "server"):      frozenset({"bind_host", "bind_port"}),
    ("api", "database"):    frozenset({"host", "port", "user", "name", "password"}),
    ("api", "forecast"):    frozenset({"provider"}),
    ("api", "alerts"):      frozenset({"provider"}),
    ("api", "aqi"):         frozenset({"provider"}),
    ("api", "earthquakes"): frozenset({"provider"}),
    ("api", "radar"):       frozenset({"provider", "librewxr_endpoint", "librewxr_bounds"}),
    ("stack", "webcam"):    frozenset({"enabled", "image_url", "video_url", "refresh_interval"}),
    ("stack", "ui"):        frozenset({
        "enabled", "bind_host", "bind_port", "tls_cert_path", "tls_key_path",
        "station_name", "latitude", "longitude", "altitude_meters", "timezone",
        "topology",
    }),
}

# Map (component, section) -> display name
_SECTION_DISPLAY: dict[tuple[str, str], str] = {
    (comp, sec): name for comp, sec, name, _secrets in _SECTION_META
}

# Map (component, section) -> tuple of secret field names
_SECTION_SECRETS: dict[tuple[str, str], tuple[str, ...]] = {
    (comp, sec): secrets for comp, sec, _name, secrets in _SECTION_META
}

# Canonical field names available for column mapping
# Sourced from the weewx standard schema; surfaced as datalist options in the UI.
_CANONICAL_FIELDS = (
    "dateTime", "usUnits", "interval", "barometer", "pressure", "altimeter",
    "inTemp", "outTemp", "inHumidity", "outHumidity", "windSpeed", "windDir",
    "windGust", "windGustDir", "rain", "rainRate", "dewpoint", "windchill",
    "heatindex", "ET", "radiation", "UV", "extraTemp1", "extraTemp2", "extraTemp3",
    "soilTemp1", "soilTemp2", "soilTemp3", "soilTemp4", "leafTemp1", "leafTemp2",
    "extraHumid1", "extraHumid2", "soilMoist1", "soilMoist2", "soilMoist3",
    "soilMoist4", "leafWet1", "leafWet2", "rxCheckPercent", "txBatteryStatus",
    "consBatteryVoltage", "hail", "hailRate", "heatingTemp", "heatingVoltage",
    "supplyVoltage", "referenceVoltage", "windBatteryStatus", "rainBatteryStatus",
    "outTempBatteryStatus", "inTempBatteryStatus", "lightning_strike_count",
    "lightning_distance", "pm1_0", "pm2_5", "pm10_0", "co2",
)

_PROVIDER_DOMAINS = frozenset({"forecast", "alerts", "aqi", "earthquakes", "radar"})


def _secrets_env_key(component: str, section: str, field: str) -> str:
    """Build the secrets.env key for a secret field.

    Convention: WEEWX_CLEARSKIES_<COMPONENT>_<SECTION>_<FIELD>
    """
    return f"WEEWX_CLEARSKIES_{component.upper()}_{section.upper()}_{field.upper()}"


def _section_display_name(component: str, section: str) -> str:
    return _SECTION_DISPLAY.get((component, section), f"{component}/{section}")


def _get_api_provider_values(section: str) -> dict[str, Any] | None:
    """Fetch provider config for *section* from the API's /setup/current-config.

    Returns a flat dict (e.g. ``{"provider": "librewxr", "librewxr_endpoint": "..."}``),
    or None if the API is unreachable or the section has no provider configured.
    The API on the weewx host is the single authority for provider config;
    the local api.conf on weather-dev may be stale.
    """
    if _config_dir is None or section not in _PROVIDER_DOMAINS:
        return None
    try:
        from weewx_clearskies_config.wizard.known_apis import load_known_apis
        from weewx_clearskies_config.wizard.state_persistence import _read_secrets_env
        from weewx_clearskies_config.wizard.api_client import ApiClient

        known = load_known_apis(_config_dir)
        if not known:
            return None
        api_url = next(iter(known))
        secrets = _read_secrets_env(_config_dir)
        proxy_secret = secrets.get("WEEWX_CLEARSKIES_PROXY_SECRET")
        if not proxy_secret:
            return None
        client = ApiClient(api_url, proxy_secret=proxy_secret)
        config = client.get_current_config()
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch provider config from API for section %s", section, exc_info=True)
        return None

    providers = config.get("providers", {})
    provider_data = providers.get(section)
    if not provider_data or not isinstance(provider_data, dict):
        return None

    values: dict[str, Any] = {"provider": str(provider_data.get("provider", ""))}
    for key in ("librewxr_endpoint", "librewxr_bounds", "iframe_url",
                "aeris_forecast_model", "nws_user_agent_contact"):
        val = provider_data.get(key)
        if val:
            values[key] = str(val)
    return values


# ---------------------------------------------------------------------------
# Help content endpoint
# ---------------------------------------------------------------------------


@router.get("/help/{section_id}", response_class=HTMLResponse)
async def admin_help(request: Request, section_id: str) -> HTMLResponse:
    """Return help content fragment for an admin section.

    HTMX loads this into the help panel on first open.
    Keys: help.admin.{section_id}.title, help.admin.{section_id}.body,
          help.admin.{section_id}.tip (optional)
    """
    _require_session(request)
    locale = get_current_locale()
    title = translate(f"help.admin.{section_id}.title", locale)
    body = translate_md(f"help.admin.{section_id}.body", locale)
    tip_key = f"help.admin.{section_id}.tip"
    tip: str | None = translate(tip_key, locale)
    # If tip == tip_key, no translation exists — treat as absent.
    if tip == tip_key:
        tip = None
    assert _templates is not None, "Admin router not initialised"
    return _templates.TemplateResponse(
        request=request,
        name="wizard/help_fragment.html",
        context={"title": title, "body": body, "tip": tip},
    )


# ---------------------------------------------------------------------------
# Config dashboard and per-section editor (formerly config/routes.py)
# ---------------------------------------------------------------------------


@router.get("/config", response_class=HTMLResponse)
@router.get("/config/", response_class=HTMLResponse)
async def config_dashboard(request: Request) -> HTMLResponse:
    """Render the config dashboard — section nav + current value overview."""
    _require_session(request)
    assert _config_dir is not None

    all_sections = get_all_sections(_config_dir)

    # Build structured nav data: list of (component, section, display_name, values)
    nav_sections = []
    for comp, sec, display_name, _secrets in _SECTION_META:
        values = all_sections.get(comp, {}).get(sec, {})
        nav_sections.append({
            "component": comp,
            "section": sec,
            "display_name": display_name,
            "values": values,
        })

    return _render_config(
        request,
        "dashboard.html",
        {
            "nav_sections": nav_sections,
            "config_dir": str(_config_dir),
        },
    )


@router.get("/config/column-mapping", response_class=HTMLResponse)
async def column_mapping_get(request: Request) -> HTMLResponse:
    """Render the column mapping edit form."""
    _require_session(request)
    assert _config_dir is not None

    current_mapping = get_column_mapping(_config_dir)

    return _render_config(
        request,
        "section.html",
        {
            "component": "api",
            "section": "column_mapping",
            "display_name": "Column Mapping",
            "is_column_mapping": True,
            "mapping": current_mapping,
            "canonical_fields": _CANONICAL_FIELDS,
            "secret_fields": (),
            "values": {},
            "result": None,
            "error": None,
        },
    )


@router.post("/config/column-mapping", response_class=HTMLResponse)
async def column_mapping_post(request: Request) -> HTMLResponse:
    """Save column mapping and return result fragment."""
    _require_session(request)
    assert _config_dir is not None

    form = await request.form()

    # Form fields are named "col_<db_column>" for each mapping entry
    mapping: dict[str, str | None] = {}
    for key, value in form.multi_items():
        if key.startswith("col_"):
            db_col = key[4:]
            canonical = str(value).strip() or None
            mapping[db_col] = canonical

    error: str | None = None
    success = False
    try:
        update_column_mapping(mapping, _config_dir)
        success = True
    except FileNotFoundError as exc:
        error = str(exc)
        logger.warning("column_mapping_post FileNotFoundError: %s", exc)
    except Exception as exc:  # noqa: BLE001
        error = f"Unexpected error saving column mapping: {exc}"
        logger.exception("column_mapping_post unexpected error")

    return _render_config(
        request,
        "result.html",
        {
            "component": "api",
            "section": "column_mapping",
            "display_name": "Column Mapping",
            "success": success,
            "error": error,
        },
        status_code=500 if error else 200,
    )


@router.get("/config/{component}/{section}", response_class=HTMLResponse)
async def config_section_get(request: Request, component: str, section: str) -> HTMLResponse:
    """Render the edit form for one config section."""
    _require_session(request)
    assert _config_dir is not None

    # Validate component/section against known-good list to prevent path traversal
    if (component, section) not in _VALID_SECTIONS:
        return _render_config(
            request,
            "result.html",
            {
                "component": component,
                "section": section,
                "display_name": f"{component}/{section}",
                "success": False,
                "error": f"Unknown section: {component}/{section}",
            },
            status_code=404,
        )

    # Provider sections read from the API (authoritative) with local fallback.
    if section in _PROVIDER_DOMAINS:
        values = _get_api_provider_values(section) or get_section(component, section, _config_dir)
    else:
        values = get_section(component, section, _config_dir)
    secret_fields = _SECTION_SECRETS.get((component, section), ())

    # Build provider metadata from the single source of truth (wizard/providers.py).
    provider_meta: dict[str, dict] = {
        p.provider_id: {
            "display": p.display_name,
            "coverage": p.geographic_coverage,
            "keyless": len(p.auth_fields) == 0,
            "fields": list(p.auth_fields),
            "notes": p.notes,
            "signup_url": p.signup_url,
        }
        for p in PROVIDERS
    }
    domain_providers: dict[str, list[str]] = {}
    for p in PROVIDERS:
        domain_providers.setdefault(p.domain, []).append(p.provider_id)

    return _render_config(
        request,
        "section.html",
        {
            "component": component,
            "section": section,
            "display_name": _section_display_name(component, section),
            "is_column_mapping": False,
            "values": values,
            "secret_fields": secret_fields,
            "mapping": {},
            "canonical_fields": _CANONICAL_FIELDS,
            "provider_meta": provider_meta,
            "domain_providers": domain_providers,
            "result": None,
            "error": None,
        },
    )


@router.post("/config/{component}/{section}", response_class=HTMLResponse)
async def config_section_post(request: Request, component: str, section: str) -> HTMLResponse:
    """Save one config section via MANAGED REGION merge, return result fragment."""
    _require_session(request)
    assert _config_dir is not None

    # Validate component/section
    if (component, section) not in _VALID_SECTIONS:
        return _render_config(
            request,
            "result.html",
            {
                "component": component,
                "section": section,
                "display_name": f"{component}/{section}",
                "success": False,
                "error": f"Unknown section: {component}/{section}",
            },
            status_code=404,
        )

    form = await request.form()
    secret_fields = _SECTION_SECRETS.get((component, section), ())
    allowed_keys = _SECTION_ALLOWED_KEYS.get((component, section), frozenset())

    # Separate normal values from secret values.
    # Only accept keys in the allowed set (input validation at trust boundary).
    conf_values: dict[str, Any] = {}
    secret_values: dict[str, str] = {}

    for key, value in form.multi_items():
        if key not in allowed_keys:
            continue  # silently drop unexpected keys
        str_val = str(value).strip()
        if key in secret_fields:
            if str_val:
                secret_values[key] = str_val
        else:
            conf_values[key] = str_val

    # LibreWxR endpoint mode radio → resolve to actual endpoint URL.
    if section == "radar":
        endpoint_mode = str(form.get("librewxr_endpoint_mode", "")).strip()
        if endpoint_mode == "selfhosted":
            url_val = conf_values.get("librewxr_endpoint", "")
            if not url_val:
                conf_values["librewxr_endpoint"] = "https://api.librewxr.net"
        else:
            conf_values["librewxr_endpoint"] = "https://api.librewxr.net"

    error: str | None = None
    success = False

    try:
        conf_path = _config_dir / f"{component}.conf"
        if not conf_path.exists():
            raise FileNotFoundError(
                f"{component}.conf not found in config directory. "
                "Run the setup wizard first."
            )

        # Write secret fields into secrets.env
        for field_name, field_value in secret_values.items():
            env_key = _secrets_env_key(component, section, field_name)
            update_secrets(env_key, field_value, _config_dir)

        # ADR-027: [ui] enabled is not flippable from the UI — remove it
        # before writing so an operator cannot accidentally toggle it via the
        # config form.
        if component == "stack" and section == "ui":
            conf_values.pop("enabled", None)

        # Merge non-secret values into the managed region
        if conf_values:
            update_managed_region(conf_path, section, conf_values)

        success = True

    except FileNotFoundError as exc:
        error = str(exc)
        logger.warning("config_section_post FileNotFoundError: %s", exc)
    except ValueError as exc:
        error = f"Validation error: {exc}"
        logger.warning("config_section_post ValueError: %s", exc)
    except OSError as exc:
        error = f"File write error: {exc}"
        logger.error("config_section_post OSError: %s", exc)
    except Exception as exc:  # noqa: BLE001
        error = f"Unexpected error saving {component}/{section}: {exc}"
        logger.exception("config_section_post unexpected error")

    return _render_config(
        request,
        "result.html",
        {
            "component": component,
            "section": section,
            "display_name": _section_display_name(component, section),
            "success": success,
            "error": error,
        },
        status_code=500 if error else 200,
    )


@router.post("/config/test-provider", response_class=HTMLResponse)
async def config_test_provider(request: Request) -> HTMLResponse:
    """Test provider connectivity; return a result fragment."""
    _require_session(request)

    form = await request.form()
    provider_id = str(form.get("provider_id", "")).strip()
    info = get_provider(provider_id)

    if not info:
        return _render_config(
            request,
            "result.html",
            {
                "component": "api",
                "section": "test-provider",
                "display_name": "Provider Test",
                "success": False,
                "error": f"Unknown provider: {provider_id!r}",
                "test_result": {"success": False, "error": f"Unknown provider: {provider_id}"},
            },
            status_code=404,
        )

    credentials: dict[str, str] = {}
    for field_name in info.auth_fields:
        credentials[field_name] = str(form.get(field_name, "")).strip()

    result = test_provider(info, credentials)

    return _render_config(
        request,
        "result.html",
        {
            "component": "api",
            "section": "test-provider",
            "display_name": f"Provider Test — {info.display_name}",
            "success": result.get("success", False),
            "error": result.get("error"),
            "test_result": result,
            "test_provider_id": provider_id,
            "test_provider_name": info.display_name,
        },
    )


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------


# Human-readable labels for registry domain groups shown on the landing page.
_GROUP_LABELS: dict[str, str] = {
    "station": "Station",
    "providers": "Providers",
    "appearance": "Appearance",
    "dashboard": "Dashboard",
    "advanced": "Advanced",
    "cards": "Card Settings",
}

# Custom sections that exist as dedicated routes but are not in the registry.
# Each entry carries the fields shown as a card on the landing overview.
# "url" is the HTMX GET target; "description" is a one-line summary shown
# instead of a dl when there are no landing_display fields to render.
_CUSTOM_SECTIONS: list[dict] = [
    {
        "section_id": "status",
        "display_name": "Status",
        "group": "station",
        "url": "/admin/status",
        "description": "",
    },
    {
        "section_id": "station-identity",
        "display_name": "Station Identity",
        "group": "station",
        "url": "/admin/config/stack/ui",
        "description": "",
    },
    {
        "section_id": "database",
        "display_name": "Database",
        "group": "station",
        "url": "/admin/config/api/database",
        "description": "",
    },
    {
        "section_id": "connection",
        "display_name": "API Connection",
        "group": "station",
        "url": "/admin/connection",
        "description": "",
    },
    {
        "section_id": "forecast-provider",
        "display_name": "Forecast",
        "group": "providers",
        "url": "/admin/config/api/forecast",
        "description": "",
    },
    {
        "section_id": "alerts-provider",
        "display_name": "Alerts",
        "group": "providers",
        "url": "/admin/config/api/alerts",
        "description": "",
    },
    {
        "section_id": "aqi-provider",
        "display_name": "AQI",
        "group": "providers",
        "url": "/admin/config/api/aqi",
        "description": "",
    },
    {
        "section_id": "earthquakes-provider",
        "display_name": "Earthquakes Provider",
        "group": "providers",
        "url": "/admin/config/api/earthquakes",
        "description": "",
    },
    {
        "section_id": "radar-provider",
        "display_name": "Radar",
        "group": "providers",
        "url": "/admin/config/api/radar",
        "description": "",
    },
    {
        "section_id": "marine-service",
        "display_name": "Marine Service",
        "group": "providers",
        "url": "/admin/marine-service",
        "description": "Connection to the marine companion service that provides all marine features.",
    },
    {
        "section_id": "now-layout",
        "display_name": "Now Page Layout",
        "group": "dashboard",
        "url": "/admin/now-layout",
        "description": "Drag-and-drop card arrangement for the Now page.",
    },
    {
        "section_id": "column-mapping",
        "display_name": "Column Mapping",
        "group": "dashboard",
        "url": "/admin/config/column-mapping",
        "description": "Map database columns to weewx canonical field names.",
    },
    {
        "section_id": "haze-calibration",
        "display_name": "Haze Calibration",
        "group": "advanced",
        "url": "/admin/haze-calibration",
        "description": "",
    },
    {
        "section_id": "basemap",
        "display_name": "Basemap",
        "group": "advanced",
        "url": "/admin/basemap",
        "description": "The map ground under every Clear Skies map: world, local and radar tiers extracted from OpenStreetMap data (Protomaps).",
    },
    {
        "section_id": "forecast-correction",
        "display_name": "Forecast Correction",
        "group": "advanced",
        "url": "/admin/forecast-correction",
        "description": "Temperature bias correction using forecast-observation pairs and Random Forest regression.",
    },
    {
        "section_id": "marine",
        "display_name": "Marine Locations",
        "group": "advanced",
        "url": "/admin/marine",
        "description": "Configure marine, surf, fishing, and beach safety locations.",
    },
    {
        "section_id": "surf-scoring",
        "display_name": "Surf Score Weights",
        "group": "advanced",
        "url": "/admin/surf-scoring",
        "description": "Operator-adjustable importance weights for the five surf score components.",
    },
]


@router.get("", response_class=HTMLResponse, response_model=None)
@router.get("/", response_class=HTMLResponse, response_model=None)
async def admin_landing(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the admin landing page — registry-driven domain-organized overview."""
    _require_session(request)
    assert _config_dir is not None

    api_conf = _config_dir / "api.conf"
    if not api_conf.exists():
        return RedirectResponse("/wizard", status_code=303)

    from weewx_clearskies_config.registry import registry

    # Build ordered domain group list: priority groups first (station, providers
    # are custom-only — not in the registry), then registry groups, then any
    # remaining custom-only groups.
    registry_groups = list(registry.get_all_domain_groups())
    all_group_names = []
    seen: set[str] = set()
    # Prepend custom-only groups that come before any registry group (station, providers)
    _priority_groups = ["station", "providers"]
    for g in _priority_groups:
        if g not in seen:
            all_group_names.append(g)
            seen.add(g)
    for g in registry_groups:
        if g not in seen:
            all_group_names.append(g)
            seen.add(g)
    # Any remaining custom-only groups not yet added
    for cs in _CUSTOM_SECTIONS:
        g = cs["group"]
        if g not in seen:
            all_group_names.append(g)
            seen.add(g)

    domain_groups = [(g, _GROUP_LABELS.get(g, g.title())) for g in all_group_names]

    # Build sections_by_group: registry sections first, then custom sections, per group
    sections_by_group: dict[str, list] = {g: [] for g in all_group_names}
    for g in all_group_names:
        for section in registry.get_sections_for_group(g):
            sections_by_group[g].append({"type": "registry", "section": section})
    for cs in _CUSTOM_SECTIONS:
        g = cs["group"]
        if g in sections_by_group:
            sections_by_group[g].append({"type": "custom", "section": cs})

    # Collect landing display field values for registry sections that have
    # fields with admin_landing_display=True.
    landing_values: dict[str, list[tuple[str, Any]]] = {}
    for g in all_group_names:
        for entry in sections_by_group[g]:
            if entry["type"] != "registry":
                continue
            section = entry["section"]
            fields = registry.get_fields_for_section(section.section_id)
            display_fields = [f for f in fields if f.admin_landing_display]
            if display_fields:
                values = _read_section_values(section, fields)
                landing_values[section.section_id] = [
                    (f.label, values.get(f.config_key, f.default))
                    for f in display_fields
                ]

    # Collect custom-section landing values: provider "provider" keys, connection URL,
    # station identity highlights, haze calibration summary.
    api_providers = _fetch_api_providers()
    from weewx_clearskies_config.wizard.known_apis import load_known_apis
    known = load_known_apis(_config_dir)
    connection_url = next(iter(known), "")
    api_section = get_section("api", "api", _config_dir)
    connection_bind = (
        f"{api_section.get('bind_host', '')}:{api_section.get('bind_port', '8765')}"
        if api_section.get("bind_host") else ""
    )
    ui_values = get_section("stack", "ui", _config_dir)
    db_values = get_section("api", "database", _config_dir)
    haze_values = _get_with_defaults(get_section("api", "conditions", _config_dir), _HAZE_DEFAULTS)
    haze_calibration = _read_calibration_state()

    _provider_domains = {
        "forecast-provider": "forecast",
        "alerts-provider": "alerts",
        "aqi-provider": "aqi",
        "earthquakes-provider": "earthquakes",
        "radar-provider": "radar",
    }
    custom_landing_values: dict[str, list[tuple[str, Any]]] = {}

    if ui_values:
        rows: list[tuple[str, Any]] = []
        if ui_values.get("station_name"):
            rows.append(("Name", ui_values["station_name"]))
        if ui_values.get("timezone"):
            rows.append(("Timezone", ui_values["timezone"]))
        if ui_values.get("latitude") and ui_values.get("longitude"):
            rows.append(("Coordinates", f"{ui_values['latitude']}, {ui_values['longitude']}"))
        if rows:
            custom_landing_values["station-identity"] = rows

    if db_values:
        rows = []
        if db_values.get("host"):
            port_str = f":{db_values['port']}" if db_values.get("port") else ""
            rows.append(("Host", f"{db_values['host']}{port_str}"))
        if db_values.get("name"):
            rows.append(("Database", db_values["name"]))
        if db_values.get("user"):
            rows.append(("User", db_values["user"]))
        if rows:
            custom_landing_values["database"] = rows

    if connection_url:
        rows = [("API URL", connection_url)]
        if connection_bind:
            rows.append(("Bind", connection_bind))
        custom_landing_values["connection"] = rows

    for cs_id, domain in _provider_domains.items():
        pdata = api_providers.get(domain) or get_section("api", domain, _config_dir)
        provider_val = pdata.get("provider", "") if pdata else ""
        custom_landing_values[cs_id] = [("Provider", provider_val or "not set")]

    # Haze calibration summary
    haze_rows: list[tuple[str, Any]] = [("Detection", haze_values.get("haze_detection", "true"))]
    if haze_calibration is None:
        haze_rows.append(("Status", "API unreachable"))
    else:
        haze_rows.append(("Status",
            haze_calibration.get("overall_state", "no-data").replace("-", " ").capitalize()))
        haze_rows.append(("Months calibrated", f"{haze_calibration.get('months_calibrated', 0)} / 12"))
        if haze_calibration.get("openaq_sensor"):
            s = haze_calibration["openaq_sensor"]
            haze_rows.append(("Sensor", f"{s['name']} ({s['distance_km']:.1f} km)"))
    custom_landing_values["haze-calibration"] = haze_rows

    # Basemap summary
    basemap_status = _fetch_basemap_status()
    basemap_rows: list[tuple[str, Any]] = []
    if basemap_status is None:
        basemap_rows.append(("Status", "API unreachable"))
    else:
        for tier_id, tier_label in (("world", "World"), ("local", "Local"), ("radar", "Radar")):
            tier = basemap_status.get(tier_id) or {}
            if tier.get("available"):
                size_bytes = tier.get("size_bytes")
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB" if size_bytes else "unknown size"
                basemap_rows.append((tier_label, f"Available ({size_str})"))
            else:
                basemap_rows.append((tier_label, "Not extracted"))
        if basemap_status.get("updating"):
            basemap_rows.append(("Status", "Updating…"))
    custom_landing_values["basemap"] = basemap_rows

    # Marine locations summary (T6.2)
    marine_config = _fetch_current_config()
    marine_rows: list[tuple[str, Any]] = []
    if marine_config is None:
        marine_rows.append(("Status", "API unreachable"))
    else:
        marine_locations = _parse_marine_locations(marine_config.get("marine") or {})
        if marine_locations:
            marine_rows.append(("Locations", str(len(marine_locations))))
        else:
            marine_rows.append(("Status", "Not configured"))
    custom_landing_values["marine"] = marine_rows

    # Marine Service connection summary (T7.5)
    marine_service_rows: list[tuple[str, Any]] = []
    if marine_config is None:
        marine_service_rows.append(("Status", "API unreachable"))
    else:
        marine_service_url = marine_config.get("marine_service_url", "")
        if marine_service_url:
            marine_service_rows.append(("Service URL", marine_service_url))
        else:
            marine_service_rows.append(("Status", "Not configured"))
    custom_landing_values["marine-service"] = marine_service_rows

    # Surf score weights summary (Round S, ADR-101 guidance 6)
    surf_scoring_rows: list[tuple[str, Any]] = []
    if marine_config is None:
        surf_scoring_rows.append(("Status", "API unreachable"))
    else:
        configured_weights = _parse_surf_scoring_weights(marine_config.get("surf_scoring"))
        if configured_weights is None:
            surf_scoring_rows.append(("Status", "Defaults"))
        else:
            shares = _surf_scoring_shares(configured_weights)
            surf_scoring_rows.append(
                ("Weights", " / ".join(f"{shares[k]:.0f}%" for k in _SURF_SCORING_DEFAULTS))
            )
    custom_landing_values["surf-scoring"] = surf_scoring_rows

    return _render(
        request,
        "landing.html",
        {
            "domain_groups": domain_groups,
            "sections_by_group": sections_by_group,
            "landing_values": landing_values,
            "custom_landing_values": custom_landing_values,
        },
    )


# ---------------------------------------------------------------------------
# T2.1 — Generic registry section handler
# ---------------------------------------------------------------------------


def _find_section(section_id: str):  # type: ignore[return]
    """Look up a section in the registry by section_id.

    Raises HTTPException 404 if not found.
    """
    from fastapi import HTTPException
    from weewx_clearskies_config.registry import registry

    for group in registry.get_all_domain_groups():
        for s in registry.get_sections_for_group(group):
            if s.section_id == section_id:
                return s
    raise HTTPException(status_code=404, detail=f"Unknown section: {section_id}")


def _read_section_values(section, fields) -> dict:  # type: ignore[return]
    """Read current field values for *section* from the appropriate backend.

    Returns a dict keyed by config_key (matching the field declarations).
    For .conf-based sources, get_section() returns keys that should already
    match the config_key names.  For JSON sources (branding.json, pages.json)
    the mapping is handled here.
    """
    assert _config_dir is not None
    source = section.config_source

    if source == "stack.conf":
        return get_section("stack", section.section_id, _config_dir)

    elif source == "api.conf":
        return get_section("api", section.section_id, _config_dir)

    elif source == "branding.json":
        branding = read_branding(_config_dir)
        # Build result keyed by config_key for each field
        values: dict = {}
        for field in fields:
            key = field.config_key
            # config_target may be "branding.json" (top-level) or
            # "branding.json:<sub_key>" (nested dict)
            target = field.config_target
            if ":" in target:
                sub = target.split(":", 1)[1]
                nested = branding.get(sub, {})
                values[key] = nested.get(key, field.default)
            else:
                values[key] = branding.get(key, field.default)
        return values

    elif source == "pages.json":
        pages_data = read_pages(_config_dir)
        hidden: list = pages_data.get("hidden", [])
        # The pages section has one field: hidden_pages (checkbox_group)
        # The value is the list of hidden slugs
        return {"hidden_pages": hidden}

    else:
        logger.warning(
            "_read_section_values: unknown config_source %r for section %s",
            source,
            section.section_id,
        )
        return {}


@router.get("/section/{section_id}", response_class=HTMLResponse)
async def generic_section_get(request: Request, section_id: str) -> HTMLResponse:
    """Render a registry-defined section edit form."""
    _require_session(request)
    assert _config_dir is not None

    from weewx_clearskies_config.registry import registry

    section = _find_section(section_id)
    fields = registry.get_fields_for_section(section_id)
    values = _read_section_values(section, fields)

    template_name = section.custom_template if section.custom_template else "generic_section.html"
    return _render(request, template_name, {
        "section": section,
        "fields": fields,
        "values": values,
    })


@router.post("/section/{section_id}", response_class=HTMLResponse)
async def generic_section_post(request: Request, section_id: str) -> HTMLResponse:
    """Save a registry-defined section and return result fragment."""
    _require_session(request)
    assert _config_dir is not None

    from weewx_clearskies_config.registry import registry
    from weewx_clearskies_config.registry.validation import (
        validate_form_against_fields,
        extract_field_values,
        save_field_values,
    )

    section = _find_section(section_id)
    fields = registry.get_fields_for_section(section_id)

    form = await request.form()
    # Collect multi-value fields (e.g. checkbox_group) as lists
    form_data: dict = {}
    for field in fields:
        raw = form.getlist(field.config_key)
        if len(raw) > 1:
            form_data[field.config_key] = raw
        elif len(raw) == 1:
            form_data[field.config_key] = raw[0]
        # absent keys stay absent (boolean fields → False, etc.)

    errors = validate_form_against_fields(form_data, fields)
    if errors:
        values = _read_section_values(section, fields)
        return _render(request, "generic_section.html", {
            "section": section,
            "fields": fields,
            "values": values,
            "error": "; ".join(errors),
        }, status_code=422)

    values = extract_field_values(form_data, fields)

    error: str | None = None
    success = False
    try:
        save_field_values(values, section, str(_config_dir))
        success = True
    except Exception as exc:  # noqa: BLE001
        error = _("Error saving: {detail}").format(detail=exc)
        logger.exception("generic_section_post error for %s", section_id)

    return _render_result(
        request,
        section_slug=section_id,
        display_name=section.display_name,
        success=success,
        error=error,
        status_code=500 if error else 200,
    )


# ---------------------------------------------------------------------------
# API Connection
# ---------------------------------------------------------------------------


@router.get("/connection", response_class=HTMLResponse)
async def connection_get(request: Request) -> HTMLResponse:
    """Render the API connection settings form."""
    _require_session(request)
    assert _config_dir is not None

    from weewx_clearskies_config.wizard.known_apis import load_known_apis

    known = load_known_apis(_config_dir)
    api_url = next(iter(known), "")
    api_values = get_section("api", "api", _config_dir)
    bind_host = api_values.get("bind_host", "")
    bind_port = api_values.get("bind_port", "8765")

    return _render(
        request,
        "connection.html",
        {
            "api_url": api_url,
            "bind_host": bind_host,
            "bind_port": bind_port,
        },
    )


@router.post("/connection", response_class=HTMLResponse)
async def connection_post(request: Request) -> HTMLResponse:
    """Save API connection settings, update caddy.env, and reload Caddy."""
    _require_session(request)
    assert _config_dir is not None

    form = await request.form()
    api_url = str(form.get("api_url", "")).strip()

    error: str | None = None
    success = False

    if not api_url:
        error = _("API URL is required.")
    elif not api_url.startswith("https://"):
        error = _("API URL must start with https://")

    if not error:
        try:
            from weewx_clearskies_config.wizard.known_apis import (
                load_known_apis,
                save_known_api,
            )
            from weewx_clearskies_config.wizard.config_writer import write_caddy_env
            from weewx_clearskies_config.wizard.state import WizardState

            known = load_known_apis(_config_dir)
            old_url = next(iter(known), None)
            old_fp = known.get(old_url, "") if old_url else ""

            if old_url and old_url != api_url:
                # Remove old entry, re-pin under new URL with same fingerprint.
                # Operator can re-run wizard if cert changed too.
                known_path = _config_dir / "known_apis.json"
                import json as _json

                new_known = {api_url: old_fp}
                known_path.write_text(_json.dumps(new_known, indent=2), encoding="utf-8")
                logger.info("Updated known_apis.json: %s -> %s", old_url, api_url)
            elif not old_url:
                save_known_api(_config_dir, api_url, "")

            # Write caddy.env
            stub_state = WizardState(api_address=api_url)
            write_caddy_env(stub_state, _config_dir)

            # Reload Caddy
            import subprocess

            subprocess.run(
                ["sudo", "systemctl", "reload", "caddy"],
                check=True,
                capture_output=True,
                timeout=10,
            )
            logger.info("connection_post: Caddy reloaded after API URL change to %s", api_url)
            success = True
        except OSError as exc:
            error = _("File write error: {detail}").format(detail=exc)
            logger.error("connection_post OSError: %s", exc)
        except subprocess.CalledProcessError as exc:
            error = _("Caddy reload failed: {detail}").format(
                detail=exc.stderr.decode() if exc.stderr else exc
            )
            logger.error("connection_post Caddy reload failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            error = _("Unexpected error: {detail}").format(detail=exc)
            logger.exception("connection_post unexpected error")

    return _render_result(
        request,
        section_slug="connection",
        display_name=_("API Connection"),
        success=success,
        error=error,
        status_code=500 if error else 200,
    )



# ---------------------------------------------------------------------------
# T8.2b — Haze calibration
# ---------------------------------------------------------------------------


@router.get("/openaq-sensors-fragment", response_class=HTMLResponse)
async def openaq_sensors_fragment(request: Request) -> HTMLResponse:
    """Return HTMX fragment with a select of nearby reference sensors.

    The returned HTML is a ``<select>`` that, when the user picks an option,
    copies the sensor ID into the manual-entry ``<input id="manual_sensor_id">``
    on the same page.  No separate form submission — one submission path only.
    """
    _require_session(request)
    client = _get_api_client()
    sensors: list[dict] = []
    error: str | None = None

    if client is None:
        error = _("Cannot connect to API.")
    else:
        try:
            response = client._request("GET", "/setup/openaq-sensors")
            data = response.json()
            sensors = data.get("sensors", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("openaq_sensors_fragment: could not load sensors: %s", exc)
            error = _("Could not load sensors: {detail}").format(detail=exc)

    if error:
        escaped = error.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return HTMLResponse(
            f'<p style="font-size:0.875rem;color:var(--pico-del-color)">{escaped}</p>'
        )
    if not sensors:
        return HTMLResponse(
            '<p style="font-size:0.875rem;color:var(--pico-muted-color)">'
            + _("No reference sensors found within 25 km.")
            + "</p>"
        )

    def _esc(v: object) -> str:
        return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    options = [f'<option value="">{_esc(_("— Select a sensor —"))}</option>']
    for s in sensors:
        sensor_id = _esc(s.get("sensor_id", ""))
        label = _esc(
            f"{s.get('name', '?')} ({float(s.get('distance_km', 0)):.1f} km, ID: {s.get('sensor_id', '?')})"
        )
        options.append(f'<option value="{sensor_id}">{label}</option>')

    select_html = (
        f'<label for="sensor-select" style="font-size:0.875rem">{_esc(_("Reference sensors nearby"))}</label>'
        f'<select id="sensor-select" aria-label="{_esc(_("Select a reference sensor"))}"'
        ' onchange="document.getElementById(\'manual_sensor_id\').value = this.value">'
        + "".join(options)
        + "</select>"
        f'<small style="display:block;margin-block:0.25rem">{_esc(_("Select a sensor to populate the ID field below, then click “Set sensor override.” Only reference-grade (AQMD/regulatory) monitors are listed."))}</small>'
    )
    return HTMLResponse(select_html)


@router.get("/haze-calibration", response_class=HTMLResponse)
async def haze_calibration_get(request: Request) -> HTMLResponse:
    """Render the haze calibration settings and status form."""
    _require_session(request)
    assert _config_dir is not None
    values = _get_with_defaults(
        get_section("api", "conditions", _config_dir), _HAZE_DEFAULTS
    )
    cal_state = _read_calibration_state()
    return _render(request, "haze_calibration.html", {
        "values": values,
        "defaults": _HAZE_DEFAULTS,
        "calibration": cal_state,
    })


@router.post("/haze-calibration", response_class=HTMLResponse)
async def haze_calibration_post(request: Request) -> HTMLResponse:
    """Save haze calibration settings and return result fragment."""
    _require_session(request)
    assert _config_dir is not None
    form = await request.form()
    values = {
        "haze_detection": "true" if form.get("haze_detection") else "false",
        "gamma": _safe_float_range(form, "gamma", 0.1, 1.0, _HAZE_DEFAULTS),
    }
    # openaq_sensor_id: only update when present in form (may be empty string to clear)
    if "openaq_sensor_id" in form:
        raw_sensor_id = str(form["openaq_sensor_id"]).strip()
        # Accept empty (clear) or positive integer only
        if raw_sensor_id == "" or (raw_sensor_id.isdigit() and int(raw_sensor_id) > 0):
            values["openaq_sensor_id"] = raw_sensor_id
        # else: ignore invalid value, don't persist it
    api_conf = _config_dir / "api.conf"
    error: str | None = None
    success = False
    try:
        if not api_conf.exists():
            raise FileNotFoundError("api.conf not found — run the setup wizard first.")
        update_managed_region(api_conf, "conditions", values)
        success = True
    except FileNotFoundError as exc:
        error = str(exc)
        logger.warning("haze_calibration_post FileNotFoundError: %s", exc)
    except OSError as exc:
        error = _("File write error: {detail}").format(detail=exc)
        logger.error("haze_calibration_post OSError: %s", exc)
    except Exception as exc:  # noqa: BLE001
        error = _("Unexpected error: {detail}").format(detail=exc)
        logger.exception("haze_calibration_post unexpected error")
    return _render_result(request, section_slug="haze-calibration",
        display_name=_("Haze Calibration"), success=success, error=error,
        status_code=500 if error else 200)


@router.post("/haze-calibration/reset", response_class=HTMLResponse)
async def haze_calibration_reset(request: Request) -> HTMLResponse:
    """Reset calibration data via the API and return result fragment."""
    _require_session(request)
    client = _get_api_client()
    error: str | None = None
    success = False
    if client is None:
        error = _("Cannot connect to API — check that the API is running and configured.")
    else:
        try:
            response = client._request("POST", "/setup/calibration-reset")
            data = response.json()
            success = data.get("success", False)
            if not success:
                error = data.get("message", _("Reset failed — unknown error."))
        except Exception as exc:  # noqa: BLE001
            error = _("API error: {detail}").format(detail=exc)
            logger.warning("calibration_reset API error: %s", exc)
    return _render_result(request, section_slug="haze-calibration",
        display_name=_("Haze Calibration"), success=success, error=error,
        status_code=500 if error else 200)


# ---------------------------------------------------------------------------
# M1 — Basemap (Protomaps) admin
# ---------------------------------------------------------------------------


@router.get("/basemap", response_class=HTMLResponse)
async def basemap_get(request: Request) -> HTMLResponse:
    """Render the basemap status page."""
    _require_session(request)
    status = _fetch_basemap_status()
    error: str | None = None
    if status is None:
        error = _("Cannot connect to the API — check that the API is running and configured.")
    return _render(request, "basemap.html", {
        "status": status,
        "error": error,
    })


@router.post("/basemap/update", response_class=HTMLResponse)
async def basemap_update(request: Request) -> HTMLResponse:
    """Trigger a basemap extract (world/local/radar tiers) via the API."""
    _require_session(request)
    from weewx_clearskies_config.wizard.api_client import ApiClientError
    client = _get_api_client()
    error: str | None = None
    flash: str | None = None
    if client is None:
        error = _("Cannot connect to the API — check that the API is running and configured.")
    else:
        try:
            client._request("POST", "/setup/basemap/update")
            flash = _("Basemap update started — this page refreshes while it runs.")
        except ApiClientError as exc:
            if exc.status_code == 409:
                flash = _("A basemap update is already running.")
            else:
                error = _("API error: {detail}").format(detail=exc.detail)
                logger.warning("basemap_update API error: %s", exc)
        except Exception as exc:  # noqa: BLE001
            error = _("API error: {detail}").format(detail=exc)
            logger.warning("basemap_update API error: %s", exc)
    return _render(request, "basemap.html", {
        "status": _fetch_basemap_status(),
        "error": error,
        "flash": flash,
    }, status_code=500 if error else 200)


# ---------------------------------------------------------------------------
# Phase 7 — Forecast correction admin
# ---------------------------------------------------------------------------


@router.get("/forecast-correction", response_class=HTMLResponse)
async def forecast_correction_get(request: Request) -> HTMLResponse:
    """Render the forecast correction status, metrics, and controls page."""
    _require_session(request)
    assert _config_dir is not None
    values = _get_with_defaults(
        get_section("api", "forecast_correction", _config_dir), _FC_DEFAULTS
    )
    status = _fetch_forecast_correction_status()
    # Format epoch timestamps in station-local time for the template.
    fmt_start = fmt_end = None
    if status:
        from datetime import datetime, timezone as _tz  # noqa: PLC0415
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        # The API's current-config or station endpoint provides the timezone;
        # fall back to reading it from the correction status response or api.conf.
        station_tz_name = None
        client = _get_api_client()
        if client is not None:
            try:
                resp = client._request("GET", "/api/v1/station")
                station_tz_name = resp.json().get("data", {}).get("timezone")
            except Exception:  # noqa: BLE001
                pass
        try:
            tz = ZoneInfo(station_tz_name) if station_tz_name else _tz.utc
        except (KeyError, ValueError):
            tz = _tz.utc
        for key, target in [("date_range_start", "start"), ("date_range_end", "end")]:
            epoch = status.get(key)
            if epoch is not None:
                try:
                    dt = datetime.fromtimestamp(int(epoch), tz=_tz.utc).astimezone(tz)
                    formatted = dt.strftime("%Y-%m-%d %H:%M %Z")
                    if target == "start":
                        fmt_start = formatted
                    else:
                        fmt_end = formatted
                except (ValueError, OSError, OverflowError):
                    pass
    return _render(request, "forecast_correction.html", {
        "values": values,
        "defaults": _FC_DEFAULTS,
        "status": status,
        "fmt_date_start": fmt_start,
        "fmt_date_end": fmt_end,
    })


@router.post("/forecast-correction/toggle", response_class=HTMLResponse)
async def forecast_correction_toggle(request: Request) -> HTMLResponse:
    """Toggle forecast correction and/or pair collection via the API."""
    _require_session(request)
    client = _get_api_client()
    error: str | None = None
    success = False
    if client is None:
        error = _("Cannot connect to API — check that the API is running and configured.")
    else:
        form = await request.form()
        body = {
            "enabled": "enabled" in form,
            "collection_enabled": "collection_enabled" in form,
        }
        try:
            client._request("POST", "/setup/forecast-correction/toggle", json=body)
            success = True
        except Exception as exc:  # noqa: BLE001
            error = _("API error: {detail}").format(detail=exc)
            logger.warning("forecast_correction_toggle API error: %s", exc)
    return _render_result(
        request,
        section_slug="forecast-correction",
        display_name=_("Forecast Correction"),
        success=success,
        error=error,
        status_code=500 if error else 200,
    )


@router.post("/forecast-correction/retrain", response_class=HTMLResponse)
async def forecast_correction_retrain(request: Request) -> HTMLResponse:
    """Trigger a model retrain via the API."""
    _require_session(request)
    client = _get_api_client()
    error: str | None = None
    success = False
    if client is None:
        error = _("Cannot connect to API — check that the API is running and configured.")
    else:
        try:
            response = client._request("POST", "/setup/forecast-correction/retrain")
            data = response.json()
            success = data.get("success", False)
            if not success:
                error = data.get("message", _("Training did not complete."))
        except Exception as exc:  # noqa: BLE001
            error = _("API error: {detail}").format(detail=exc)
            logger.warning("forecast_correction_retrain API error: %s", exc)
    return _render_result(
        request,
        section_slug="forecast-correction",
        display_name=_("Forecast Correction"),
        success=success,
        error=error,
        status_code=500 if error else 200,
    )


# ---------------------------------------------------------------------------
# Round S — Surf score weights admin (ADR-101 guidance 6)
# ---------------------------------------------------------------------------


def _parse_surf_scoring_weights(raw: Any) -> dict[str, float] | None:
    """Parse the API's current-config top-level ``surf_scoring`` section into
    the five positive floats, or None when the section is absent/empty/not a
    dict (→ the caller shows the ADR-101 defaults).

    Mirrors the marine service's per-key tolerance (its
    ``SurfScoreWeightsConfig``): an individually malformed or non-positive
    stored value falls back to that key's default rather than discarding the
    whole section — the form then shows what the scorer would actually use.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    weights: dict[str, float] = {}
    for key, default in _SURF_SCORING_DEFAULTS.items():
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = float(default)
        if not math.isfinite(value) or value <= 0:
            value = float(default)
        weights[key] = value
    return weights


def _surf_scoring_shares(weights: dict[str, float]) -> dict[str, float]:
    """Effective percentage per component: value ÷ sum × 100 (ADR-101 —
    the scorer normalizes by sum at computation time, so only proportions
    matter)."""
    total = sum(weights.values())
    return {key: value / total * 100.0 for key, value in weights.items()}


def _surf_scoring_components() -> list[tuple[str, str]]:
    """Ordered (config_key, translated label) pairs for the five components.

    Order and vocabulary match the visitor-facing score bars (ADR-101
    display: Size / Shape / Conditions / Power / Consistency) so the
    operator adjusts the same names visitors see on the dashboard. Built
    per-request, not at import time, so labels follow the active locale.
    """
    return [
        ("weight_size", _("Size")),
        ("weight_shape", _("Shape")),
        ("weight_conditions", _("Conditions")),
        ("weight_power", _("Power")),
        ("weight_consistency", _("Consistency")),
    ]


def _format_weight(value: float) -> str:
    """Display format for a weight input value — trims float noise
    (0.25 → "0.25", 0.2 → "0.2") without locale formatting, because the
    value lands in an HTML number input, which always takes "." decimals."""
    return f"{value:g}"


@router.get("/surf-scoring", response_class=HTMLResponse)
async def surf_scoring_get(request: Request) -> HTMLResponse:
    """Render the surf score weights form, pre-filled from the API's
    current-config top-level ``surf_scoring`` section — ADR-101 shipped
    defaults when the section is absent (defaults live in code, not
    config)."""
    _require_session(request)
    config = _fetch_current_config()
    if config is None:
        return _render(request, "surf_scoring.html", {
            "api_unreachable": True,
            "components": _surf_scoring_components(),
        })
    weights = _parse_surf_scoring_weights(config.get("surf_scoring"))
    is_default = weights is None
    if weights is None:
        weights = {k: float(v) for k, v in _SURF_SCORING_DEFAULTS.items()}
    return _render(request, "surf_scoring.html", {
        "api_unreachable": False,
        "components": _surf_scoring_components(),
        "values": {k: _format_weight(v) for k, v in weights.items()},
        "shares": _surf_scoring_shares(weights),
        "is_default": is_default,
        "defaults": _SURF_SCORING_DEFAULTS,
        "errors": {},
        "error_summary": None,
    })


@router.post("/surf-scoring", response_class=HTMLResponse)
async def surf_scoring_post(request: Request) -> HTMLResponse:
    """Validate the five weights and save them as the top-level
    ``[surf_scoring]`` section via POST /setup/apply — the existing
    admin → API path; the API pushes the section on to the marine service's
    /config (the admin never talks to the marine service directly).

    Zero, negative, non-numeric (including NaN/inf) and missing values are
    rejected here with a 422 re-render (ADR-101 guidance 6: "reject
    zero/negative values at the form"). The scorer normalizes by sum, so any
    positive combination is valid — there is deliberately no combination
    check.
    """
    _require_session(request)
    form = await request.form()
    components = _surf_scoring_components()
    entered: dict[str, str] = {}
    errors: dict[str, str] = {}
    weights: dict[str, float] = {}
    for key, _label in components:
        raw = str(form.get(key, "")).strip()
        entered[key] = raw
        try:
            value = float(raw)
        except ValueError:
            errors[key] = _("Enter a positive number.")
            continue
        if not math.isfinite(value) or value <= 0:
            errors[key] = _("Enter a positive number.")
            continue
        weights[key] = value
    if errors:
        return _render(request, "surf_scoring.html", {
            "api_unreachable": False,
            "components": components,
            "values": entered,
            "shares": None,
            "is_default": False,
            "defaults": _SURF_SCORING_DEFAULTS,
            "errors": errors,
            "error_summary": _("All five weights must be positive numbers. Nothing was saved."),
        }, status_code=422)

    error: str | None = None
    success = False
    config = _fetch_current_config()
    client = _get_api_client()
    if config is None or client is None:
        error = _("Cannot connect to API — check that the API is running and configured.")
    else:
        payload = _build_base_apply_payload(config)
        payload["surf_scoring"] = {key: weights[key] for key, _label in components}
        try:
            client.apply(payload)
            success = True
        except Exception as exc:  # noqa: BLE001
            error = _("API error: {detail}").format(detail=exc)
            logger.warning("surf_scoring_post API error: %s", exc)
    return _render_result(
        request,
        section_slug="surf-scoring",
        display_name=_("Surf Score Weights"),
        success=success,
        error=error,
        status_code=500 if error else 200,
    )


# ---------------------------------------------------------------------------
# T4.2 — Card layout editor helpers
# ---------------------------------------------------------------------------


def _read_card_manifest() -> list[dict]:
    """Read card-manifest.json from the dashboard web root."""
    if _dashboard_root is None:
        return []
    manifest_path = _dashboard_root / "card-manifest.json"
    if not manifest_path.exists():
        return []
    try:
        with open(manifest_path) as f:
            data = json.load(f)
        return data.get("cards", [])
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read card-manifest.json from %s", manifest_path)
        return []


def _read_now_layout() -> dict:
    """Read now-layout.json from config dir, or return default empty layout."""
    if _config_dir is None:
        return {"version": 1, "cards": []}
    layout_path = _config_dir / "now-layout.json"
    if not layout_path.exists():
        return {"version": 1, "cards": []}
    try:
        with open(layout_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read now-layout.json from %s", layout_path)
        return {"version": 1, "cards": []}


# ---------------------------------------------------------------------------
# T4.2 — Card layout editor routes
# ---------------------------------------------------------------------------


@router.get("/now-layout", response_class=HTMLResponse)
async def now_layout_get(request: Request) -> HTMLResponse:
    """Render the Now Page card layout editor."""
    _require_session(request)

    manifest_cards = _read_card_manifest()
    current_layout = _read_now_layout()

    # Build active card list (in layout order) with manifest metadata
    active_types = [c["type"] for c in current_layout.get("cards", [])]
    manifest_by_type = {c["type"]: c for c in manifest_cards}

    active_cards = []
    for entry in current_layout.get("cards", []):
        meta = manifest_by_type.get(entry["type"])
        if meta:
            allowed = meta.get("allowedLayouts", [])
            default_footprint = allowed[0]["footprint"] if allowed else "tile"
            default_row_span = allowed[0]["rowSpan"] if allowed else 1
            active_cards.append({
                **meta,
                "currentFootprint": entry.get("footprint", default_footprint),
                "currentRowSpan": entry.get("rowSpan", default_row_span),
            })

    # Palette = manifest cards NOT in active layout
    palette_cards = [c for c in manifest_cards if c["type"] not in active_types]

    return _render(request, "card_layout.html", {
        "active_cards": active_cards,
        "palette_cards": palette_cards,
    })


@router.post("/now-layout", response_class=HTMLResponse)
async def now_layout_post(request: Request) -> HTMLResponse:
    """Save the card layout to now-layout.json in the config directory."""
    _require_session(request)
    assert _config_dir is not None

    form = await request.form()
    layout_json_str = str(form.get("layout_json", "{}"))

    try:
        layout_data = json.loads(layout_json_str)
    except json.JSONDecodeError:
        return _render_result(
            request,
            section_slug="now-layout",
            display_name=_("Now Page Layout"),
            success=False,
            error=_("Invalid layout data"),
            status_code=422,
        )

    # Validate card types and layouts against manifest
    manifest_cards = _read_card_manifest()
    valid_types = {c["type"] for c in manifest_cards}
    _VALID_FOOTPRINTS = {"tile", "wide", "panel", "full"}
    _VALID_ROWSPANS = {1, 2, 2.5}

    cards = []
    for entry in layout_data.get("cards", []):
        card_type = entry.get("type", "")
        if card_type not in valid_types:
            logger.warning("now_layout_post: skipping unknown card type %r", card_type)
            continue
        footprint = entry.get("footprint", "tile")
        if footprint not in _VALID_FOOTPRINTS:
            footprint = "tile"
        row_span = entry.get("rowSpan", 1)
        if row_span not in _VALID_ROWSPANS:
            row_span = 1
        cards.append({
            "type": card_type,
            "footprint": footprint,
            "rowSpan": row_span,
        })

    config = {"version": 1, "cards": cards}
    layout_path = _config_dir / "now-layout.json"

    error: str | None = None
    success = False
    try:
        with open(layout_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        success = True
    except OSError as exc:
        error = _("File write error: {detail}").format(detail=exc)
        logger.error("now_layout_post OSError: %s", exc)
    except Exception as exc:  # noqa: BLE001
        error = _("Unexpected error saving card layout: {detail}").format(detail=exc)
        logger.exception("now_layout_post unexpected error")

    return _render_result(
        request,
        section_slug="now-layout",
        display_name=_("Now Page Layout"),
        success=success,
        error=error,
        status_code=500 if error else 200,
    )


# ---------------------------------------------------------------------------
# Phase 6 T6.2 — Marine locations admin section
#
# NOTE on data source: GET /setup/current-config's "marine" field was added
# to weewx-clearskies-api specifically to support this admin section
# (coordinator-owned change, 2026-07-10 — see CurrentConfigResponse.marine
# in weewx_clearskies_api/endpoints/setup.py). It returns the [marine]
# section of api.conf "as-is" (raw ConfigObj mirror), so values below are
# defensively type-coerced rather than trusted as already-typed JSON.
#
# NOTE on write path: POST /setup/apply is not a partial-patch endpoint —
# `database`, `station`, `column_mapping`, and `column_units` are always
# rewritten from whatever is sent (non-Optional/defaulted fields), while
# `providers`/`branding`/`social`/`earthquakes`/`units`/`openaq_api_key` are
# left untouched when omitted (Optional, None-means-skip). To edit one
# marine location without clobbering unrelated config, _build_marine_apply_
# payload() rebuilds the always-rewritten fields from the just-fetched
# current-config response and omits every skip-if-absent field.
# ---------------------------------------------------------------------------

# Mirrors the validation vocabulary from wizard/routes.py's marine step
# (T6.1). Duplicated (not imported) so admin/routes.py stays decoupled from
# the wizard router module's own FastAPI router/global state.
_MARINE_VALID_ACTIVITIES: frozenset[str] = frozenset({"marine", "surf", "fishing", "beach_safety"})
_MARINE_VALID_BOTTOM_TYPES: frozenset[str] = frozenset({"sand", "rock", "coral_reef", "mixed"})
_MARINE_VALID_TOPO_FEATURES: frozenset[str] = frozenset(
    {"point_break", "bay_break", "headland", "straight_beach"}
)
_MARINE_VALID_EXPOSURE: frozenset[str] = frozenset({"N", "NE", "E", "SE", "S", "SW", "W", "NW"})
_MARINE_VALID_TARGET_CATEGORIES: frozenset[str] = frozenset(
    {"saltwater_inshore", "bottom_fish", "freshwater_sport", "salmonids"}
)
# Mirrors the API's _MARINE_LOCATION_ID_PATTERN (lowercase slug, 1-64 chars).
_MARINE_LOCATION_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")

_MARINE_ACTIVITY_LABELS: dict[str, str] = {
    "marine": "Marine / Boating",
    "surf": "Surf",
    "fishing": "Fishing",
    "beach_safety": "Beach Safety",
}


def _marine_to_bool(value: Any, *, default: bool = False) -> bool:
    """Best-effort bool coercion for ConfigObj-sourced values (T5.3).

    ConfigObj stores booleans as strings (``"True"``/``"False"``).
    Also handles actual ``bool``, ``int``, and ``None``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return default


def _marine_to_float(value: Any) -> float | None:
    """Best-effort float coercion for ConfigObj-sourced scalar values."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _marine_to_str_list(value: Any) -> list[str]:
    """Normalise a ConfigObj-sourced value to a ``list[str]``.

    ConfigObj returns a single string for one value, or a Python list for
    comma-separated values — GET /setup/current-config passes the [marine]
    section through as-is, so both forms occur depending on how many values
    a given key holds.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _marine_exposure_list(value: Any) -> list[str]:
    """Normalise ``directional_exposure`` to a validated ``list[str]``.

    Tolerates three on-disk formats:
    - ``dict[str, bool]`` (the API apply schema's shape, e.g. ``{"N": True}``)
    - ConfigObj colon-format list (e.g. ``["N:true", "SE:true"]``) — written
      by ``_build_marine_conf_section`` in the API's setup.py
    - Bare direction list (e.g. ``["N", "SE"]``)
    """
    if isinstance(value, dict):
        directions = [
            k for k, v in value.items() if v is True or str(v).lower() == "true"
        ]
    else:
        raw = _marine_to_str_list(value)
        directions = []
        for entry in raw:
            if ":" in entry:
                dir_part, bool_part = entry.split(":", 1)
                if bool_part.strip().lower() == "true":
                    directions.append(dir_part.strip())
            else:
                directions.append(entry.strip())
    return [d for d in directions if d in _MARINE_VALID_EXPOSURE]


def _marine_structures_list(value: Any) -> list[dict[str, Any]]:
    """Normalise ``structures`` from ConfigObj dict-of-dicts to a list."""
    if not isinstance(value, dict):
        return list(value) if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for struct in value.values():
        if isinstance(struct, dict):
            entry: dict[str, Any] = {
                "type": str(struct.get("type", "")),
                "material": str(struct.get("material", "")),
                "length_m": _marine_to_float(struct.get("length_m")),
                "bearing_degrees": _marine_to_float(struct.get("bearing_degrees")),
                "distance_m": _marine_to_float(struct.get("distance_m")),
            }
            # Way/polyline geometry from the wizard's discovery or map-draw
            # tool (E13), already [lon, lat]. Absent geometry stays absent —
            # never fabricated here.
            coords = struct.get("coordinates")
            if isinstance(coords, list) and coords:
                entry["coordinates"] = coords
            result.append(entry)
    return result


def _fetch_current_config() -> dict[str, Any] | None:
    """Fetch the full current configuration from the API.

    Returns None if the API is unreachable or not configured. Shared by the
    marine admin routes both for reading configured locations and for
    rebuilding a safe /setup/apply payload (see module note above).
    """
    client = _get_api_client()
    if client is None:
        return None
    try:
        return client.get_current_config()
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch current config from API for marine admin", exc_info=True)
        return None


def _parse_marine_locations(marine_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalise the raw ``[marine][[locations]]`` ConfigObj dict for template use.

    Returns a dict keyed by location id; each value has keys: id, name, lat,
    lon, activities, ndbc_station_ids, coops_station_ids, nws_marine_zone_id,
    surf, fishing, beach_safety (the latter three are ``{}`` when
    the corresponding activity is not selected for that location).
    """
    raw_locations = marine_cfg.get("locations") if isinstance(marine_cfg, dict) else None
    if not isinstance(raw_locations, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for loc_id, raw in raw_locations.items():
        if not isinstance(raw, dict):
            continue
        activities = [
            a for a in _marine_to_str_list(raw.get("activities")) if a in _MARINE_VALID_ACTIVITIES
        ]
        surf_raw = raw.get("surf") if isinstance(raw.get("surf"), dict) else {}
        fishing_raw = raw.get("fishing") if isinstance(raw.get("fishing"), dict) else {}
        beach_raw = raw.get("beach_safety") if isinstance(raw.get("beach_safety"), dict) else {}

        external_links: list[dict[str, str]] = []
        links_raw = beach_raw.get("external_links") if isinstance(beach_raw, dict) else None
        if isinstance(links_raw, dict):
            # ConfigObj nested-subsection form: {"0": {"label": .., "url": ..}, ...}
            for link in links_raw.values():
                if isinstance(link, dict) and link.get("url"):
                    external_links.append(
                        {"label": str(link.get("label", "")), "url": str(link["url"])}
                    )
        elif isinstance(links_raw, list):
            for link in links_raw:
                if isinstance(link, dict) and link.get("url"):
                    external_links.append(
                        {"label": str(link.get("label", "")), "url": str(link["url"])}
                    )

        result[str(loc_id)] = {
            "id": str(loc_id),
            "name": str(raw.get("name", "")) or str(loc_id),
            "lat": _marine_to_float(raw.get("lat")),
            "lon": _marine_to_float(raw.get("lon")),
            "activities": activities,
            "ndbc_station_ids": _marine_to_str_list(raw.get("ndbc_station_ids")),
            "coops_station_ids": _marine_to_str_list(raw.get("coops_station_ids")),
            "nws_marine_zone_id": str(raw.get("nws_marine_zone_id", "") or ""),
            "surf": {
                # Segment fields (T2.5 — replaces beach_facing_degrees, which is now
                # computed by the API from the segment bearing via _perpendicular_bearing).
                "segment_start_lat": _marine_to_float(surf_raw.get("segment_start_lat")),
                "segment_start_lon": _marine_to_float(surf_raw.get("segment_start_lon")),
                "segment_end_lat": _marine_to_float(surf_raw.get("segment_end_lat")),
                "segment_end_lon": _marine_to_float(surf_raw.get("segment_end_lon")),
                "transect_spacing_m": _marine_to_float(surf_raw.get("transect_spacing_m")) or 10.0,
                "l3_enabled": str(surf_raw.get("l3_enabled", "auto") or "auto"),
                "bottom_type": str(surf_raw.get("bottom_type", "") or ""),
                "topographic_feature": str(surf_raw.get("topographic_feature", "") or ""),
                "directional_exposure": _marine_exposure_list(surf_raw.get("directional_exposure")),
                "structures": _marine_structures_list(surf_raw.get("structures")),
                # SurfBeat and friction fields (T5.3).
                "surfbeat_enabled": _marine_to_bool(surf_raw.get("surfbeat_enabled"), default=True),
                "surfbeat_cadence_hours": int(_marine_to_float(surf_raw.get("surfbeat_cadence_hours")) or 3),
                "friction_coefficient": _marine_to_float(surf_raw.get("friction_coefficient")) or 0.038,
            } if "surf" in activities else {},
            "fishing": {
                "target_categories": fishing_raw.get("target_categories") or ([fishing_raw["target_category"]] if fishing_raw.get("target_category") else []),
                "species": _marine_to_str_list(fishing_raw.get("species")),
            } if "fishing" in activities else {},
            "beach_safety": {
                "external_links": external_links,
            } if "beach_safety" in activities else {},
            # C9b: display-only -- whether the on-disk config carries the
            # directional_exposure key at all (any shape), i.e. whether this
            # location has an explicit manual override in effect vs the
            # fan-derived Auto default. Mirrors the marine service's own
            # directional_exposure_is_override (marine_config.py) for
            # vocabulary parity. Deliberately kept OUTSIDE the "surf" dict --
            # both _build_marine_apply_payload() and the trushore_save
            # rebuild path (`dict(loc["surf"])`) would otherwise forward an
            # unrecognised key into the /setup/apply "surf" payload, which
            # the API's extra="forbid" schema would reject. Never sent to
            # the API; both apply-payload builders key off the normalized
            # "directional_exposure" list above being non-empty, unchanged.
            "directional_exposure_is_override": (
                surf_raw.get("directional_exposure") is not None
                if "surf" in activities else False
            ),
        }
    return result


def _slugify_marine_location_name(name: str, existing_ids: set[str]) -> str:
    """Derive a unique, API-valid location id slug from an operator-entered name.

    Mirrors the API's ``_MARINE_LOCATION_ID_PATTERN`` (lowercase slug, 1-64
    chars, letters/digits/hyphen/underscore) and disambiguates against
    *existing_ids* by appending ``-2``, ``-3``, etc.
    """
    base = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")[:60]
    if not base or not _MARINE_LOCATION_ID_RE.match(base):
        base = "location"
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def _validate_marine_location_form(form: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and validate one marine location edit-form submission.

    Returns ``(location_dict, None)`` on success, or ``(partial_dict_or_None,
    error_message)`` on failure. Mirrors the required-field rules the API's
    MarineLocationApplyConfig / MarineSurfSpotApplyConfig /
    MarineFishingSpotApplyConfig enforce, so the admin UI fails fast with a
    friendly message rather than surfacing a raw 422 from /setup/apply.
    """
    name = str(form.get("name", "")).strip()
    if not name:
        return None, _("Location name is required.")

    lat = _marine_to_float(form.get("lat"))
    lon = _marine_to_float(form.get("lon"))
    if lat is None or not (-90 <= lat <= 90):
        return None, _("Latitude must be a number between -90 and 90.")
    if lon is None or not (-180 <= lon <= 180):
        return None, _("Longitude must be a number between -180 and 180.")

    activities = [a for a in form.getlist("activities") if a in _MARINE_VALID_ACTIVITIES]
    if not activities:
        return None, _("Select at least one activity.")

    location: dict[str, Any] = {
        "name": name,
        "lat": lat,
        "lon": lon,
        "activities": activities,
        "ndbc_station_ids": [
            s.strip() for s in str(form.get("ndbc_station_ids", "")).split(",") if s.strip()
        ],
        "coops_station_ids": [
            s.strip() for s in str(form.get("coops_station_ids", "")).split(",") if s.strip()
        ],
        "nws_marine_zone_id": str(form.get("nws_marine_zone_id", "")).strip(),
        "surf": {},
        "fishing": {},
        "beach_safety": {},
        # Local-only — never sent to the API (see _read_marine_photos_sidecar).
        # photo_url carries forward via the template's hidden field; a fresh
        # upload in marine_save's Phase 3 overrides it.
        "photo_url": str(form.get("photo_url", "")).strip(),
        "photo_attribution": str(form.get("photo_attribution", "")).strip(),
    }

    if "surf" in activities:
        # Segment fields (T2.5 — replaces beach_facing_degrees).
        seg_start_lat = _marine_to_float(form.get("surf_segment_start_lat"))
        seg_start_lon = _marine_to_float(form.get("surf_segment_start_lon"))
        seg_end_lat = _marine_to_float(form.get("surf_segment_end_lat"))
        seg_end_lon = _marine_to_float(form.get("surf_segment_end_lon"))
        if seg_start_lat is None or not (-90 <= seg_start_lat <= 90):
            return location, _("Surf: a shoreline segment is required — draw it on the map above.")
        if seg_start_lon is None or not (-180 <= seg_start_lon <= 180):
            return location, _("Surf: a shoreline segment is required — draw it on the map above.")
        if seg_end_lat is None or not (-90 <= seg_end_lat <= 90):
            return location, _("Surf: a shoreline segment is required — draw it on the map above.")
        if seg_end_lon is None or not (-180 <= seg_end_lon <= 180):
            return location, _("Surf: a shoreline segment is required — draw it on the map above.")
        raw_spacing = _marine_to_float(form.get("transect_spacing_m"))
        transect_spacing: float = raw_spacing if (raw_spacing is not None and 1 <= raw_spacing <= 500) else 10.0
        bottom_type = str(form.get("surf_bottom_type", "")).strip()
        topo = str(form.get("surf_topographic_feature", "")).strip()
        if bottom_type not in _MARINE_VALID_BOTTOM_TYPES:
            return location, _("Surf: a valid bottom type is required.")
        if topo not in _MARINE_VALID_TOPO_FEATURES:
            return location, _("Surf: a valid topographic feature is required.")
        structures: list[dict] = []
        si = 0
        while True:
            s_type = str(form.get(f"structure_{si}_type", "")).strip()
            if not s_type:
                break
            s_material = str(form.get(f"structure_{si}_material", "")).strip()
            s_length = _marine_to_float(form.get(f"structure_{si}_length_m"))
            s_bearing = _marine_to_float(form.get(f"structure_{si}_bearing_degrees"))
            s_distance = _marine_to_float(form.get(f"structure_{si}_distance_m"))
            if s_type and s_material and s_length and s_bearing is not None and s_distance:
                structure: dict[str, Any] = {
                    "type": s_type, "material": s_material,
                    "length_m": s_length, "bearing_degrees": s_bearing, "distance_m": s_distance,
                }
                # Way/polyline geometry round-tripped from the hidden
                # structure_{n}_coordinates field (E13). Absent geometry
                # stays absent — never fabricated here.
                s_coords_raw = str(form.get(f"structure_{si}_coordinates", "")).strip()
                if s_coords_raw:
                    try:
                        s_coords = json.loads(s_coords_raw)
                    except (ValueError, TypeError):
                        s_coords = None
                    if isinstance(s_coords, list) and s_coords:
                        structure["coordinates"] = s_coords
                structures.append(structure)
            si += 1

        l3_enabled = str(form.get("surf_l3_enabled", "auto")).strip().lower()
        if l3_enabled not in {"auto", "on", "off"}:
            l3_enabled = "auto"

        surf_cfg: dict = {
            "segment_start_lat": seg_start_lat,
            "segment_start_lon": seg_start_lon,
            "segment_end_lat": seg_end_lat,
            "segment_end_lon": seg_end_lon,
            "transect_spacing_m": transect_spacing,
            "l3_enabled": l3_enabled,
            "bottom_type": bottom_type,
            "topographic_feature": topo,
            "directional_exposure": [
                d for d in form.getlist("surf_exposure") if d in _MARINE_VALID_EXPOSURE
            ],
        }
        if structures:
            surf_cfg["structures"] = structures

        # SurfBeat and friction fields (T5.3).
        surfbeat_checked = form.get("surfbeat_enabled")
        surf_cfg["surfbeat_enabled"] = surfbeat_checked is not None
        raw_cadence = _marine_to_float(form.get("surfbeat_cadence_hours"))
        cadence_int = int(raw_cadence) if raw_cadence is not None and 1 <= raw_cadence <= 6 else 3
        surf_cfg["surfbeat_cadence_hours"] = cadence_int
        raw_friction = _marine_to_float(form.get("friction_coefficient"))
        if raw_friction is not None and raw_friction > 0:
            surf_cfg["friction_coefficient"] = raw_friction
        else:
            surf_cfg["friction_coefficient"] = 0.038

        location["surf"] = surf_cfg

    if "fishing" in activities:
        target_categories = [
            c.strip() for c in form.getlist("fishing_target_categories")
            if c.strip() in _MARINE_VALID_TARGET_CATEGORIES
        ]
        if not target_categories:
            old_single = str(form.get("fishing_target_category", "")).strip()
            if old_single in _MARINE_VALID_TARGET_CATEGORIES:
                target_categories = [old_single]
        if not target_categories:
            return location, _("Fishing: at least one target category is required.")
        species = [s.strip() for s in form.getlist("fishing_species") if s.strip()]
        location["fishing"] = {"target_categories": target_categories, "species": species}

    if "beach_safety" in activities:
        labels = form.getlist("beach_safety_link_label")
        urls = form.getlist("beach_safety_link_url")
        location["beach_safety"] = {
            "external_links": [
                {"label": label.strip(), "url": url.strip()}
                for label, url in zip(labels, urls)
                if url.strip()
            ]
        }

    return location, None


def _build_base_apply_payload(config: dict[str, Any]) -> dict[str, Any]:
    """Build the always-rewritten half of a safe /setup/apply payload.

    See the module-level "NOTE on write path" comment above: ``database``,
    ``station``, ``column_mapping`` and ``column_units`` are non-Optional on
    the API's ``ApplyRequest`` and are rewritten from whatever arrives, so any
    admin section doing a partial edit must re-send them faithfully from a
    just-fetched current-config.  Every skip-if-absent section is omitted.
    Callers add only the keys their own section owns.
    """
    database = config.get("database") or {}
    station = config.get("station") or {}

    return {
        "database": {
            "kind": database.get("kind", "mysql"),
            "host": database.get("host", ""),
            "port": database.get("port", 3306),
            "user": database.get("user", ""),
            "password": database.get("password", ""),
            "name": database.get("name", ""),
            "path": database.get("path", ""),
        },
        "station": {
            "name": station.get("name"),
            "latitude": station.get("latitude"),
            "longitude": station.get("longitude"),
            "altitude_meters": station.get("altitude_meters"),
            "timezone": station.get("timezone"),
            "default_locale": station.get("default_locale"),
        },
        "column_mapping": config.get("column_mapping") or {},
        "column_units": config.get("column_units") or {},
    }


def _build_marine_apply_payload(
    config: dict[str, Any], locations: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build a minimal, safe /setup/apply payload that updates only marine locations."""
    payload: dict[str, Any] = _build_base_apply_payload(config)

    location_list: list[dict[str, Any]] = []
    for loc_id, loc in locations.items():
        activities = loc.get("activities", [])
        entry: dict[str, Any] = {
            "id": loc_id,
            "name": loc["name"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "activities": activities,
        }
        if loc.get("ndbc_station_ids"):
            entry["ndbc_station_ids"] = loc["ndbc_station_ids"]
        if loc.get("coops_station_ids"):
            entry["coops_station_ids"] = loc["coops_station_ids"]
        if loc.get("nws_marine_zone_id"):
            entry["nws_marine_zone_id"] = loc["nws_marine_zone_id"]
        if "surf" in activities and loc.get("surf"):
            s = loc["surf"]
            # Segment fields sent instead of beach_facing_degrees (T2.5).
            # The API's /setup/apply handler computes beach_facing_degrees from
            # the segment bearing via _perpendicular_bearing(_segment_bearing(...)).
            entry["surf"] = {
                "segment_start_lat": s["segment_start_lat"],
                "segment_start_lon": s["segment_start_lon"],
                "segment_end_lat": s["segment_end_lat"],
                "segment_end_lon": s["segment_end_lon"],
                "bottom_type": s["bottom_type"],
                "topographic_feature": s["topographic_feature"],
                "l3_enabled": s.get("l3_enabled", "auto"),
            }
            if s.get("transect_spacing_m") and s["transect_spacing_m"] != 10.0:
                entry["surf"]["transect_spacing_m"] = s["transect_spacing_m"]
            if s.get("directional_exposure"):
                entry["surf"]["directional_exposure"] = {d: True for d in s["directional_exposure"]}
            if s.get("structures"):
                entry["surf"]["structures"] = s["structures"]
            # SurfBeat and friction fields (T5.3).
            if "surfbeat_enabled" in s:
                entry["surf"]["surfbeat_enabled"] = s["surfbeat_enabled"]
            if "surfbeat_cadence_hours" in s:
                entry["surf"]["surfbeat_cadence_hours"] = s["surfbeat_cadence_hours"]
            if "friction_coefficient" in s:
                entry["surf"]["friction_coefficient"] = s["friction_coefficient"]
        if "fishing" in activities and loc.get("fishing"):
            cats = loc["fishing"].get("target_categories") or ([loc["fishing"]["target_category"]] if loc["fishing"].get("target_category") else [])
            if cats:
                fishing_payload: dict[str, Any] = {"target_categories": cats}
                species = loc["fishing"].get("species")
                if species:
                    fishing_payload["species"] = species
                entry["fishing"] = fishing_payload
        if "beach_safety" in activities:
            links = loc.get("beach_safety", {}).get("external_links") or []
            if links:
                entry["beach_safety"] = {
                    "external_links": [
                        {"label": link.get("label", ""), "url": link["url"]}
                        for link in links if link.get("url")
                    ]
                }
        location_list.append(entry)

    payload["marine"] = {"locations": location_list}
    return payload


def _marine_esc(v: object) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _marine_photos_sidecar_path(config_dir: Path) -> Path:
    return config_dir / "marine-photos.json"


def _read_marine_photos_sidecar(config_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Read marine-photos.json: local-only photo_url/photo_attribution per location id.

    This data is never sent to the API — the API's marine location contract
    always returns ``photoUrl: null`` and the dashboard constructs photo URLs
    directly from the location id (see API-MANUAL "THE API IS NOT A FILE
    SERVER"; photos are served by Caddy from
    /etc/weewx-clearskies/marine-photos/, not by the API). The admin
    ``locations`` dict is rebuilt from the API's config on every request
    (see marine_get / marine_edit_form / marine_save), so this sidecar is the
    only durable home for photo metadata. Mirrors the branding.json /
    webcam.json local-file pattern. Kept as a local copy of the identical
    wizard/routes.py helper rather than a shared import so each router module
    has no import-time dependency on the other (see the `_()` helper above
    for the same rationale).

    Returns ``{"locations": {}}`` if the file is missing or unreadable.
    """
    path = _marine_photos_sidecar_path(config_dir)
    if not path.exists():
        return {"locations": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read marine-photos.json: %s", exc)
        return {"locations": {}}
    if not isinstance(data, dict) or not isinstance(data.get("locations"), dict):
        return {"locations": {}}
    return data


def _write_marine_photos_sidecar(config_dir: Path, locations: dict[str, dict[str, str]]) -> None:
    """Write marine-photos.json. Atomic write, local-only — never applied to the API."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = _marine_photos_sidecar_path(config_dir)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps({"locations": locations}, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write marine-photos.json: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _apply_marine_photo_sidecar(
    locations: dict[str, dict[str, Any]], config_dir: Path | None
) -> None:
    """Overlay locally-persisted photo_url/photo_attribution onto a locations dict.

    ``locations`` is normally freshly parsed from the API's config response
    each request and never carries photo fields on its own.
    """
    if config_dir is None or not locations:
        return
    sidecar_locations = _read_marine_photos_sidecar(config_dir).get("locations", {})
    for loc_id, loc in locations.items():
        meta = sidecar_locations.get(loc_id)
        if not isinstance(meta, dict):
            continue
        if meta.get("photo_url") and not loc.get("photo_url"):
            loc["photo_url"] = meta["photo_url"]
        if meta.get("photo_attribution") and not loc.get("photo_attribution"):
            loc["photo_attribution"] = meta["photo_attribution"]


def _restart_api_after_apply(client: Any) -> None:
    """Trigger POST /setup/restart so the API reloads api.conf.

    /setup/apply only writes files to disk — it does not itself reload the
    running API process. The wizard's re-run apply flow always follows apply()
    with a restart() call (see wizard/routes.py wizard_apply, "Step 3: Trigger
    service restarts"); this mirrors that for the admin marine save/delete
    routes. Best-effort: a failed restart request is logged but does not turn
    an already-successful apply() into a reported error, since the config
    change is safely on disk either way — see ApiClient.restart()'s docstring
    (a dropped connection while the API exits is expected, not a failure).
    """
    try:
        client.restart()
    except Exception:  # noqa: BLE001
        logger.warning("_restart_api_after_apply: restart request failed", exc_info=True)


@router.get("/marine", response_class=HTMLResponse)
async def marine_get(request: Request) -> HTMLResponse:
    """Render the marine locations admin section — location list.

    Non-HTMX requests (direct URL navigation, page refresh) redirect to the
    admin landing page — the marine template is a fragment designed to load
    inside the landing page's ``#admin-content`` div.
    """
    _require_session(request)
    if not request.headers.get("HX-Request"):
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/admin/config#marine", status_code=303)
    config = _fetch_current_config()
    error: str | None = None
    locations: dict[str, dict[str, Any]] = {}
    if config is None:
        error = _("Cannot connect to the API — check that the API is running and configured.")
    else:
        locations = _parse_marine_locations(config.get("marine") or {})
        _apply_marine_photo_sidecar(locations, _config_dir)
    return _render(request, "marine.html", {
        "locations": locations,
        "activity_labels": _MARINE_ACTIVITY_LABELS,
        "error": error,
        "flash": None,
    })


@router.post("/marine/edit", response_class=HTMLResponse)
async def marine_edit_form(request: Request) -> HTMLResponse:
    """Render the add/edit form for one marine location (HTMX fragment).

    ``location_id`` empty means "add a new location"; otherwise the form is
    pre-populated from the matching entry in the current-config response.
    """
    _require_session(request)
    form = await request.form()
    location_id = str(form.get("location_id", "")).strip()

    config = _fetch_current_config()
    locations: dict[str, dict[str, Any]] = {}
    edit_location: dict[str, Any] = {}
    error: str | None = None
    if config is None:
        error = _("Cannot connect to the API — check that the API is running and configured.")
    else:
        locations = _parse_marine_locations(config.get("marine") or {})
        _apply_marine_photo_sidecar(locations, _config_dir)
        if location_id:
            edit_location = locations.get(location_id, {})

    return _render(request, "marine.html", {
        "locations": locations,
        "activity_labels": _MARINE_ACTIVITY_LABELS,
        "edit_mode": True,
        "edit_location_id": location_id,
        "edit_location": edit_location,
        "edit_error": None,
        "error": error,
        "flash": None,
    })


@router.post("/marine/save", response_class=HTMLResponse)
async def marine_save(request: Request) -> HTMLResponse:
    """Validate and save one marine location (add or edit), then re-render the list.

    Photo upload is processed first — it is a local file operation on the
    front-end host and must not be gated on the API being reachable.  The API
    apply call happens afterward; if it fails the photo is still saved.

    Persists by rebuilding a full-but-minimal /setup/apply payload (see
    _build_marine_apply_payload) so unrelated config sections (providers,
    branding, database credentials, etc.) are left untouched.
    """
    _require_session(request)
    form = await request.form()
    location_id = str(form.get("location_id", "")).strip()

    # ---- Phase 1: validate form (no API needed) ----
    parsed, validation_error = _validate_marine_location_form(form)
    if validation_error:
        return _render(request, "marine.html", {
            "locations": {},
            "activity_labels": _MARINE_ACTIVITY_LABELS,
            "edit_mode": True,
            "edit_location_id": location_id,
            "edit_location": parsed or {},
            "edit_error": validation_error,
            "error": None,
            "flash": None,
        }, status_code=422)

    assert parsed is not None

    # ---- Phase 2: determine loc_id (no API needed for edits) ----
    if location_id:
        loc_id = location_id
    else:
        loc_id = _slugify_marine_location_name(parsed["name"], set())

    # ---- Phase 3: save photo to disk (no API needed) ----
    photo_upload = form.get("photo")
    if photo_upload and hasattr(photo_upload, "filename") and photo_upload.filename:
        suffix = Path(str(photo_upload.filename)).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            photo_data: bytes = await photo_upload.read()
            if len(photo_data) <= 200 * 1024:
                photos_dir = Path("/etc/weewx-clearskies/marine-photos")
                photos_dir.mkdir(parents=True, exist_ok=True)
                for old in photos_dir.glob(f"{loc_id}.*"):
                    old.unlink(missing_ok=True)
                (photos_dir / f"{loc_id}{suffix}").write_bytes(photo_data)
                logger.info("Saved marine photo for %s (%d bytes)", loc_id, len(photo_data))
                parsed["photo_url"] = f"/marine-photos/{loc_id}{suffix}"
            else:
                return _render(request, "marine.html", {
                    "locations": {},
                    "activity_labels": _MARINE_ACTIVITY_LABELS,
                    "edit_mode": True,
                    "edit_location_id": loc_id,
                    "edit_location": parsed,
                    "edit_error": _("Photo exceeds 200 KB limit ({size} KB).").format(size=len(photo_data) // 1024),
                    "error": None,
                    "flash": None,
                }, status_code=422)

    # Photo metadata is local-only — never sent to the API (see
    # _read_marine_photos_sidecar). Persisted here, decoupled from Phase 4,
    # so it is not gated on API reachability — same rationale as the photo
    # file itself (see docstring above). Keyed by the same loc_id the photo
    # file was just saved under.
    if _config_dir is not None:
        sidecar_locations = _read_marine_photos_sidecar(_config_dir).get("locations", {})
        photo_entry = {
            k: v for k, v in (
                ("photo_url", parsed.get("photo_url", "")),
                ("photo_attribution", parsed.get("photo_attribution", "")),
            ) if v
        }
        if photo_entry:
            sidecar_locations[loc_id] = photo_entry
        else:
            sidecar_locations.pop(loc_id, None)
        _write_marine_photos_sidecar(_config_dir, sidecar_locations)

    # ---- Phase 4: fetch config and apply to API ----
    config = _fetch_current_config()
    if config is None:
        return _render(request, "marine.html", {
            "locations": {},
            "activity_labels": _MARINE_ACTIVITY_LABELS,
            "error": _("Photo saved. Cannot connect to the API to update location config — check that the API is running."),
            "flash": None,
        }, status_code=500)

    locations = _parse_marine_locations(config.get("marine") or {})
    _apply_marine_photo_sidecar(locations, _config_dir)

    # C-64, second direction: the operator can reach the same broken state
    # from either screen, so both are guarded.  Adding a location with no
    # marine service configured produces a location nothing will ever serve,
    # and until now nothing said so.  Only *new* locations are blocked: an
    # install that already has locations and no URL (hand-edited api.conf,
    # or a config predating this guard) must stay editable, and the error
    # below tells the operator where to fix the real problem either way.
    if not location_id and not str(config.get("marine_service_url") or "").strip():
        return _render(request, "marine.html", {
            "locations": locations,
            "activity_labels": _MARINE_ACTIVITY_LABELS,
            "edit_mode": True,
            "edit_location_id": "",
            "edit_location": parsed,
            "edit_error": _(
                "A marine service connection must be configured before adding marine "
                "locations — without one there is nothing to provide their data. Set the "
                "marine service URL in the Marine Service section first."
            ),
            "error": None,
            "flash": None,
        }, status_code=422)

    if location_id:
        if location_id not in locations:
            return _render(request, "marine.html", {
                "locations": locations,
                "activity_labels": _MARINE_ACTIVITY_LABELS,
                "error": _("Location not found. It may have been deleted in another session."),
                "flash": None,
            }, status_code=404)
    else:
        loc_id = _slugify_marine_location_name(parsed["name"], set(locations.keys()))

    locations[loc_id] = parsed

    client = _get_api_client()
    error = None
    flash = None
    if client is None:
        error = _("Photo saved. Cannot connect to the API to update location config.")
    else:
        try:
            client.apply(_build_marine_apply_payload(config, locations))
            _restart_api_after_apply(client)
            flash = _("Location saved. The API is restarting to apply the change.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("marine_save: apply failed", exc_info=True)
            error = _("Save failed: {detail}").format(detail=exc)

    return _render(request, "marine.html", {
        "locations": locations,
        "activity_labels": _MARINE_ACTIVITY_LABELS,
        "error": error,
        "flash": flash,
    }, status_code=500 if error else 200)


@router.post("/marine/delete", response_class=HTMLResponse)
async def marine_delete(request: Request) -> HTMLResponse:
    """Delete a marine location and persist via /setup/apply."""
    _require_session(request)
    form = await request.form()
    location_id = str(form.get("location_id", "")).strip()

    config = _fetch_current_config()
    if config is None:
        return _render(request, "marine.html", {
            "locations": {},
            "activity_labels": _MARINE_ACTIVITY_LABELS,
            "error": _("Cannot connect to the API — check that the API is running and configured."),
            "flash": None,
        }, status_code=500)

    locations = _parse_marine_locations(config.get("marine") or {})
    error: str | None = None
    flash: str | None = None

    if location_id not in locations:
        error = _("Location not found. It may have already been deleted.")
    else:
        locations.pop(location_id, None)
        client = _get_api_client()
        if client is None:
            error = _("Cannot connect to the API — check that the API is running and configured.")
        else:
            try:
                client.apply(_build_marine_apply_payload(config, locations))
                _restart_api_after_apply(client)
                flash = _("Location deleted. The API is restarting to apply the change.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("marine_delete: apply failed", exc_info=True)
                error = _("Delete failed: {detail}").format(detail=exc)

    return _render(request, "marine.html", {
        "locations": locations,
        "activity_labels": _MARINE_ACTIVITY_LABELS,
        "error": error,
        "flash": flash,
    }, status_code=500 if error else 200)


@router.post("/marine/test-connectivity", response_class=HTMLResponse)
async def marine_test_connectivity(request: Request) -> HTMLResponse:
    """Test connectivity to a marine location's configured data sources.

    Checks NDBC/CO-OPS station reachability via a fresh call to the same
    discovery lookup the wizard uses (GET /setup/marine/discover-stations
    from the location's stored coordinates). There is no dedicated
    "re-verify this exact station" endpoint on the API today, so this is a
    best-effort proxy: stations found nearby is treated as reachable; none
    found (or an API error) is treated as unreachable. The NWS marine zone
    indicator reflects whether a zone id is stored for the location — there
    is currently no endpoint to verify live zone-forecast availability, so
    this checks configuration presence rather than live reachability.
    Returns a small hand-built HTML fragment (green/amber status dots),
    swapped into the triggering row only — not a full section reload.
    """
    _require_session(request)
    form = await request.form()
    location_id = str(form.get("location_id", "")).strip()

    config = _fetch_current_config()
    if config is None:
        return HTMLResponse(
            f'<span style="color:var(--pico-del-color);font-size:0.8rem">{_marine_esc(_("API unreachable"))}</span>'
        )
    location = _parse_marine_locations(config.get("marine") or {}).get(location_id)
    if location is None or location.get("lat") is None or location.get("lon") is None:
        return HTMLResponse(
            f'<span style="color:var(--pico-del-color);font-size:0.8rem">{_marine_esc(_("Location not found"))}</span>'
        )

    client = _get_api_client()
    ndbc_ok = coops_ok = False
    if client is not None:
        try:
            result = client.discover_marine_stations(location["lat"], location["lon"], radius_miles=50)
            ndbc_ok = bool(result.get("ndbc_stations"))
            coops_ok = bool(result.get("coops_stations"))
        except Exception:  # noqa: BLE001
            logger.warning("marine_test_connectivity: discovery call failed", exc_info=True)

    nws_ok = bool(location.get("nws_marine_zone_id"))

    def _dot(ok: bool, label: str) -> str:
        color = "var(--pico-color-green-500,#22c55e)" if ok else "var(--pico-color-amber-500,#f59e0b)"
        return (
            '<span style="display:inline-flex;align-items:center;gap:0.3rem;'
            f'margin-inline-end:0.6rem;font-size:0.8rem">'
            f'<span aria-hidden="true" style="display:inline-block;width:0.6rem;height:0.6rem;'
            f'border-radius:50%;background:{color}"></span>{_marine_esc(label)}</span>'
        )

    html = _dot(ndbc_ok, _("NDBC")) + _dot(coops_ok, _("CO-OPS")) + _dot(nws_ok, _("NWS zone"))
    return HTMLResponse(html)



@router.post("/marine/coverage", response_class=HTMLResponse)
async def marine_coverage_refresh(request: Request) -> HTMLResponse:
    """Refresh the data coverage panel for a marine location (T3.6)."""
    _require_session(request)
    form = await request.form()
    lat_str = str(form.get("lat", "")).strip()
    lon_str = str(form.get("lon", "")).strip()

    if not lat_str or not lon_str:
        return HTMLResponse(
            '<div style="font-size:0.85rem;color:var(--pico-muted-color)">'
            + _marine_esc(_("Enter coordinates to see data coverage."))
            + "</div>"
        )

    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        return HTMLResponse(
            '<div style="font-size:0.85rem;color:var(--pico-del-color)">'
            + _marine_esc(_("Invalid coordinates."))
            + "</div>"
        )

    client = _get_api_client()
    if client is None:
        return HTMLResponse(
            '<div style="font-size:0.85rem;color:var(--pico-del-color)">'
            + _marine_esc(_("API unreachable."))
            + "</div>"
        )

    try:
        cov = client.get_marine_coverage(lat, lon)
    except Exception as exc:
        logger.warning("marine_coverage_refresh: API error", exc_info=True)
        return HTMLResponse(
            '<div style="font-size:0.85rem;color:var(--pico-del-color)">'
            + _marine_esc(_("Coverage check failed: {detail}").format(detail=exc))
            + "</div>"
        )

    return HTMLResponse(_render_coverage_html(cov))


def _coverage_populate_script(cov: dict) -> str:
    """Build an inline <script> that writes coverage-discovered station IDs
    into the edit form's visible fields (T3.4).

    /admin/marine/coverage (unlike the wizard's dedicated discover-stations
    endpoint) only returns the single *nearest* station of each type, so this
    merges rather than overwrites: NDBC/CO-OPS IDs are appended to the
    comma-separated field only if not already present (preserves any
    manually-entered extra stations); the NWS zone field is filled only if
    currently empty (won't clobber an operator's existing choice). Mirrors
    the inline-script pattern in templates/wizard/marine_station_results.html
    that writes discovered IDs into the wizard's hidden fields, adapted here
    for admin's single page-unique field IDs (marine-ndbc/marine-coops/
    marine-zone) via getElementById instead of closest(".marine-location-card").
    HTMX evaluates <script> tags in swapped fragments, so this runs
    automatically once the coverage panel is swapped in.
    """
    ndbc = cov.get("nearest_ndbc_buoy") or {}
    coops = cov.get("nearest_coops_station") or {}
    ndbc_id = ndbc.get("station_id") or ""
    coops_id = coops.get("station_id") or ""
    zone_id = cov.get("nws_marine_zone") or ""
    if not (ndbc_id or coops_id or zone_id):
        return ""
    added_label = _("Added to location:")
    return (
        '<div id="marine-coverage-populate-note" role="status" aria-live="polite" '
        'style="font-size:0.8rem;margin-block-start:0.4rem;color:var(--pico-ins-color,#16a34a)"></div>'
        "<script>(function(){"
        "function addToField(id,value){"
        "if(!value)return null;"
        "var input=document.getElementById(id);"
        "if(!input)return null;"
        "var existing=input.value.split(',').map(function(s){return s.trim();}).filter(Boolean);"
        "if(existing.indexOf(value)!==-1)return null;"
        "existing.push(value);"
        "input.value=existing.join(', ');"
        "return value;"
        "}"
        "function setIfEmpty(id,value){"
        "if(!value)return null;"
        "var input=document.getElementById(id);"
        "if(!input||input.value.trim())return null;"
        "input.value=value;"
        "return value;"
        "}"
        "var messages=[];"
        f"var ndbcAdded=addToField('marine-ndbc',{json.dumps(ndbc_id)});"
        "if(ndbcAdded)messages.push('NDBC '+ndbcAdded);"
        f"var coopsAdded=addToField('marine-coops',{json.dumps(coops_id)});"
        "if(coopsAdded)messages.push('CO-OPS '+coopsAdded);"
        f"var zoneAdded=setIfEmpty('marine-zone',{json.dumps(zone_id)});"
        "if(zoneAdded)messages.push('NWS '+zoneAdded);"
        "var note=document.getElementById('marine-coverage-populate-note');"
        "if(note&&messages.length){"
        f"note.textContent={json.dumps(added_label)}+' '+messages.join(', ')+'.';"
        "}"
        "})();</script>"
    )


def _render_coverage_html(cov: dict) -> str:
    """Render the coverage panel as an HTML fragment for HTMX swap."""

    def _check(ok: bool, label: str, detail: str = "") -> str:
        icon = "&#x2705;" if ok else "&#x274C;"
        detail_span = (
            f' <span style="color:var(--pico-muted-color)">({_marine_esc(detail)})</span>'
            if detail else ""
        )
        return (
            f'<div style="display:flex;align-items:center;gap:0.4rem;padding:0.2rem 0">'
            f'<span aria-hidden="true">{icon}</span>'
            f'<span>{_marine_esc(label)}{detail_span}</span></div>'
        )

    ofs_model = cov.get("ofs_model")
    ofs_fallback = cov.get("ofs_fallback")
    tier = cov.get("coverage_tier", "unavailable")
    available = cov.get("available_data", [])
    ndbc = cov.get("nearest_ndbc_buoy")
    coops = cov.get("nearest_coops_station")
    nws_zone = cov.get("nws_marine_zone")
    on_prem = cov.get("on_premises_sensor", "not_configured")

    tier_labels = {
        "ofs": _("Full coverage (OFS coastal model)"),
        "regional_erddap": _("Regional coverage (ERDDAP)"),
        "rtofs": _("Global coverage (RTOFS)"),
        "mur_sst": _("Surface temperature only (MUR SST)"),
        "unavailable": _("No ocean data coverage"),
    }

    html_parts = []

    tier_label = tier_labels.get(tier, tier)
    tier_color = (
        "var(--pico-ins-color,#16a34a)"
        if tier in ("ofs", "regional_erddap")
        else "var(--pico-color-amber-500,#f59e0b)"
    )
    html_parts.append(
        f'<div style="font-weight:600;font-size:0.9rem;margin-bottom:0.5rem;color:{tier_color}">'
        f'{_marine_esc(str(tier_label))}</div>'
    )

    if ofs_model:
        res = cov.get("ofs_model_resolution_deg")
        res_str = f"~{res}°" if res else ""
        html_parts.append(_check(True, _("OFS model: {model}").format(model=ofs_model), res_str))
        if ofs_fallback:
            html_parts.append(_check(True, _("Fallback: {model}").format(model=ofs_fallback)))
    else:
        html_parts.append(_check(False, _("No OFS coastal model coverage")))

    cap_labels = {
        "surface_temp": _("Surface temperature"),
        "water_column": _("Water column profiles"),
        "currents": _("Ocean currents"),
        "salinity": _("Salinity"),
        "modeled_water_levels": _("Modeled water levels"),
        "forecast": _("Ocean forecast"),
    }
    for cap in ["surface_temp", "water_column", "currents", "salinity", "modeled_water_levels", "forecast"]:
        html_parts.append(_check(cap in available, str(cap_labels.get(cap, cap))))

    html_parts.append('<hr style="margin:0.5rem 0">')

    if ndbc:
        html_parts.append(_check(
            True,
            _("NDBC buoy: {id}").format(id=ndbc["station_id"]),
            _("{dist} mi").format(dist=ndbc["distance_miles"]),
        ))
    else:
        html_parts.append(_check(False, _("No NDBC buoy within range")))

    if coops:
        html_parts.append(_check(
            True,
            _("CO-OPS station: {id}").format(id=coops["station_id"]),
            _("{dist} mi").format(dist=coops["distance_miles"]),
        ))
    else:
        html_parts.append(_check(False, _("No CO-OPS station within range")))

    html_parts.append(_check(bool(nws_zone), _("NWS marine zone: {zone}").format(zone=nws_zone or "—")))

    prem_labels = {
        "within_threshold": _("Weather station nearby"),
        "too_far": _("Weather station too far"),
        "not_configured": _("Not configured"),
    }
    html_parts.append(_check(on_prem == "within_threshold", str(prem_labels.get(on_prem, on_prem))))

    return (
        '<div style="background:var(--pico-card-background-color);'
        'border:1px solid var(--pico-muted-border-color);'
        'border-radius:var(--pico-border-radius);padding:0.75rem;'
        'font-size:0.85rem">'
        + "".join(html_parts)
        + "</div>"
        + _coverage_populate_script(cov)
    )


@router.post("/marine/discover-structures", response_class=HTMLResponse)
async def marine_discover_structures(request: Request) -> HTMLResponse:
    """HTMX: discover nearby coastal structures via OSM Overpass (R-ADMIN-1).

    Mirrors the wizard's ``POST /wizard/marine/discover-structures``
    (wizard/routes.py) 1:1 against the same API endpoint
    (``ApiClient.discover_structures`` -> ``GET
    /setup/marine/discover-structures``) -- this admin edit form carries a
    single location's coordinates in flat ``lat``/``lon`` fields (no
    ``loc_N_`` prefix, unlike the wizard's multi-location cards), so the
    field lookup is simpler than the wizard's regex-over-form-keys approach.
    """
    _require_session(request)
    form = await request.form()
    lat = _marine_to_float(form.get("lat"))
    lon = _marine_to_float(form.get("lon"))
    if lat is None or lon is None:
        return HTMLResponse(
            content='<div class="admin-alert admin-alert-error" role="alert">'
            + _marine_esc(_("Enter coordinates before discovering structures."))
            + "</div>",
            status_code=200,
        )

    client = _get_api_client()
    if client is None:
        return HTMLResponse(
            content='<div class="admin-alert admin-alert-error" role="alert">'
            + _marine_esc(_("API unreachable."))
            + "</div>",
            status_code=200,
        )

    try:
        result = client.discover_structures(lat, lon, radius_m=2000)
    except Exception:  # noqa: BLE001
        logger.warning("marine_discover_structures: error", exc_info=True)
        return HTMLResponse(
            content='<div class="admin-alert admin-alert-error" role="alert">'
            + _marine_esc(_("Could not discover structures. Check API connection."))
            + "</div>",
            status_code=200,
        )

    structures = result.get("structures", [])
    if not structures:
        return HTMLResponse(
            content='<div class="admin-alert admin-alert-warning" role="status">'
            + _marine_esc(
                _('No structures found within 2 km. Use "+ Add Structure" if you know of structures nearby.')
            )
            + "</div>",
            status_code=200,
        )

    count = len(structures)
    label = _("structure") if count == 1 else _("structures")
    html_parts = [
        '<div class="admin-alert admin-alert-success" role="status">',
        f"<p><strong>{_('Found {count} {label}').format(count=count, label=label)}</strong> "
        f"{_('within 2 km. Check the ones that affect waves at your spot:')}</p>",
        "</div>",
        '<div class="admin-discovered-structures-list" '
        'style="display:flex;flex-direction:column;gap:0.5rem;margin:0.5rem 0;">',
    ]
    type_labels = {
        "breakwater": _("Breakwater"),
        "pier": _("Pier"),
        "groin": _("Groin"),
        "seawall": _("Seawall"),
        "jetty": _("Jetty"),
    }
    mat_labels = {
        "impermeable": _("Impermeable"),
        "semi_permeable": _("Semi-permeable"),
        "permeable": _("Permeable"),
    }
    for s in structures:
        stype = s.get("type", "breakwater")
        name = s.get("name") or type_labels.get(stype, stype.title())
        dist = s.get("distance_m", 0)
        length = s.get("length_m", 0)
        bearing = s.get("bearing_degrees", 0)
        mat = s.get("material") or ""
        mat_source = s.get("material_source", "operator")
        mat_display = mat_labels.get(mat, "")
        if mat_display and mat_source == "osm":
            mat_html = _marine_esc(mat_display)
        else:
            mat_html = f'<span style="color:var(--pico-del-color);">&#9888; {_marine_esc(_("Needs input"))}</span>'
        bearing_html = f"{bearing:.0f}°"
        # geometry is [[lat, lon], ...] per MarineDiscoveredStructure -- same
        # shape the wizard's route emits; JS below converts to the
        # [lon, lat] StructureConfig.coordinates contract, matching the
        # wizard's client-side conversion exactly.
        geometry = s.get("geometry") or []
        geometry_json = _marine_esc(json.dumps(geometry))
        html_parts.append(
            f'<label style="display:flex;align-items:flex-start;gap:0.75rem;padding:0.75rem;'
            f'border:1px solid var(--pico-muted-border-color);border-radius:0.375rem;cursor:pointer;">'
            f'<input type="checkbox" class="admin-discovered-structure-check" '
            f'style="margin-top:0.25rem;flex-shrink:0;" '
            f'data-type="{stype}" '
            f'data-material="{mat}" '
            f'data-material-source="{mat_source}" '
            f'data-length="{length:.1f}" '
            f'data-bearing="{bearing:.1f}" '
            f'data-distance="{dist:.1f}" '
            f'data-geometry="{geometry_json}">'
            f'<div style="flex:1;line-height:1.4;">'
            f'<strong>{_marine_esc(name)}</strong>'
            f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(10rem,1fr));'
            f'gap:0.25rem 1rem;margin-top:0.25rem;">'
            f'<small style="color:var(--pico-muted-color);">{_marine_esc(_("Type"))}: '
            f'{_marine_esc(type_labels.get(stype, stype.title()))}</small>'
            f'<small style="color:var(--pico-muted-color);">{_marine_esc(_("Distance"))}: {dist:.0f} m</small>'
            f'<small style="color:var(--pico-muted-color);">{_marine_esc(_("Length"))}: {length:.0f} m</small>'
            f'<small style="color:var(--pico-muted-color);">{_marine_esc(_("Bearing"))}: {bearing_html}</small>'
            f'<small style="color:var(--pico-muted-color);">{_marine_esc(_("Material"))}: {mat_html}</small>'
            f'</div></div></label>'
        )
    html_parts.append("</div>")

    # Emit an inline <script> that builds a .admin-structure-card fieldset
    # when a discovered structure is checked -- HTMX evaluates <script> tags
    # in swapped fragments (see _coverage_populate_script's identical
    # pattern above). This is a standalone card-builder, deliberately NOT
    # sharing code with the existing #admin-add-structure-btn handler
    # (marine.html) per the no-refactor-existing-code constraint -- the
    # wizard's equivalent (createStructureCard(), step_marine.html:815) is a
    # single helper reused by its add/discover/draw entry points; admin's
    # three entry points each build their own fieldset HTML. A future
    # consolidation round could extract a shared admin card-builder.
    html_parts.append(
        "<script>"
        "document.querySelectorAll('.admin-discovered-structure-check').forEach(function(cb){"
        "cb.addEventListener('change', function(){"
        "var container = document.getElementById('admin-structure-cards');"
        "if (!container) return;"
        "if (this.checked) {"
        "var si = container.querySelectorAll('.admin-structure-card').length;"
        "var matSrc = this.dataset.materialSource;"
        "var matWarn = (matSrc !== 'osm') ? ' \\u26A0' : '';"
        "var matVal = this.dataset.material || 'semi_permeable';"
        "var geomRaw = this.dataset.geometry;"
        "var coordsInputHtml = '';"
        "if (geomRaw) {"
        "try {"
        "var geomArr = JSON.parse(geomRaw);"
        "if (geomArr && geomArr.length) {"
        "var lonLatArr = geomArr.map(function (pt) { return [pt[1], pt[0]]; });"
        "coordsInputHtml = '<input type=\"hidden\" name=\"structure_' + si + '_coordinates\" value=\"' "
        "+ JSON.stringify(lonLatArr) + '\">';"
        "}"
        "} catch (e) {}"
        "}"
        "var fs = document.createElement('fieldset');"
        "fs.className = 'admin-structure-card';"
        "fs.style.cssText = 'margin-block:0.5rem;padding:0.75rem;border:1px solid "
        "var(--pico-muted-border-color);border-radius:4px;';"
        "fs.innerHTML = '<div style=\"display:flex;justify-content:space-between;align-items:center;\">'"
        "+ '<strong>Structure ' + (si+1) + ' (discovered)</strong>'"
        "+ '<button type=\"button\" class=\"admin-remove-structure-btn\" "
        "style=\"font-size:0.8rem;padding:0.2rem 0.5rem;cursor:pointer;background:none;"
        "border:1px solid var(--pico-del-color,#cf222e);color:var(--pico-del-color,#cf222e);"
        "border-radius:0.25rem;\">&times;</button></div>'"
        "+ '<div class=\"grid\" style=\"margin-block-start:0.5rem;\"><div><label>Type"
        "<select name=\"structure_' + si + '_type\">'"
        "+ '<option value=\"jetty\"' + (this.dataset.type==='jetty'?' selected':'') + '>Jetty</option>'"
        "+ '<option value=\"pier\"' + (this.dataset.type==='pier'?' selected':'') + '>Pier</option>'"
        "+ '<option value=\"breakwater\"' + (this.dataset.type==='breakwater'?' selected':'') + '>Breakwater</option>'"
        "+ '<option value=\"seawall\"' + (this.dataset.type==='seawall'?' selected':'') + '>Seawall</option>'"
        "+ '<option value=\"groin\"' + (this.dataset.type==='groin'?' selected':'') + '>Groin</option>'"
        "+ '</select></label></div><div><label>Material' + matWarn + '<select name=\"structure_' + si "
        "+ '_material\" class=\"struct-material\"' + (matSrc !== 'osm' ? ' style=\"border-color:"
        "var(--pico-del-color);\"' : '') + '>'"
        "+ '<option value=\"impermeable\"' + (matVal==='impermeable'?' selected':'') + '>Impermeable</option>'"
        "+ '<option value=\"semi_permeable\"' + (matVal==='semi_permeable'?' selected':'') + '>Semi-permeable</option>'"
        "+ '<option value=\"permeable\"' + (matVal==='permeable'?' selected':'') + '>Permeable</option>'"
        "+ '</select></label></div></div>'"
        "+ '<div class=\"grid\"><div><label>Length (m)<input type=\"number\" step=\"0.1\" min=\"1\" "
        "name=\"structure_' + si + '_length_m\" value=\"' + this.dataset.length + '\"></label></div>'"
        "+ '<div><label>Bearing (\\u00B0)<input type=\"number\" step=\"0.1\" min=\"0\" max=\"360\" "
        "name=\"structure_' + si + '_bearing_degrees\" value=\"' + this.dataset.bearing + '\"></label></div>'"
        "+ '<div><label>Distance (m)<input type=\"number\" step=\"0.1\" min=\"1\" "
        "name=\"structure_' + si + '_distance_m\" value=\"' + this.dataset.distance + '\"></label></div></div>'"
        "+ coordsInputHtml;"
        "container.appendChild(fs);"
        "} else {"
        "var cards = container.querySelectorAll('.admin-structure-card');"
        "if (cards.length > 0) cards[cards.length - 1].remove();"
        "}"
        "});"
        "});"
        "</script>"
    )

    return HTMLResponse(content="\n".join(html_parts), status_code=200)


# ---------------------------------------------------------------------------
# SWAN+TruShore admin section (T4.5)
# ---------------------------------------------------------------------------


@router.get("/trushore", response_class=HTMLResponse)
async def trushore_get(request: Request) -> HTMLResponse:
    """Render the SWAN+TruShore admin section.

    Non-HTMX requests (direct URL navigation) redirect to the admin landing
    page — the trushore template is a fragment designed to load inside
    #admin-content.

    Reads:
    - GET /setup/marine/swan-check for SWAN availability and status.
    - GET /setup/current-config for current trushore + marine location config.
    """
    _require_session(request)
    if not request.headers.get("HX-Request"):
        from starlette.responses import RedirectResponse as _RR
        return _RR(url="/admin/config#trushore", status_code=303)

    client = _get_api_client()
    error: str | None = None

    # SWAN availability check
    swan_info: dict[str, Any] = {
        "available": False,
        "version": None,
        "path": None,
        "cpu_cores": None,
    }
    if client is not None:
        try:
            swan_info = client._request("GET", "/setup/marine/swan-check").json()
        except Exception:  # noqa: BLE001
            logger.warning("trushore_get: swan-check failed", exc_info=True)
            error = _("Could not reach the API — check that the API is running and configured.")

    # Current configuration
    config = _fetch_current_config()
    trushore_cfg: dict[str, Any] = {}
    surf_locations: dict[str, dict[str, Any]] = {}
    if config is not None:
        # "swan", not "trushore" — /setup/current-config emits the section
        # under "swan" (API _serialize_swan_section).  Reading the old key
        # silently yielded {} and rendered defaults over the operator's saved
        # settings.  See C-67.
        trushore_cfg = config.get("swan") or {}
        marine_cfg = config.get("marine") or {}
        all_locations = _parse_marine_locations(marine_cfg)
        surf_locations = {
            slug: loc
            for slug, loc in all_locations.items()
            if "surf" in loc.get("activities", [])
        }

    return _render(request, "trushore.html", {
        "swan_info": swan_info,
        "trushore_cfg": trushore_cfg,
        "surf_locations": surf_locations,
        "error": error,
        "flash": None,
    })


@router.post("/trushore/save", response_class=HTMLResponse)
async def trushore_save(request: Request) -> HTMLResponse:
    """Save SWAN+TruShore configuration via /setup/apply.

    Updates omp_num_threads, outer_grid_resolution_km, inner_nest_resolution_m,
    and per-spot surf settings (breaker_formula, surf_height_display) for all
    surf locations.
    """
    _require_session(request)
    form = await request.form()

    client = _get_api_client()
    if client is None:
        return _render(request, "trushore.html", {
            "swan_info": {"available": False},
            "trushore_cfg": {},
            "surf_locations": {},
            "error": _("Cannot connect to the API — check that the API is running and configured."),
            "flash": None,
        })

    # Read current config so we can merge changes into a full apply payload.
    config = _fetch_current_config()
    if config is None:
        return _render(request, "trushore.html", {
            "swan_info": {"available": False},
            "trushore_cfg": {},
            "surf_locations": {},
            "error": _("Cannot connect to the API — check that the API is running and configured."),
            "flash": None,
        })

    # Build trushore config from form.  Deployment mode and service URL are
    # gone (T7.2): where SWAN runs is no longer a per-section choice.  SWAN
    # lives in the marine service, reached at the single marine_service_url
    # set in the Marine Service section.
    omp_threads_raw = str(form.get("trushore_omp_num_threads", "0")).strip()
    try:
        omp_threads = max(0, int(omp_threads_raw))
    except ValueError:
        omp_threads = 0

    # Outer grid resolution (km) — coarse continental shelf grid
    outer_res_raw = str(form.get("trushore_outer_grid_resolution_km", "3")).strip()
    try:
        outer_res = float(outer_res_raw)
        if not (1.0 <= outer_res <= 10.0):
            outer_res = 3.0
    except ValueError:
        outer_res = 3.0

    # Inner nest resolution (m) — fine grid focused on surf spots
    inner_res_raw = str(form.get("trushore_inner_nest_resolution_m", "200")).strip()
    try:
        inner_res = int(inner_res_raw)
        if not (50 <= inner_res <= 1000):
            inner_res = 200
    except ValueError:
        inner_res = 200

    trushore_payload: dict[str, Any] = {
        "omp_num_threads": omp_threads,
        "outer_grid_resolution_km": outer_res,
        "inner_nest_resolution_m": inner_res,
    }

    # Build per-spot surf settings updates.
    # We update breaker_formula and surf_height_display within each surf
    # location's surf sub-dict, then reconstruct the full marine locations
    # payload so /setup/apply gets a consistent picture.
    marine_cfg = config.get("marine") or {}
    all_locations = _parse_marine_locations(marine_cfg)
    updated_locations: list[dict[str, Any]] = []
    for loc_id, loc in all_locations.items():
        entry: dict[str, Any] = {
            "id": loc_id,
            "name": loc.get("name", ""),
            "lat": loc.get("lat", 0.0),
            "lon": loc.get("lon", 0.0),
            "activities": loc.get("activities", []),
        }
        for key in ("ndbc_station_ids", "coops_station_ids", "nws_marine_zone_id"):
            if loc.get(key):
                entry[key] = loc[key]
        if loc.get("surf"):
            surf_out = dict(loc["surf"])
            if "surf" in loc.get("activities", []):
                bf = str(form.get(f"surf_{loc_id}_breaker_formula", "")).strip()
                if bf in ("komar_gaughan", "caldwell"):
                    surf_out["breaker_formula"] = bf
                shd = str(form.get(f"surf_{loc_id}_surf_height_display", "")).strip()
                if shd in ("face", "hawaiian"):
                    surf_out["surf_height_display"] = shd
                # Convert directional_exposure list to dict for API
                exposure = surf_out.get("directional_exposure")
                if isinstance(exposure, list):
                    surf_out["directional_exposure"] = {d: True for d in exposure}
            entry["surf"] = surf_out
        if loc.get("fishing"):
            entry["fishing"] = loc["fishing"]
        if loc.get("beach_safety"):
            entry["beach_safety"] = loc["beach_safety"]
        updated_locations.append(entry)

    # The always-rewritten sections must be re-sent from the just-fetched
    # current-config — see the module-level "NOTE on write path".  Without
    # them /setup/apply rejects the request outright: ApplyRequest.database
    # has no default and the model is extra="forbid".
    apply_payload: dict[str, Any] = _build_base_apply_payload(config)
    # Key is "swan", not "trushore" — the API's ApplyRequest field was renamed
    # in the TruShore->SWAN rename and never followed here.  extra="forbid"
    # means the old key 422s the whole apply.  See C-67.
    apply_payload["swan"] = trushore_payload
    apply_payload["marine"] = {"locations": updated_locations} if updated_locations else {}

    try:
        client.apply(apply_payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trushore_save: apply failed", exc_info=True)
        swan_info_r: dict[str, Any] = {}
        try:
            swan_info_r = client._request("GET", "/setup/marine/swan-check").json()
        except Exception:  # noqa: BLE001
            pass
        surf_locations = {
            slug: loc for slug, loc in all_locations.items()
            if "surf" in loc.get("activities", [])
        }
        return _render(request, "trushore.html", {
            "swan_info": swan_info_r,
            "trushore_cfg": config.get("swan") or {},
            "surf_locations": surf_locations,
            "error": _("API error: {detail}").format(detail=str(exc)),
            "flash": None,
        }, status_code=422)

    # Re-fetch to show updated state
    updated_config = _fetch_current_config()
    updated_trushore = (updated_config or {}).get("swan") or {}
    swan_info_after: dict[str, Any] = {}
    try:
        swan_info_after = client._request("GET", "/setup/marine/swan-check").json()
    except Exception:  # noqa: BLE001
        pass

    updated_marine = (updated_config or {}).get("marine") or {}
    updated_all = _parse_marine_locations(updated_marine)
    updated_surf_locs = {
        slug: loc for slug, loc in updated_all.items()
        if "surf" in loc.get("activities", [])
    }

    return _render(request, "trushore.html", {
        "swan_info": swan_info_after,
        "trushore_cfg": updated_trushore,
        "surf_locations": updated_surf_locs,
        "error": None,
        "flash": _("SWAN+TruShore settings saved."),
    })


@router.post("/trushore/trigger-run", response_class=HTMLResponse)
async def trushore_trigger_run(request: Request) -> HTMLResponse:
    """HTMX: trigger a manual SWAN run via the API."""
    _require_session(request)
    client = _get_api_client()
    if client is None:
        return HTMLResponse(
            '<span class="error-text">'
            + _marine_esc(_("API unreachable."))
            + "</span>"
        )
    try:
        # POST to the TruShore trigger endpoint on the API.
        # The API routes this to the bundled runner or remote service as
        # appropriate for the configured deployment mode.
        client._request("POST", "/setup/marine/trushore-trigger")
        return HTMLResponse(
            '<span class="success-text">'
            + _marine_esc(_("SWAN run triggered. Results will appear after the run completes."))
            + "</span>"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("trushore_trigger_run: API error", exc_info=True)
        return HTMLResponse(
            '<span class="error-text">'
            + _marine_esc(_("SWAN run failed: {detail}").format(detail=str(exc)))
            + "</span>"
        )


# ---------------------------------------------------------------------------
# Marine Service admin section (T7.1/T7.2/T7.3/T7.5/T7.6)
#
# Replaces the former compute-offload section (F1, renamed by T7.1).  The
# marine service is not a compute-offload option: it is a companion service
# that owns all marine computation, reached only through the API
# (ARCHITECTURE.md, "The marine service is an add-on reached only through the
# API — INVARIANT").  This section records where it lives and how to
# authenticate; the API does every probe and every push.
# ---------------------------------------------------------------------------

def _marine_service_context(
    config: dict[str, Any] | None,
    *,
    error: str | None = None,
    flash: str | None = None,
    marine_config_push: dict[str, Any] | None = None,
    url_override: str | None = None,
    verify_tls_override: bool | None = None,
    has_secret_override: bool | None = None,
) -> dict[str, Any]:
    """Build the marine-service template context from a current-config response.

    The connection settings are read back from the API rather than from this
    host's own secrets.env: the API is where they live (api.conf [providers]
    plus its own secrets.env), and on a split deployment the config UI's
    secrets.env is a different file on a different host.  The secret is
    returned unmasked by /setup/current-config on purpose — CLAUDE.md, "Never
    hide operator secrets from the operator" — and only its presence is shown.

    The ``*_override`` arguments let the POST handler echo what the operator
    just submitted when the API cannot be re-read afterwards.
    """
    cfg = config or {}
    url = cfg.get("marine_service_url") or ""
    verify_tls = cfg.get("marine_verify_tls")
    has_secret = bool(cfg.get("marine_service_secret"))
    return {
        "marine_service_url": url_override if url_override is not None else str(url),
        "verify_tls": (
            verify_tls_override
            if verify_tls_override is not None
            else _marine_to_bool(verify_tls, default=True)
        ),
        "has_secret": has_secret_override if has_secret_override is not None else has_secret,
        "same_host_url": MARINE_SAME_HOST_URL,
        "marine_config_push": marine_config_push,
        "error": error,
        "flash": flash,
    }


@router.get("/marine-service", response_class=HTMLResponse)
async def marine_service_get(request: Request) -> HTMLResponse:
    """Render the Marine Service admin section.

    Non-HTMX requests redirect to the admin landing page — the template is a
    fragment designed to load inside #admin-content.
    """
    _require_session(request)
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url="/admin/config#marine-service", status_code=303)

    config = _fetch_current_config()
    error = (
        _("Cannot connect to the API — check that the API is running and configured.")
        if config is None
        else None
    )
    return _render(request, "marine-service.html", _marine_service_context(config, error=error))


@router.post("/marine-service", response_class=HTMLResponse)
async def marine_service_save(request: Request) -> HTMLResponse:
    """Save the marine service URL, TLS flag and optional new secret via /setup/apply.

    Field names are the API's top-level ``marine_service_url`` /
    ``marine_verify_tls`` / ``marine_service_secret`` (API-MANUAL §19.2) — the
    same three the wizard sends.  The always-rewritten sections are re-sent
    from the just-fetched current-config per the module-level "NOTE on write
    path"; without them ``/setup/apply`` rejects the request outright, since
    ``ApplyRequest.database`` has no default and the model is ``extra=forbid``.
    """
    _require_session(request)
    form = await request.form()

    # "Same host" wins over whatever is in the URL box: the checkbox makes the
    # field read-only client-side, so a submission carrying both a checked box
    # and a different URL means the read-only guard was bypassed, and the
    # explicit checkbox is the clearer statement of intent.
    if str(form.get("marine_same_host", "")).strip() == "1":
        marine_url = MARINE_SAME_HOST_URL
    else:
        marine_url = str(form.get("marine_service_url", "")).strip()
    # Unchecked checkboxes are not submitted; the control is always rendered
    # on this form, so absence unambiguously means "do not verify".
    verify_tls = str(form.get("marine_verify_tls", "")).strip() == "1"
    # Blank means "keep the secret already saved" — the field always renders
    # empty, so there is no way for a blank box to mean "clear it".
    new_secret = str(form.get("marine_service_secret", "")).strip()

    config = _fetch_current_config()
    if config is None:
        return _render(
            request,
            "marine-service.html",
            _marine_service_context(
                None,
                error=_(
                    "Cannot connect to the API — check that the API is running and configured."
                ),
                url_override=marine_url,
                verify_tls_override=verify_tls,
            ),
        )

    # C-64: the admin counterpart of the wizard's blank-URL guard (C-57).
    # The wizard keys its guard off state.marine_enabled, which is wizard
    # session state with no admin equivalent — the admin edits marine
    # locations in a different section from this one.  On the admin surface
    # "marine is enabled" means the *saved* configuration has at least one
    # marine location: persisted state is the right analogue for a screen
    # that has none of its own, and it is exactly what those locations
    # depend on this URL for (coordinator ruling, 2026-07-26).
    #
    # Without this, clearing the field wrote `marine_service_url = ` to
    # api.conf and reported success — Pydantic accepts "" as a str, so
    # ApplyRequest raised no 422 — silently disconnecting every configured
    # marine location.
    if not marine_url and _parse_marine_locations(config.get("marine") or {}):
        return _render(
            request,
            "marine-service.html",
            _marine_service_context(
                config,
                error=_(
                    "The marine service URL cannot be blank while marine locations are "
                    "configured — those locations would have no service to provide their "
                    "data. Enter the address of your marine service, or delete the marine "
                    "locations first."
                ),
                url_override=marine_url,
                verify_tls_override=verify_tls,
            ),
            status_code=422,
        )

    client = _get_api_client()
    if client is None:
        return _render(
            request,
            "marine-service.html",
            _marine_service_context(
                config,
                error=_(
                    "Cannot connect to the API — check that the API is running and configured."
                ),
                url_override=marine_url,
                verify_tls_override=verify_tls,
            ),
        )

    apply_payload = _build_base_apply_payload(config)
    # C-64 part 1: a blank box must never be sent as an empty string.
    # ApplyRequest.marine_service_url is `str | None = None` and Pydantic
    # accepts "" as a valid str, so an empty submission used to be written
    # verbatim into api.conf as a URL that can never resolve.  Omitting the
    # key entirely is the API's documented "leave this alone" signal
    # (setup.py: `if apply.marine_service_url is not None:` — None writes
    # nothing).  marine_verify_tls travels with it because the API writes
    # the pair inside that same branch; sending it alone would do nothing.
    # This mirrors the wizard, which already sends the key only when the
    # URL is non-empty (wizard/routes.py `if state.marine_service_url:`).
    if marine_url:
        apply_payload["marine_service_url"] = marine_url
        apply_payload["marine_verify_tls"] = verify_tls
    if new_secret:
        apply_payload["marine_service_secret"] = new_secret

    error: str | None = None
    flash: str | None = None
    marine_config_push: dict[str, Any] | None = None
    try:
        apply_response = client.apply(apply_payload)
        # T7.6: {"attempted": bool, "ok": bool, "error": str | None}.  A failed
        # push never fails the apply (API-MANUAL §19.5), so this is rendered as
        # a reachability warning, never as a failed save.  attempted=False
        # means no marine service is configured — say nothing.
        if isinstance(apply_response, dict):
            push_raw = apply_response.get("marine_config_push")
            if isinstance(push_raw, dict):
                marine_config_push = push_raw
        _restart_api_after_apply(client)
        flash = _("Marine service settings saved. The API is restarting to apply the change.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("marine_service_save: apply failed", exc_info=True)
        error = _("Save failed: {detail}").format(detail=exc)

    # Re-read for an accurate display after save.  The API is mid-restart, so
    # fall back to echoing what was just submitted when the re-read fails.
    refreshed = _fetch_current_config()
    return _render(
        request,
        "marine-service.html",
        _marine_service_context(
            refreshed,
            error=error,
            flash=flash,
            marine_config_push=marine_config_push,
            url_override=None if refreshed else marine_url,
            verify_tls_override=None if refreshed else verify_tls,
            has_secret_override=(
                None if refreshed else bool(new_secret or config.get("marine_service_secret"))
            ),
        ),
    )


@router.post("/marine-service/test", response_class=HTMLResponse)
async def marine_service_test(request: Request) -> HTMLResponse:
    """HTMX: ask the API to test the marine service connection (T7.3).

    The admin never contacts the marine service itself — that would break the
    invariant that the marine service is reached only through the API
    (ARCHITECTURE.md).  This proxies a POST to the API's
    ``/setup/providers/test-marine``, which probes the marine service and
    returns ``{ok, version, spots, last_run, status, error}``.

    The API also decides whether a failure was a rejected secret; this handler
    renders what it is told and never infers that classification itself.
    """
    _require_session(request)
    form = await request.form()
    marine_url = str(form.get("marine_service_url", "")).strip()
    marine_secret = str(form.get("marine_service_secret", "")).strip()
    verify_tls = str(form.get("marine_verify_tls", "")).strip() == "1"

    if not marine_url:
        return HTMLResponse(
            '<span class="error-text">'
            + _marine_esc(_("Enter a marine service URL before testing."))
            + "</span>"
        )

    client = _get_api_client()
    if client is None:
        return HTMLResponse(
            '<span class="error-text">'
            + _marine_esc(
                _("Cannot connect to the API — check that the API is running and configured.")
            )
            + "</span>"
        )

    # The secret box always renders empty, so an empty box means "test with the
    # secret already saved."  Read it back from the API, which is where it
    # lives, rather than from this host's own secrets.env.
    if not marine_secret:
        saved = _fetch_current_config() or {}
        marine_secret = str(saved.get("marine_service_secret") or "")

    try:
        resp = client._request(
            "POST",
            "/setup/providers/test-marine",
            json={
                "url": marine_url,
                "secret": marine_secret,
                "verify_tls": verify_tls,
            },
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        logger.debug("marine_service_test: error", exc_info=True)
        return HTMLResponse(
            '<span class="error-text">'
            + _marine_esc(_("Could not reach the API to test the marine service."))
            + "</span>"
        )

    if not data.get("ok"):
        detail = data.get("error") or _("Unknown error")
        return HTMLResponse(
            '<span class="error-text">'
            + _marine_esc(
                _("The API could not use the marine service: {error}").format(error=detail)
            )
            + "</span>",
            status_code=200,
        )

    parts = [
        '<span class="success-text">'
        + _marine_esc(_("The API reached the marine service."))
        + "</span>"
    ]
    details: list[str] = []
    version = data.get("version")
    if version:
        details.append(_("Version {version}").format(version=version))
    spots = data.get("spots")
    if spots is not None:
        details.append(_("{n} configured location(s)").format(n=spots))
    status = data.get("status")
    if status:
        details.append(_("Status: {status}").format(status=status))
    last_run = data.get("last_run")
    if last_run:
        details.append(_("Last run: {when}").format(when=last_run))
    if details:
        parts.append("<br><small>" + _marine_esc(" · ".join(str(d) for d in details)) + "</small>")
    return HTMLResponse("".join(parts))


# ---------------------------------------------------------------------------
# Status page (B4) — API health + marine service health, read-only
# ---------------------------------------------------------------------------

# Named inputs the B3 marine health payload may report under "inputs". Order
# fixed here so the panel always lists them in the same order regardless of
# JSON key ordering in the response.
_MARINE_HEALTH_INPUT_NAMES: tuple[str, ...] = ("ww3_boundary", "wind", "bathymetry", "tide")


def _format_age_seconds(age_s: Any) -> str | None:
    """Render a raw ``age_s`` value as a short human-readable duration.

    Display only — never used for any decision. Returns None (rendered as
    "unknown" by the template) for anything that is not a non-negative
    number, since a malformed value from an unverified payload must not be
    guessed at.
    """
    if isinstance(age_s, bool) or not isinstance(age_s, (int, float)):
        return None
    if age_s < 0:
        return None
    total_seconds = int(age_s)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _status_context() -> dict[str, Any]:
    """Build the context shared by both /admin/status and /admin/status/panel.

    Every value pulled from the marine health payload is read with ``.get()``
    and type-checked before use — the payload is untrusted input from a
    service that may be running an older version than this page expects
    (pre-B3: only ``status``/``version``/``last_run``/``spots``/
    ``run_in_progress``; post-B3 additionally ``reasons``/``inputs``/
    ``invariants``). A key or shape this code does not recognise is rendered
    as "not reported by this version of the marine service", never as a
    crash or a blank panel.
    """
    context: dict[str, Any] = {
        "api_reachable": False,
        "marine_reachable": None,
        "marine_error": None,
        "marine_status": None,
        "marine_version": None,
        "marine_last_run": None,
        "marine_run_in_progress": None,
        "marine_reasons": [],
        "marine_reasons_reported": False,
        "marine_inputs": [],
        "marine_inputs_reported": False,
        "marine_invariants": None,
        "marine_invariants_reported": False,
    }

    client = _get_api_client()
    if client is None:
        unreachable_msg = _(
            "Cannot connect to the API — check that the API is running and configured."
        )
        context["marine_error"] = unreachable_msg
        context["marine_reachable"] = False
        return context

    try:
        context["api_reachable"] = bool(client.health())
    except Exception:  # noqa: BLE001
        logger.warning("status page: API health() call failed", exc_info=True)
        context["api_reachable"] = False

    try:
        resp = client._request("GET", "/setup/marine/health")
        data = resp.json()
    except Exception:  # noqa: BLE001
        logger.warning("status page: /setup/marine/health call failed", exc_info=True)
        context["marine_error"] = _("Could not reach the API to check the marine service.")
        context["marine_reachable"] = False
        return context

    if not isinstance(data, dict):
        context["marine_error"] = _("The API returned an unexpected response.")
        context["marine_reachable"] = False
        return context

    reachable = bool(data.get("reachable"))
    context["marine_reachable"] = reachable
    error = data.get("error")
    context["marine_error"] = str(error) if error else None

    health = data.get("health")
    if not isinstance(health, dict):
        return context

    status = health.get("status")
    if isinstance(status, str):
        context["marine_status"] = status
    version = health.get("version")
    if version is not None:
        context["marine_version"] = str(version)
    last_run = health.get("last_run")
    if last_run is not None:
        context["marine_last_run"] = str(last_run)
    context["marine_run_in_progress"] = bool(health.get("run_in_progress"))

    reasons = health.get("reasons")
    if isinstance(reasons, list):
        context["marine_reasons_reported"] = True
        context["marine_reasons"] = [str(r) for r in reasons]

    inputs_raw = health.get("inputs")
    if isinstance(inputs_raw, dict):
        context["marine_inputs_reported"] = True
        rows: list[dict[str, Any]] = []
        for name in _MARINE_HEALTH_INPUT_NAMES:
            entry = inputs_raw.get(name)
            if not isinstance(entry, dict):
                continue
            age_s = entry.get("age_s")
            rows.append(
                {
                    "name": name,
                    "available": bool(entry.get("available")),
                    "age_s": age_s,
                    "age_human": _format_age_seconds(age_s),
                }
            )
        context["marine_inputs"] = rows

    invariants_raw = health.get("invariants")
    if isinstance(invariants_raw, dict):
        context["marine_invariants_reported"] = True
        names = invariants_raw.get("last_fired_names")
        context["marine_invariants"] = {
            "fired_total": invariants_raw.get("fired_total"),
            "last_fired_at": invariants_raw.get("last_fired_at"),
            "last_fired_names": [str(n) for n in names] if isinstance(names, list) else [],
        }

    return context


@router.get("/status", response_class=HTMLResponse, response_model=None)
async def admin_status(request: Request) -> HTMLResponse:
    """Render the Status admin section (B4): API health + marine service health.

    Read-only. Non-HTMX requests redirect to the admin landing page — the
    template is a fragment designed to load inside #admin-content, matching
    marine_service_get's pattern.
    """
    _require_session(request)
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url="/admin/config#status", status_code=303)

    context = _status_context()
    context["panel_only"] = False
    return _render(request, "status.html", context)


@router.get("/status/panel", response_class=HTMLResponse, response_model=None)
async def admin_status_panel(request: Request) -> HTMLResponse:
    """HTMX polling target: return only the inner status panel fragment.

    Polled every 30s from status.html (`hx-trigger="load, every 30s"`).
    Built from the same _status_context() as the full page so the two can
    never drift apart.
    """
    _require_session(request)
    context = _status_context()
    context["panel_only"] = True
    return _render(request, "status.html", context)
