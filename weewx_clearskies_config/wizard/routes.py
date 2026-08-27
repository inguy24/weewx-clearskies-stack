"""FastAPI router for the 15-step setup wizard.

All endpoints require an authenticated session (session cookie set by the
login flow in app.py).  The wizard uses HTMX: forms post via hx-post, and
routes return HTML fragments when the HX-Request header is present.

Route summary:
  GET  /wizard                  — full wizard page (step 1)
  GET  /wizard/step/1           — step 1 fragment (API connection)
  POST /wizard/step/1           — verify fingerprint + handshake, return step 2 fragment
  GET  /wizard/import           — step 2 fragment (import existing skin.conf or start fresh)
  POST /wizard/import           — parse uploaded skin.conf or skip, return step 3 (EULA) fragment
  GET  /wizard/eula             — step 3 fragment (Operator License Agreement)
  POST /wizard/eula             — record acceptance timestamp, return step 4 (DB) fragment
  GET  /wizard/step/2           — step 4 fragment (DB connection); pre-fills from API defaults
  POST /wizard/step/2/test      — test DB connection via API, return result fragment
  POST /wizard/step/2           — save DB settings, fetch schema via API, return step 5 or 6 fragment
  GET  /wizard/step/3           — render column mapping form using schema from state or API
  POST /wizard/step/3           — save column mapping, return step 6 fragment
  GET  /wizard/units            — step 6 fragment (display units)
  POST /wizard/units            — save unit selections, return step 7 (station) fragment
  GET  /wizard/step/4           — read station identity from API, render step 7 fragment
  POST /wizard/step/4/timezone  — lookup timezone from lat/lon, return input fragment
  POST /wizard/step/4           — save station info, return step 8 (providers) fragment
  GET  /wizard/step/6           — step 8 fragment (provider selection + inline key entry)
  GET  /wizard/step/6/key-fields/{domain}/{provider_id} — inline key fields fragment
  POST /wizard/step/6/test-key/{provider_id}            — test one provider's key, return result fragment
  POST /wizard/step/6           — save provider choices + keys, return step 9 fragment (webcam)
  GET  /wizard/step/7           — step 9 fragment (webcam configuration)
  POST /wizard/step/7           — save webcam settings, return step 10 fragment (branding)
  GET  /wizard/step/8           — step 10 fragment (appearance: branding + social)
  POST /wizard/step/8           — save branding settings, return step 11 fragment (privacy)
  GET  /wizard/privacy          — step 11 fragment (privacy, legal & analytics)
  POST /wizard/privacy          — save privacy/legal settings, return step 12 fragment (features)
  GET  /wizard/features         — step 12 fragment (feature settings: seismic page)
  POST /wizard/features         — save feature settings, return step 13 fragment (marine)
  GET  /wizard/marine           — step 13 fragment (marine location configuration)
  POST /wizard/marine           — save marine config, return step 14 fragment (TruShore or TLS)
  POST /wizard/marine/discover-stations — HTMX: discover nearby NDBC/CO-OPS stations + NWS marine zone
  GET  /wizard/marine/species           — HTMX: load the species checklist for a fishing location's
                                           target category (T2.5)
  GET  /wizard/trushore         — step 14 fragment (SWAN+TruShore nearshore model setup)
  POST /wizard/trushore         — save TruShore config, return step 15 fragment (TLS)
  POST /wizard/providers/test-marine — HTMX: ask the API to test the marine service (T7.3)
  GET  /wizard/tls              — step 15 fragment (TLS / HTTPS configuration)
  POST /wizard/tls              — save TLS config, return step 16 fragment (review)
  GET  /wizard/step/9           — step 16 fragment (review summary)
  POST /wizard/apply            — send config to API, write local config files, render completion page
"""

from __future__ import annotations

import getpass
import json
import logging
from html import escape as html_escape
import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from weewx_clearskies_config.auth import COOKIE_NAME, SessionManager
from weewx_clearskies_config.constants import MARINE_SAME_HOST_URL
from weewx_clearskies_config.i18n import (
    DEFAULT_LOCALE,
    LOCALE_COOKIE_NAME,
    get_current_locale,
    get_supported_locales,
    translate,
    translate_md,
)
from weewx_clearskies_config.wizard.api_client import (
    APPLY_TIMEOUT_SECONDS,
    ApiClient,
    ApiClientError,
)
from weewx_clearskies_config.wizard.config_writer import (
    apply_wizard,
    build_marine_payload,
    build_skin_conf_payload,
    build_trushore_payload,
    write_branding_json,
)
from weewx_clearskies_config.wizard.known_apis import load_known_apis, verify_or_pin_fingerprint
from weewx_clearskies_config.wizard.providers import (
    get_provider,
    providers_by_domain,
    test_provider,
)
from weewx_clearskies_config.wizard.schema import (
    _ALL_CANONICAL_NAMES,
    canonical_groups,
    process_api_schema,
)
from weewx_clearskies_config.wizard.state import (
    WizardState,
    configure_state_persistence,
    get_wizard_state,
    save_wizard_state,
)
from weewx_clearskies_config.wizard.skin_import import SkinImportError, parse_skin_conf_text
from weewx_clearskies_config.wizard.station import lookup_timezone
from weewx_clearskies_config.wizard.topology import generate_proxy_secret
from weewx_clearskies_config.wizard.units import (
    UNIT_GROUP_LABELS,
    UNIT_OPTIONS,
    UNIT_PRESETS,
    validate_units,
)


def _(key: str) -> str:
    """Translate *key* using the current request's wizard UI locale.

    Python-code counterpart to the Jinja2 ``_()`` global registered in
    app.py — templates call ``_()`` at render time; route handlers building
    error/status strings outside a template call this instead. Both resolve
    through the same translations/*.json files via
    weewx_clearskies_config.i18n.translate().
    """
    return translate(key, get_current_locale())




# ---------------------------------------------------------------------------
# Timezone list helper
# ---------------------------------------------------------------------------

# Prefixes to exclude from the dropdown — these are deprecated or
# system-specific zones that are not useful to operators.
_TZ_EXCLUDE_PREFIXES = (
    "Etc/",
    "SystemV/",
    "US/",    # aliases; operators should use America/*
    "Canada/",
    "Mexico/",
    "Brazil/",
    "Chile/",
    "Cuba",
    "Egypt",
    "Eire",
    "Factory",
    "GB",
    "Greenwich",
    "Hongkong",
    "Iceland",
    "Iran",
    "Israel",
    "Jamaica",
    "Japan",
    "Kwajalein",
    "Libya",
    "MET",
    "MST",
    "MST7MDT",
    "NZ",
    "Navajo",
    "Poland",
    "Portugal",
    "ROC",
    "ROK",
    "Singapore",
    "Turkey",
    "UCT",
    "UTC",
    "Universal",
    "W-SU",
    "Zulu",
    "WET",
    "CET",
    "EET",
    "EST",
    "EST5EDT",
    "CST6CDT",
    "PST8PDT",
    "HST",
    "posix/",
    "right/",
)

# Preferred region ordering for the optgroups.
_REGION_ORDER = [
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
]


def _build_timezone_list() -> list[tuple[str, list[str]]]:
    """Return a sorted list of (region, [timezone_name, ...]) tuples.

    Uses ``zoneinfo.available_timezones()`` (Python 3.9+ stdlib) to enumerate
    all IANA zone names.  Deprecated and system aliases are filtered out.
    Regions appear in the preferred order defined by ``_REGION_ORDER``; any
    remaining regions follow alphabetically.
    """
    try:
        from zoneinfo import available_timezones
    except ImportError:
        # Python < 3.9: fall back to an empty list; the template will render
        # a plain-text input as a fallback.
        return []

    all_zones = sorted(available_timezones())
    grouped: dict[str, list[str]] = {}

    for tz in all_zones:
        # Filter out deprecated / non-operator zones.
        skip = False
        for prefix in _TZ_EXCLUDE_PREFIXES:
            if tz == prefix.rstrip("/") or tz.startswith(prefix):
                skip = True
                break
        if skip:
            continue

        # Group by the part before the first slash; zones with no slash go
        # into "Other" (e.g. "UTC" if not filtered, "WET" etc.)
        region = tz.split("/", 1)[0] if "/" in tz else "Other"
        grouped.setdefault(region, []).append(tz)

    # Sort zones within each region.
    for region in grouped:
        grouped[region].sort()

    # Order regions: preferred list first, then alphabetically.
    ordered_regions = [r for r in _REGION_ORDER if r in grouped]
    remaining = sorted(r for r in grouped if r not in _REGION_ORDER)
    ordered_regions.extend(remaining)

    return [(region, grouped[region]) for region in ordered_regions]


# Build once at module load; the list is static.
_TIMEZONE_LIST: list[tuple[str, list[str]]] = _build_timezone_list()

# ---------------------------------------------------------------------------
# ADR-021 locale list
# ---------------------------------------------------------------------------

# All 13 supported locales in (bcp47_tag, human_label) order.
# The first entry ("en") is the default selection.
_SUPPORTED_LOCALES: list[tuple[str, str]] = [
    ("en",    "English (en)"),
    ("de",    "Deutsch (de)"),
    ("es",    "Español (es)"),
    ("fil",   "Filipino (fil)"),
    ("fr",    "Français (fr)"),
    ("it",    "Italiano (it)"),
    ("ja",    "日本語 (ja)"),
    ("nl",    "Nederlands (nl)"),
    ("pt-PT", "Português — Portugal (pt-PT)"),
    ("pt-BR", "Português — Brasil (pt-BR)"),
    ("ru",    "Русский (ru)"),
    ("zh-CN", "中文 简体 (zh-CN)"),
    ("zh-TW", "中文 繁體 (zh-TW)"),
]

_VALID_LOCALES: frozenset[str] = frozenset(tag for tag, _ in _SUPPORTED_LOCALES)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wizard", tags=["wizard"])


# ---------------------------------------------------------------------------
# Help content endpoint
# ---------------------------------------------------------------------------


@router.get("/help/{step_id}", response_class=HTMLResponse)
async def wizard_help(request: Request, step_id: str) -> HTMLResponse:
    """Return help content fragment for a wizard step.

    HTMX loads this into the help panel on first open.
    Keys: help.wizard.{step_id}.title, help.wizard.{step_id}.body,
          help.wizard.{step_id}.tip (optional)
    """
    _require_session(request)
    locale = get_current_locale()
    title = translate(f"help.wizard.{step_id}.title", locale)
    body = translate_md(f"help.wizard.{step_id}.body", locale)
    tip_key = f"help.wizard.{step_id}.tip"
    tip: str | None = translate(tip_key, locale)
    if tip == tip_key:
        tip = None
    return _render(
        request,
        "help_fragment.html",
        {"title": title, "body": body, "tip": tip},
    )


# Codes accepted for the wizard's own UI language (clearskies-wizard-locale
# cookie). Deliberately not reused from _VALID_LOCALES above: that set
# validates state.default_locale, the *dashboard's* default language for
# visitors (ADR-021) — a different, independently-configurable setting from
# what language the operator sees while running this wizard.
_WIZARD_LOCALE_CODES: frozenset[str] = frozenset(
    loc["code"] for loc in get_supported_locales()
)


def _guess_locale_from_accept_language(header_value: str) -> str:
    """Best-effort match of an Accept-Language header to a supported wizard locale.

    Parses the comma-separated list of language ranges (RFC 9110 §12.5.4),
    ignoring q-values beyond their ordering, and returns the first supported
    locale that matches either the full BCP-47 tag or its primary subtag.
    Falls back to English when nothing matches.
    """
    for part in header_value.split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        for code in _WIZARD_LOCALE_CODES:
            if tag.lower() == code.lower():
                return code
        primary = tag.split("-", 1)[0].lower()
        for code in _WIZARD_LOCALE_CODES:
            if primary == code.split("-", 1)[0].lower():
                return code
    return DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# Language step (step 0) — the wizard's own UI language, chosen once before
# any other step. Unrelated to the "Default language" field in step 4
# (state.default_locale), which sets the dashboard's default for visitors.
# ---------------------------------------------------------------------------


@router.get("/step/language", response_class=HTMLResponse)
async def step_language_get(request: Request) -> HTMLResponse:
    """Render the language selection step as a full page.

    Reached either by direct navigation/bookmark or by the redirect in
    wizard_index() when no locale cookie is set yet. Rendered as a full page
    (via wizard/layout.html) rather than a fragment because, unlike every
    other step, it can be the very first thing a browser requests — there is
    no existing page chrome for an HTMX fragment to swap into.
    """
    _require_session(request)
    cookie_locale = request.cookies.get(LOCALE_COOKIE_NAME, "")
    if cookie_locale in _WIZARD_LOCALE_CODES:
        selected = cookie_locale
    else:
        selected = _guess_locale_from_accept_language(request.headers.get("accept-language", ""))
    assert _templates is not None
    return _templates.TemplateResponse(
        request=request,
        name="wizard/layout.html",
        context={
            "step": 0,
            "initial_step_template": "step_language.html",
            "locales": get_supported_locales(),
            # NOTE: deliberately not named "current_locale" — that name is a
            # Jinja2 global (i18n.get_current_locale, wired up in app.py) used
            # by base.html for <html lang>. A context key of the same name
            # would shadow the global for this render and break that lookup.
            "selected_locale": selected,
        },
    )


@router.get("/set-language/{locale}", name="wizard_set_language")
async def wizard_set_language(request: Request, locale: str) -> RedirectResponse:
    """Persist the operator's chosen wizard UI language and return to the wizard.

    Sets the clearskies-wizard-locale cookie read by _LocaleMiddleware
    (app.py) on every subsequent request. An unrecognised locale code falls
    back to English rather than erroring, so a stale or hand-edited URL can't
    break the wizard.
    """
    _require_session(request)
    chosen = locale if locale in _WIZARD_LOCALE_CODES else DEFAULT_LOCALE
    response = RedirectResponse(url="/wizard", status_code=303)
    secure = bool(_session_manager and _session_manager.tls_enabled)
    response.set_cookie(
        key=LOCALE_COOKIE_NAME,
        value=chosen,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# Step 0: skin.conf Import or Fresh Start
# ---------------------------------------------------------------------------


@router.get("/import", response_class=HTMLResponse)
async def step_import_get(request: Request) -> HTMLResponse:
    """Step 2: Import an existing skin.conf or start fresh."""
    _require_session(request)
    return _render(
        request,
        "step_import.html",
        {"step": 2, "error": None},
    )


@router.post("/import", response_class=HTMLResponse)
async def step_import_post(request: Request) -> HTMLResponse:
    """Handle the skin.conf import or fresh-start choice.

    - fresh_start=1 in the form body: skip import, proceed to step 2 (database).
    - skin_name in the form body: fetch skin.conf from the API and parse it, then step 2.
    - A file upload (field name "skin_conf"): parse and store, then step 2.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    form = await request.form()
    fresh_start = str(form.get("fresh_start", "0")).strip() == "1"

    # --- Charts migration (optional) ---
    # Independent of the skin.conf import path chosen below (API fetch, file
    # upload, or fresh start) — an operator may upload graphs.conf regardless
    # of how (or whether) they import skin.conf. Failure here never blocks
    # the wizard; charts can always be configured manually later.
    graphs_file = form.get("graphs_conf_file")
    if graphs_file is not None and hasattr(graphs_file, "read"):
        filename = getattr(graphs_file, "filename", None)
        if filename:
            import tempfile

            try:
                from weewx_clearskies_api.tools.migrate_charts import migrate

                content = await graphs_file.read()
                tmp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(mode="wb", suffix=".conf", delete=False) as tmp:
                        tmp.write(content)
                        tmp_path = Path(tmp.name)
                    output_text, _log_lines, warnings = migrate(tmp_path)
                    state.charts_conf_text = output_text
                    if warnings:
                        logger.info(
                            "Charts migration completed with %d warning(s): %s",
                            len(warnings),
                            "; ".join(warnings),
                        )
                finally:
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)
            except ImportError:
                logger.warning("migrate_charts not available — skipping charts migration")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Charts migration failed: %s", exc)
                # Don't block the wizard — charts migration is optional.

    if fresh_start:
        # Clear any previously imported config and proceed to step 3 (EULA).
        state.imported_config = None
        save_wizard_state(session_id, state)
        return await step_eula_get(request)

    skin_name = str(form.get("skin_name", "")).strip()
    skin_conf_file = form.get("skin_conf")

    if skin_name:
        # API-based fetch path: retrieve skin.conf from the weewx host.
        try:
            client = _get_api_client(state)
            data = client.fetch_skin_file(skin_name, "skin.conf")
            if data is None:
                return _render(
                    request,
                    "step_import.html",
                    {
                        "step": 2,
                        "error": _(
                            "Could not find skin.conf in /etc/weewx/skins/{skin_name}/. "
                            "Check the skin name."
                        ).format(skin_name=skin_name),
                    },
                    status_code=422,
                )
            text = data.decode("utf-8", errors="replace")
        except ValueError:
            return _render(
                request,
                "step_import.html",
                {
                    "step": 2,
                    "error": _("API not connected. Complete step 1 (API Connection) before importing."),
                },
                status_code=422,
            )
        except Exception as exc:  # noqa: BLE001
            return _render(
                request,
                "step_import.html",
                {"step": 2, "error": _("Failed to fetch skin.conf: {error}").format(error=exc)},
                status_code=422,
            )

        try:
            imported = parse_skin_conf_text(text)
        except SkinImportError as exc:
            return _render(
                request,
                "step_import.html",
                {"step": 2, "error": _("Could not parse skin.conf: {error}").format(error=exc)},
                status_code=422,
            )

        state.source_skin = skin_name

    elif skin_conf_file is not None and hasattr(skin_conf_file, "read"):
        # File upload fallback path.
        try:
            raw_bytes = await skin_conf_file.read()
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return _render(
                request,
                "step_import.html",
                {"step": 2, "error": _("Could not read the uploaded file: {error}").format(error=exc)},
                status_code=422,
            )

        try:
            imported = parse_skin_conf_text(text)
        except SkinImportError as exc:
            return _render(
                request,
                "step_import.html",
                {"step": 2, "error": _("Could not parse skin.conf: {error}").format(error=exc)},
                status_code=422,
            )

        state.source_skin = "Belchertown"  # default; enhance later

    else:
        return _render(
            request,
            "step_import.html",
            {"step": 2, "error": _("Enter a skin name or upload a file, or click Skip — Start Fresh.")},
            status_code=422,
        )

    # Common post-import processing (runs for both API-fetch and file-upload paths).
    state.imported_config = imported

    # Detect image paths for later resolution (ADR-043).
    from weewx_clearskies_config.wizard.image_import import detect_image_paths, resolve_images_local

    image_paths = detect_image_paths(imported)
    if image_paths:
        if _config_dir is not None:
            results_local = resolve_images_local(
                image_paths,
                state.source_skin or "Belchertown",
                _config_dir / "branding",
            )
            state.imported_images = results_local
        else:
            # No config dir yet — record paths as unresolved for API resolution later.
            state.imported_images = {
                k: {"status": "unresolved", "dest": None, "original": v}
                for k, v in image_paths.items()
            }

    # Pre-populate unit state from the imported skin.conf groups (if present).
    # Only fill groups that exist in UNIT_OPTIONS; ignore unknown groups.
    imported_groups: dict[str, str] = imported.get("units", {}).get("groups", {})
    if imported_groups and state.units is None:
        merged: dict[str, str] = dict(UNIT_PRESETS["us"])  # start from US defaults
        valid_units_by_group = {g: {u for u, _ in opts} for g, opts in UNIT_OPTIONS.items()}
        for group, unit in imported_groups.items():
            if group in valid_units_by_group and unit in valid_units_by_group[group]:
                merged[group] = unit
        state.units = merged

    save_wizard_state(session_id, state)
    return await step_eula_get(request)


# ---------------------------------------------------------------------------
# Step 3: EULA — Operator License Agreement acceptance
# ---------------------------------------------------------------------------

# Path to the directory containing EULA text files (relative to this module).
_STATIC_DIR: Path = Path(__file__).parent.parent / "static"


def _load_eula_text(locale: str) -> str:
    """Return the EULA text for *locale*, falling back to English.

    Tries to load ``EULA_{locale}.txt`` from the static directory.  Falls back
    to the English ``EULA.txt`` if the locale-specific file is absent or cannot
    be read.
    """
    if locale and locale != "en":
        locale_path = _STATIC_DIR / f"EULA_{locale}.txt"
        try:
            return locale_path.read_text(encoding="utf-8")
        except OSError:
            pass  # Fall through to English
    english_path = _STATIC_DIR / "EULA.txt"
    try:
        return english_path.read_text(encoding="utf-8")
    except OSError:
        return ""  # Should never happen in a correctly installed package


@router.get("/eula", response_class=HTMLResponse)
async def step_eula_get(request: Request) -> HTMLResponse:
    """Step 3: EULA — render the Operator License Agreement acceptance step.

    Auto-advances to step 4 (DB) on re-run if the EULA was previously accepted
    and the version hasn't changed.  Re-acceptance is required on version change.
    Accepts ``?english=1`` to force the English (legally governing) text.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.eula_accepted_at:
        _merge_from_existing_config(state)
    if state.eula_accepted_at:
        return await step2_db_get(request)
    force_english = request.query_params.get("english") == "1"
    locale = get_current_locale()
    eula_locale = "en" if force_english else locale
    eula_text = _load_eula_text(eula_locale)
    return _render(
        request,
        "step_eula.html",
        {
            "step": 3,
            "state": state,
            "error": None,
            "eula_text": eula_text,
            "eula_is_translated": eula_locale != "en",
            "eula_showing_english": force_english and locale != "en",
        },
    )


@router.post("/eula", response_class=HTMLResponse)
async def step_eula_post(request: Request) -> HTMLResponse:
    """Step 3: EULA — validate acceptance checkbox and record timestamp, advance to step 4 (DB)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    accepted = str(form.get("eula_accepted", "")).strip() == "1"
    if not accepted:
        locale = get_current_locale()
        eula_text = _load_eula_text(locale)
        return _render(
            request,
            "step_eula.html",
            {
                "step": 3,
                "state": state,
                "error": _(
                    "You must check the acceptance box to continue. "
                    "Please scroll through the Agreement and check the box."
                ),
                "eula_text": eula_text,
                "eula_is_translated": locale != "en",
                "eula_showing_english": False,
            },
            status_code=422,
        )

    # Record acceptance timestamp in ISO-8601 UTC format.
    state.eula_accepted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    save_wizard_state(session_id, state)
    return await step2_db_get(request)


# ---------------------------------------------------------------------------
# Unit Configuration step (inserted after station identity, step 7 in new numbering)
# ---------------------------------------------------------------------------


@router.get("/units", response_class=HTMLResponse)
async def step_units_get(request: Request) -> HTMLResponse:
    """Unit configuration step: choose display units per group."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if state.units is None:
        _merge_from_existing_config(state)

    # Determine the units to pre-fill the dropdowns with.
    # Priority: 1) already-saved in state, 2) imported skin.conf, 3) US defaults.
    if state.units is not None:
        current_units = dict(state.units)
    else:
        imported_groups: dict[str, str] = {}
        if state.imported_config:
            imported_groups = state.imported_config.get("units", {}).get("groups", {})
        current_units = dict(UNIT_PRESETS["us"])
        valid_units_by_group = {g: {u for u, _ in opts} for g, opts in UNIT_OPTIONS.items()}
        for group, unit in imported_groups.items():
            if group in valid_units_by_group and unit in valid_units_by_group[group]:
                current_units[group] = unit

    return _render(
        request,
        "step_units.html",
        {
            "step": 6,
            "state": state,
            "current_units": current_units,
            "unit_options": UNIT_OPTIONS,
            "unit_group_labels": UNIT_GROUP_LABELS,
            "presets": UNIT_PRESETS,
            "error": None,
            "errors": {},
            "schema_skipped": state.schema_skipped,
        },
    )


@router.post("/units", response_class=HTMLResponse)
async def step_units_post(request: Request) -> HTMLResponse:
    """Save unit selections and advance to step 7 (station)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    # Collect unit group selections from form.
    submitted_units: dict[str, str] = {}
    for group in UNIT_OPTIONS:
        val = str(form.get(group, "")).strip()
        submitted_units[group] = val

    errors = validate_units(submitted_units)
    if errors:
        return _render(
            request,
            "step_units.html",
            {
                "step": 6,
                "state": state,
                "current_units": submitted_units,
                "unit_options": UNIT_OPTIONS,
                "unit_group_labels": UNIT_GROUP_LABELS,
                "presets": UNIT_PRESETS,
                "error": _("Please correct the errors below."),
                "errors": errors,
            },
            status_code=422,
        )

    state.units = submitted_units
    save_wizard_state(session_id, state)
    return await step4_get(request)


def _get_api_client(state: WizardState) -> ApiClient:
    """Create an API client from wizard state.

    Supports two authentication modes:

    - **Session mode** (first run): uses ``state.api_session_id`` acquired from
      the handshake call in step 1.
    - **Proxy-auth mode** (re-run): uses ``state.proxy_secret`` from the local
      secrets.env when setup is already complete and no session has been
      established.

    Raises:
        ValueError: If neither api_address nor a valid auth credential is
            available (step 1 has not been completed in either mode).
    """
    if not state.api_address:
        raise ValueError("API not connected")
    if state.api_session_id:
        return ApiClient(state.api_address, session_id=state.api_session_id)
    if state.proxy_secret:
        return ApiClient(state.api_address, proxy_secret=state.proxy_secret)
    raise ValueError("API not connected")


def _is_rerun_mode(api_address: str | None) -> bool:
    """Return True if *api_address* has a stored fingerprint AND the proxy secret exists.

    Both conditions are required: a stored fingerprint means step 1 was completed
    before, and the proxy secret means the full wizard completed (Apply wrote it
    to secrets.env).  Without the proxy secret, we fall back to first-run mode
    so the operator can use a trust token instead.
    """
    if not api_address or _config_dir is None:
        return False
    from weewx_clearskies_config.wizard.known_apis import get_known_fingerprint
    if get_known_fingerprint(_config_dir, api_address) is None:
        return False
    from weewx_clearskies_config.wizard.state_persistence import _read_secrets_env
    secrets = _read_secrets_env(_config_dir)
    return bool(secrets.get("WEEWX_CLEARSKIES_PROXY_SECRET"))


def _api_error_message(exc: ApiClientError) -> str:
    """Map an ApiClientError to a user-friendly plain-English message."""
    if exc.status_code == 401:
        return _("Your setup session has expired. Go back to step 1 and reconnect to the API.")
    if exc.status_code == 410:
        return _("This API has already been set up. If you need to reconfigure it, restart the API with the --reset flag.")
    if exc.status_code == 503:
        return _("The API is temporarily unavailable. Wait a moment and try again.")
    return _("The API returned an error ({status_code}). Check the API server log and try again.").format(status_code=exc.status_code)

# Templates are resolved at router creation time; the Jinja2Templates instance
# is set by create_wizard_router() so the caller can pass the correct path.
_templates: Jinja2Templates | None = None
_session_manager: SessionManager | None = None
_config_dir: Path | None = None
_dashboard_root: Path | None = None


def create_wizard_router(
    templates: Jinja2Templates,
    session_manager: SessionManager,
    config_dir: Path,
    dashboard_root: Path | None = None,
) -> APIRouter:
    """Configure the wizard router with shared app objects and return it."""
    global _templates, _session_manager, _config_dir, _dashboard_root  # noqa: PLW0603
    _templates = templates
    _session_manager = session_manager
    _config_dir = config_dir
    _dashboard_root = dashboard_root or Path("/var/www/clearskies")
    configure_state_persistence(config_dir)

    # Mount the operator's uploaded branding assets so they are served at a
    # stable URL (/wizard/branding/<filename>).  The directory is created on
    # demand during the first upload; StaticFiles raises an error if the
    # directory does not exist yet, so we create it here.
    branding_dir = config_dir / "branding"
    branding_dir.mkdir(parents=True, exist_ok=True)
    router.mount(
        "/branding",
        StaticFiles(directory=str(branding_dir)),
        name="wizard-branding",
    )

    return router


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_session_id(request: Request) -> str | None:
    """Extract a validated session ID from the request cookie."""
    if _session_manager is None:
        return None
    session_id = request.cookies.get(COOKIE_NAME, "")
    if not session_id or not _session_manager.get_username(session_id):
        return None
    return session_id


def _require_session(request: Request) -> str:
    """Return session_id or raise 401 if unauthenticated."""
    session_id = _get_session_id(request)
    if not session_id:
        raise _unauthorized()
    return session_id


def _unauthorized() -> Exception:
    # Wizard is an HTML UI; unauthenticated requests get a 401.  Browser
    # clients that receive it are redirected to /login by the HTMX response
    # error handler wired in layout.html.
    from starlette.exceptions import HTTPException as StarletteHTTPException
    return StarletteHTTPException(status_code=401, detail=_("Authentication required"))


def _render(
    request: Request,
    template_name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a wizard step fragment.

    Step templates are always fragments (no full-page wrapper here — the
    layout.html base wraps step 1 on initial load; subsequent steps are
    swapped into #wizard-content by HTMX).
    """
    assert _templates is not None, "Wizard router not initialised"
    return _templates.TemplateResponse(
        request=request,
        name=f"wizard/{template_name}",
        context=context,
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Wizard entry point
# ---------------------------------------------------------------------------


def _split_api_address(api_address: str) -> tuple[str, str]:
    """Split a stored ``https://{host}:{port}`` URL back into (host, port) strings.

    Returns ("", "8765") if the address is blank or cannot be parsed.
    """
    if not api_address:
        return "", "8765"
    # Strip the scheme prefix produced by step1_api_post.
    addr = api_address
    if addr.startswith("https://"):
        addr = addr[len("https://"):]
    # The host may be a bare IPv6 literal ("[::1]") or a host:port pair.
    if addr.startswith("["):
        # IPv6 bracketed literal — find the closing bracket.
        close = addr.find("]")
        if close != -1:
            host = addr[1:close]
            rest = addr[close + 1:]
            port = rest.lstrip(":") or "8765"
            return host, port
    # Plain host or host:port.
    if ":" in addr:
        host, _, port = addr.rpartition(":")
        return host, port or "8765"
    return addr, "8765"


@router.get("", response_class=HTMLResponse, response_model=None)
@router.get("/", response_class=HTMLResponse, response_model=None)
async def wizard_index(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the full wizard page with step 1 (API connection) loaded.

    First redirects to the language step if no wizard UI locale has been
    chosen yet — see step_language_get()/wizard_set_language() above.
    """
    session_id = _require_session(request)

    if LOCALE_COOKIE_NAME not in request.cookies:
        return RedirectResponse(url="/wizard/step/language", status_code=302)

    state = get_wizard_state(session_id)

    # Fresh-browser case: the session is new so state fields are blank, but a
    # previous session may have made progress.  Two recovery paths:
    #
    # 1. wizard_progress_*.json exists — merge its data into the current session so
    #    API credentials, DB settings, column mappings, and station info are
    #    restored.  Only fills fields that are still at their defaults (never
    #    overwrites data the user has already typed in the new session).
    # 2. known_apis.json exists but no progress file — pre-populate api_address
    #    from the pinned fingerprint store alone.
    if not state.api_address and _config_dir is not None:
        from weewx_clearskies_config.wizard.state_persistence import load_most_recent_progress
        prior = load_most_recent_progress(_config_dir)
        if prior is not None:
            # Merge: only fill blank fields in state from the prior session.
            # api_session_id is intentionally excluded — it expires with the old session.
            if not state.api_address and prior.api_address:
                state.api_address = prior.api_address
            if not state.cert_fingerprint and prior.cert_fingerprint:
                state.cert_fingerprint = prior.cert_fingerprint
            if state.db_kind == "mysql" and prior.db_kind != "mysql":
                state.db_kind = prior.db_kind
            if state.db_host is None and prior.db_host is not None:
                state.db_host = prior.db_host
            if state.db_port == 3306 and prior.db_port != 3306:
                state.db_port = prior.db_port
            if state.db_user is None and prior.db_user is not None:
                state.db_user = prior.db_user
            if state.db_password is None and prior.db_password is not None:
                state.db_password = prior.db_password
            if state.db_name == "weewx" and prior.db_name != "weewx":
                state.db_name = prior.db_name
            if not state.db_path and prior.db_path:
                state.db_path = prior.db_path
            if not state.column_mapping and prior.column_mapping:
                state.column_mapping = prior.column_mapping
            if state.station_name is None and prior.station_name is not None:
                state.station_name = prior.station_name
            if state.latitude is None and prior.latitude is not None:
                state.latitude = prior.latitude
            if state.longitude is None and prior.longitude is not None:
                state.longitude = prior.longitude
            if state.altitude_meters is None and prior.altitude_meters is not None:
                state.altitude_meters = prior.altitude_meters
            if state.timezone is None and prior.timezone is not None:
                state.timezone = prior.timezone
            if not state.providers and prior.providers:
                state.providers = prior.providers
            if not state.api_keys and prior.api_keys:
                state.api_keys = prior.api_keys
            if state.topology == "same-host" and prior.topology != "same-host":
                state.topology = prior.topology
            if state.proxy_secret is None and prior.proxy_secret is not None:
                state.proxy_secret = prior.proxy_secret
            if state.default_locale == "en" and prior.default_locale != "en":
                state.default_locale = prior.default_locale
            if state.api_bind_host == "127.0.0.1" and prior.api_bind_host != "127.0.0.1":
                state.api_bind_host = prior.api_bind_host
            if state.api_bind_port == 8765 and prior.api_bind_port != 8765:
                state.api_bind_port = prior.api_bind_port
            if not state.webcam_enabled and prior.webcam_enabled:
                state.webcam_enabled = prior.webcam_enabled
            if state.webcam_image_url == "/webcam/weather_cam.jpg" and prior.webcam_image_url != "/webcam/weather_cam.jpg":
                state.webcam_image_url = prior.webcam_image_url
            if state.webcam_video_url == "/webcam/weewx_timelapse.mp4" and prior.webcam_video_url != "/webcam/weewx_timelapse.mp4":
                state.webcam_video_url = prior.webcam_video_url
            if state.webcam_refresh_interval == 60 and prior.webcam_refresh_interval != 60:
                state.webcam_refresh_interval = prior.webcam_refresh_interval
            if not state.site_title and prior.site_title:
                state.site_title = prior.site_title
            if not state.copyright_entity and prior.copyright_entity:
                state.copyright_entity = prior.copyright_entity
            if not state.logo_light_url and prior.logo_light_url:
                state.logo_light_url = prior.logo_light_url
            if not state.logo_dark_url and prior.logo_dark_url:
                state.logo_dark_url = prior.logo_dark_url
            if not state.logo_alt and prior.logo_alt:
                state.logo_alt = prior.logo_alt
            if not state.favicon_url and prior.favicon_url:
                state.favicon_url = prior.favicon_url
            if not state.accent and prior.accent:
                state.accent = prior.accent
            if not state.default_theme_mode and prior.default_theme_mode:
                state.default_theme_mode = prior.default_theme_mode
            if not state.google_analytics_id and prior.google_analytics_id:
                state.google_analytics_id = prior.google_analytics_id
            if not state.privacy_regions and prior.privacy_regions:
                state.privacy_regions = prior.privacy_regions
            if state.earthquake_radius_km == 100.0 and prior.earthquake_radius_km != 100.0:
                state.earthquake_radius_km = prior.earthquake_radius_km
            if state.earthquake_min_magnitude == 2.0 and prior.earthquake_min_magnitude != 2.0:
                state.earthquake_min_magnitude = prior.earthquake_min_magnitude
            if state.earthquake_default_days == 7 and prior.earthquake_default_days != 7:
                state.earthquake_default_days = prior.earthquake_default_days
            if not state.custom_terms_md and prior.custom_terms_md:
                state.custom_terms_md = prior.custom_terms_md
            if not state.custom_privacy_md and prior.custom_privacy_md:
                state.custom_privacy_md = prior.custom_privacy_md
            if not state.about_content and prior.about_content:
                state.about_content = prior.about_content
            if not state.station_photo_url and prior.station_photo_url:
                state.station_photo_url = prior.station_photo_url
            if not state.station_photo_alt and prior.station_photo_alt:
                state.station_photo_alt = prior.station_photo_alt
            # AQI regional configuration (ADR-059)
            if state.aeris_aqi_filter == "airnow" and prior.aeris_aqi_filter != "airnow":
                state.aeris_aqi_filter = prior.aeris_aqi_filter
            if state.openmeteo_aqi_index == "us_aqi" and prior.openmeteo_aqi_index != "us_aqi":
                state.openmeteo_aqi_index = prior.openmeteo_aqi_index
            if state.iqair_aqi_scale == "us" and prior.iqair_aqi_scale != "us":
                state.iqair_aqi_scale = prior.iqair_aqi_scale
            # LibreWxR radar configuration
            if state.librewxr_endpoint == "https://api.librewxr.net" and prior.librewxr_endpoint != "https://api.librewxr.net":
                state.librewxr_endpoint = prior.librewxr_endpoint
            if not state.librewxr_bounds and prior.librewxr_bounds:
                state.librewxr_bounds = prior.librewxr_bounds
            # TLS configuration (step 14)
            if not state.tls_mode and prior.tls_mode:
                state.tls_mode = prior.tls_mode
            if not state.tls_domain and prior.tls_domain:
                state.tls_domain = prior.tls_domain
            if not state.tls_acme_email and prior.tls_acme_email:
                state.tls_acme_email = prior.tls_acme_email
            if not state.tls_dns_provider and prior.tls_dns_provider:
                state.tls_dns_provider = prior.tls_dns_provider
            if not state.tls_dns_api_token and prior.tls_dns_api_token:
                state.tls_dns_api_token = prior.tls_dns_api_token
            if not state.tls_cert_path and prior.tls_cert_path:
                state.tls_cert_path = prior.tls_cert_path
                state.tls_cert_uploaded = prior.tls_cert_uploaded
            if not state.tls_key_path and prior.tls_key_path:
                state.tls_key_path = prior.tls_key_path
                state.tls_key_uploaded = prior.tls_key_uploaded
            save_wizard_state(session_id, state)
            logger.info("Restored wizard progress from prior session into session %s", session_id[:8])
        else:
            known = load_known_apis(_config_dir)
            if known:
                # Use the first (typically only) pinned API URL.
                state.api_address = next(iter(known))
                save_wizard_state(session_id, state)

    api_host, api_port = _split_api_address(state.api_address or "")
    rerun = _is_rerun_mode(state.api_address)
    assert _templates is not None
    return _templates.TemplateResponse(
        request=request,
        name="wizard/layout.html",
        context={
            "step": 1,
            "state": state,
            "success": False,
            "api_host": api_host,
            "api_port": api_port,
            "rerun_mode": rerun,
            "error": None,
        },
    )


# ---------------------------------------------------------------------------
# Step 1: API Connection
# ---------------------------------------------------------------------------


@router.get("/step/1", response_class=HTMLResponse)
async def step1_api_get(request: Request) -> HTMLResponse:
    """Step 1: Connect to API — show the connection form."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    api_host, api_port = _split_api_address(state.api_address or "")
    rerun = _is_rerun_mode(state.api_address)
    return _render(
        request,
        "step_api.html",
        {"step": 1, "state": state, "error": None, "success": False,
         "api_host": api_host, "api_port": api_port, "rerun_mode": rerun},
    )


@router.post("/step/1", response_class=HTMLResponse)
async def step1_api_post(request: Request) -> HTMLResponse:
    """Step 1: Connect to API — verify fingerprint + handshake, show success feedback.

    Two modes:

    - **First run**: operator provides a trust token and certificate fingerprint.
      The wizard exchanges the token for a setup session ID (handshake) and
      pins the fingerprint in known_apis.json.

    - **Re-run**: setup was completed before; the fingerprint is already stored
      in known_apis.json and the proxy secret is in secrets.env.  The trust
      token is not required.  The wizard verifies the live fingerprint still
      matches the stored pin, then uses X-Clearskies-Proxy-Auth for all
      subsequent API calls.
    """
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    host = str(form.get("api_host", "")).strip()
    port_raw = str(form.get("api_port", "8765")).strip() or "8765"
    trust_token = str(form.get("trust_token", "")).strip()
    cert_fingerprint = str(form.get("cert_fingerprint", "")).strip()

    # Validate host is always required.
    if not host:
        return _render(
            request,
            "step_api.html",
            {"step": 1, "state": state,
             "error": _("API host is required."),
             "success": False, "api_host": host, "api_port": port_raw,
             "rerun_mode": _is_rerun_mode(state.api_address)},
            status_code=422,
        )

    # Normalise port — must be an integer in 1–65535.
    port = _parse_int(port_raw, default=8765)
    if not (1 <= port <= 65535):
        return _render(
            request,
            "step_api.html",
            {"step": 1, "state": state, "error": _("Port must be between 1 and 65535."),
             "success": False, "api_host": host, "api_port": port_raw,
             "rerun_mode": _is_rerun_mode(state.api_address)},
            status_code=422,
        )

    api_address = f"https://{host}:{port}"
    assert _config_dir is not None, "config_dir not set — wizard router not initialised"

    # Detect which mode we are in before doing anything else.
    rerun = _is_rerun_mode(api_address)

    if rerun:
        # Re-run mode: verify the fingerprint is still pinned (no operator input
        # needed for the fingerprint itself — we compare live vs. stored).
        # Fetch the proxy secret from secrets.env for auth.
        from weewx_clearskies_config.wizard.known_apis import get_known_fingerprint
        stored_fp = get_known_fingerprint(_config_dir, api_address) or ""
        ok, err_msg = verify_or_pin_fingerprint(_config_dir, api_address, stored_fp)
        if not ok:
            return _render(
                request,
                "step_api.html",
                {"step": 1, "state": state, "error": err_msg,
                 "success": False, "api_host": host, "api_port": str(port),
                 "rerun_mode": True},
                status_code=422,
            )

        # Load the proxy secret from secrets.env so subsequent API calls can use
        # X-Clearskies-Proxy-Auth.  If the secret is absent (edge case: operator
        # deleted secrets.env), fall back gracefully with a clear error.
        from weewx_clearskies_config.wizard.state_persistence import _read_secrets_env
        secrets = _read_secrets_env(_config_dir)
        proxy_secret = secrets.get("WEEWX_CLEARSKIES_PROXY_SECRET") or state.proxy_secret
        if not proxy_secret:
            return _render(
                request,
                "step_api.html",
                {
                    "step": 1,
                    "state": state,
                    "error": _(
                        "Re-run failed: the proxy secret is not in secrets.env. "
                        "If you deleted secrets.env, re-run the installer to regenerate it, "
                        "or restart the API with --reset to start a fresh setup."
                    ),
                    "success": False,
                    "api_host": host,
                    "api_port": str(port),
                    "rerun_mode": True,
                },
                status_code=422,
            )

        # Verify the proxy secret works by probing the API health endpoint.
        try:
            client = ApiClient(api_address, proxy_secret=proxy_secret)
            client.get_db_defaults()  # Any authenticated endpoint will do.
        except ApiClientError as exc:
            if exc.status_code == 401:
                error_msg = _(
                    "The API did not accept the proxy secret. "
                    "Check that secrets.env contains the correct WEEWX_CLEARSKIES_PROXY_SECRET "
                    "and that the API is running with the same value."
                )
            else:
                error_msg = _("Could not verify the API connection: {detail}").format(detail=_api_error_message(exc))
            return _render(
                request,
                "step_api.html",
                {"step": 1, "state": state, "error": error_msg,
                 "success": False, "api_host": host, "api_port": str(port),
                 "rerun_mode": True},
                status_code=422,
            )
        except Exception:  # noqa: BLE001
            return _render(
                request,
                "step_api.html",
                {"step": 1, "state": state,
                 "error": _("Could not reach the API. Check the address and try again."),
                 "success": False, "api_host": host, "api_port": str(port),
                 "rerun_mode": True},
                status_code=422,
            )

        # Success — store address and proxy secret; leave api_session_id unset so
        # _get_api_client() uses proxy-auth mode for all subsequent steps.
        state.api_address = api_address
        state.proxy_secret = proxy_secret
        # api_session_id is intentionally left as-is (may be None or stale —
        # _get_api_client prefers session_id when set, so clear it).
        state.api_session_id = None

        # Pre-populate all fields from the API's current config so the operator
        # doesn't have to re-enter every password and API key on re-run.
        _merge_from_api_current_config(client, state)

        save_wizard_state(session_id, state)
        return _render(
            request,
            "step_api.html",
            {
                "step": 1,
                "state": state,
                "error": None,
                "success": True,
                "api_host": host,
                "api_port": str(port),
                "rerun_mode": True,
            },
        )

    # ------------------------------------------------------------------
    # First-run mode: trust token + fingerprint required.
    # ------------------------------------------------------------------
    if not trust_token or not cert_fingerprint:
        return _render(
            request,
            "step_api.html",
            {"step": 1, "state": state, "error": _("All fields are required."),
             "success": False, "api_host": host, "api_port": port_raw,
             "rerun_mode": False},
            status_code=422,
        )

    # Verify fingerprint (TOFU) — fetch the live cert and compare.
    ok, err_msg = verify_or_pin_fingerprint(_config_dir, api_address, cert_fingerprint)
    if not ok:
        return _render(
            request,
            "step_api.html",
            {"step": 1, "state": state, "error": err_msg,
             "success": False, "api_host": host, "api_port": str(port),
             "rerun_mode": False},
            status_code=422,
        )

    # Handshake — exchange the one-time trust token for a setup session ID.
    try:
        client = ApiClient(api_address)
        api_session_id = client.handshake(trust_token)
    except ApiClientError as exc:
        if exc.status_code == 401:
            error_msg = _("Invalid trust token. Check the token printed in the API terminal and try again.")
        elif exc.status_code == 410:
            error_msg = _("This API has already been set up. If you need to reconfigure it, restart the API with the --reset flag.")
        else:
            error_msg = _("Could not connect to the API. Check the address and try again.")
        return _render(
            request,
            "step_api.html",
            {"step": 1, "state": state, "error": error_msg,
             "success": False, "api_host": host, "api_port": str(port),
             "rerun_mode": False},
            status_code=422,
        )
    except Exception:  # noqa: BLE001
        return _render(
            request,
            "step_api.html",
            {"step": 1, "state": state,
             "error": _("Could not reach the API. Check the address and try again."),
             "success": False, "api_host": host, "api_port": str(port),
             "rerun_mode": False},
            status_code=422,
        )

    # Success — store in wizard state and re-render step 1 with success feedback.
    # The operator sees a green confirmation and a "Continue" button.  This gives
    # clear evidence that the handshake worked before advancing to step 2.
    state.api_address = api_address
    state.api_session_id = api_session_id
    state.cert_fingerprint = cert_fingerprint
    save_wizard_state(session_id, state)
    return _render(
        request,
        "step_api.html",
        {
            "step": 1,
            "state": state,
            "error": None,
            "success": True,
            "api_host": host,
            "api_port": str(port),
            "rerun_mode": False,
        },
    )


# ---------------------------------------------------------------------------
# Step 2: DB connection
# ---------------------------------------------------------------------------


@router.get("/step/2", response_class=HTMLResponse)
async def step2_db_get(request: Request) -> HTMLResponse:
    """Step 2: DB connection — pre-fill form from API defaults or saved state."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    # Merge from existing config files (e.g. wizard re-run).
    if state.db_host is None and not state.db_path:
        _merge_from_existing_config(state)

    # If still no DB info, ask the API for defaults from its weewx.conf.
    api_warning: str | None = None
    if state.db_host is None and not state.db_path:
        try:
            client = _get_api_client(state)
            defaults = client.get_db_defaults()
            kind = str(defaults.get("kind", "mysql")).strip().lower()
            if kind not in ("sqlite", "mysql"):
                kind = "mysql"
            state.db_kind = kind
            if kind == "sqlite":
                state.db_path = str(defaults.get("path", "")) or state.db_path
            else:
                state.db_host = str(defaults.get("host", "localhost")) or "localhost"
                if defaults.get("port"):
                    state.db_port = int(defaults["port"])
                if defaults.get("user"):
                    state.db_user = str(defaults["user"])
                if defaults.get("name"):
                    state.db_name = str(defaults["name"])
            # Never pre-fill the password from the API response — the operator
            # must enter it explicitly (the API doesn't transmit passwords).
        except ValueError:
            # API not connected yet — user navigated directly to step 2.
            pass
        except ApiClientError as exc:
            if exc.status_code == 401:
                # Session expired — redirect to step 1.
                return await step1_api_get(request)
            api_warning = _("Could not fetch database defaults from the API. Enter the settings below.")
            logger.warning("get_db_defaults failed: %s", exc)
        except Exception:  # noqa: BLE001
            api_warning = _("Could not reach the API to fetch database defaults. Enter the settings below.")
            logger.warning("get_db_defaults network error", exc_info=True)

    return _render(
        request,
        "step_db.html",
        {"step": 4, "state": state, "result": None, "error": api_warning, "db_kind": state.db_kind},
    )


@router.post("/step/2/test", response_class=HTMLResponse)
async def step2_db_test(request: Request) -> HTMLResponse:
    """Test the DB connection via the API without saving; return a result fragment."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    form = await request.form()
    db_kind = str(form.get("db_kind", "mysql")).strip().lower()
    if db_kind not in ("sqlite", "mysql"):
        db_kind = "mysql"

    result: dict[str, Any]
    try:
        client = _get_api_client(state)
        if db_kind == "sqlite":
            db_path = str(form.get("db_path", "")).strip()
            result = client.test_db(kind="sqlite", path=db_path)
        else:
            host = str(form.get("db_host", "localhost")).strip()
            port = _parse_int(str(form.get("db_port", "3306")), default=3306)
            user = str(form.get("db_user", "")).strip()
            password = str(form.get("db_password", ""))
            db_name = str(form.get("db_name", "weewx")).strip()
            result = client.test_db(kind="mysql", host=host, port=port, user=user, password=password, name=db_name)
    except ValueError:
        result = {"success": False, "error": _("API not connected. Go back to step 1 and reconnect."), "version": None}
    except ApiClientError as exc:
        if exc.status_code == 401:
            result = {"success": False, "error": _("Your setup session has expired. Go back to step 1 to reconnect."), "version": None}
        else:
            result = {"success": False, "error": _api_error_message(exc), "version": None}
    except Exception:  # noqa: BLE001
        result = {"success": False, "error": _("Could not reach the API to test the connection. Check that the API is running and try again."), "version": None}

    return _render(
        request,
        "step_db_test_result.html",
        {"result": result},
    )


@router.post("/step/2", response_class=HTMLResponse)
async def step2_db_post(request: Request) -> HTMLResponse:
    """Save DB settings, fetch schema via API, advance to step 3 or 4."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    db_kind = str(form.get("db_kind", "mysql")).strip().lower()
    if db_kind not in ("sqlite", "mysql"):
        db_kind = "mysql"
    state.db_kind = db_kind

    if db_kind == "sqlite":
        state.db_path = str(form.get("db_path", "")).strip()
        # A SQLite database is always a local file next to the API process —
        # there is no remote host to reach, so topology is always same-host.
        state.topology = "same-host"
        state.api_bind_host = "127.0.0.1"
    else:
        state.db_host = str(form.get("db_host", "")).strip() or None
        state.db_port = _parse_int(str(form.get("db_port", "3306")), default=3306)
        state.db_user = str(form.get("db_user", "")).strip() or None
        # Password handling: the template sends db_password_unchanged=1 and an empty
        # password field when the user has not re-typed a password on re-render.
        # Only overwrite the stored secret when the user actually supplies a value.
        submitted_db_password = str(form.get("db_password", ""))
        db_password_unchanged = str(form.get("db_password_unchanged", "0")).strip() == "1"
        if submitted_db_password:
            state.db_password = submitted_db_password
        elif not db_password_unchanged:
            # Explicit blank entry with the flag cleared — user cleared the password.
            state.db_password = ""
        # else: flag is set and field is empty — keep existing state.db_password.
        state.db_name = str(form.get("db_name", "weewx")).strip() or "weewx"

        # Auto-detect topology from DB host: loopback → same-host, anything else → cross-host.
        _LOOPBACK = {"localhost", "127.0.0.1", "::1"}
        if (state.db_host or "").lower() in _LOOPBACK:
            state.topology = "same-host"
            state.api_bind_host = "127.0.0.1"
        else:
            state.topology = "cross-host"
            if not state.proxy_secret:
                state.proxy_secret = generate_proxy_secret()
            state.api_bind_host = "0.0.0.0"
    state.api_bind_port = 8765

    # Persist the DB fields entered by the user so partial progress survives even
    # if the connection test or schema fetch fails (user can adjust and retry).
    save_wizard_state(session_id, state)

    # Test the connection via API before proceeding.
    try:
        client = _get_api_client(state)
        if state.db_kind == "sqlite":
            test_result = client.test_db(kind="sqlite", path=state.db_path or "")
        else:
            test_result = client.test_db(
                kind="mysql",
                host=state.db_host or "",
                port=state.db_port,
                user=state.db_user or "",
                password=state.db_password or "",
                name=state.db_name,
            )
        if not test_result.get("success"):
            error_msg = f"Connection test failed: {test_result.get('error', 'unknown error')}"
            return _render(
                request,
                "step_db.html",
                {"step": 4, "state": state, "result": None, "error": error_msg, "db_kind": state.db_kind},
                status_code=422,
            )
    except ValueError:
        return _render(
            request,
            "step_db.html",
            {"step": 4, "state": state, "result": None, "error": _("API not connected. Go back to step 1 and reconnect."), "db_kind": state.db_kind},
            status_code=422,
        )
    except ApiClientError as exc:
        if exc.status_code == 401:
            return await step1_api_get(request)
        return _render(
            request,
            "step_db.html",
            {"step": 4, "state": state, "result": None, "error": _api_error_message(exc), "db_kind": state.db_kind},
            status_code=422,
        )
    except Exception:  # noqa: BLE001
        return _render(
            request,
            "step_db.html",
            {"step": 4, "state": state, "result": None, "error": _("Could not reach the API to test the connection. Check that the API is running and try again."), "db_kind": state.db_kind},
            status_code=422,
        )

    # Fetch schema via API and process it.
    try:
        api_schema = client.get_schema()
        schema_data = process_api_schema(api_schema)
        state.schema_data = schema_data
    except ApiClientError as exc:
        logger.warning("get_schema failed in step2_db_post (%s): %s", exc.status_code, exc.detail)
        state.schema_data = None
    except Exception:  # noqa: BLE001
        logger.warning("get_schema network error in step2_db_post", exc_info=True)
        state.schema_data = None

    state.schema_skipped = False
    save_wizard_state(session_id, state)
    return await step3_get(request)


# ---------------------------------------------------------------------------
# Step 3: Schema + Column Mapping
# ---------------------------------------------------------------------------


@router.get("/step/3", response_class=HTMLResponse)
async def step3_get(request: Request) -> HTMLResponse:
    """Step 3: Column mapping — use schema cached in state or re-fetch from API."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.column_mapping:
        _merge_from_existing_config(state)

    schema_data: dict[str, Any] | None = state.schema_data
    error: str | None = None

    # If schema wasn't cached (e.g. user navigated directly to step 3), fetch it.
    if schema_data is None:
        try:
            client = _get_api_client(state)
            api_schema = client.get_schema()
            schema_data = process_api_schema(api_schema)
            state.schema_data = schema_data
            save_wizard_state(session_id, state)
        except ValueError:
            error = _("API not connected. Go back to step 1 and reconnect.")
        except ApiClientError as exc:
            if exc.status_code == 401:
                return await step1_api_get(request)
            error = _("Could not fetch the database schema from the API — check your connection settings in step 2 and try again.")
            logger.warning("get_schema failed in step3_get: %s", exc)
        except Exception:  # noqa: BLE001
            error = _("Could not reach the API to fetch the database schema. Check that the API is running and try again.")
            logger.warning("get_schema network error in step3_get", exc_info=True)

    # If the user has previously saved column mappings (e.g. they advanced to step 4
    # then clicked Previous, or re-run after apply), overlay those choices onto the
    # schema's suggested values so the dropdowns pre-select what they chose rather
    # than the heuristic suggestion.
    if schema_data is not None and state.column_mapping:
        saved_canonicals: set[str] = {
            v for v in state.column_mapping.values() if v
        }
        for col in schema_data.get("unmapped_columns", []):
            if col["db_name"] in state.column_mapping:
                # Column has a saved mapping (canonical name) or was explicitly
                # excluded (None).  Either way, use the saved value.
                saved = state.column_mapping[col["db_name"]]
                col["suggested"] = saved if saved else None
                col["confidence"] = "saved" if saved else "none"
            elif col.get("suggested") and col["suggested"] in saved_canonicals:
                # Column is NEW (not in saved mapping) but its heuristic
                # suggestion conflicts with a canonical name already claimed
                # by a saved mapping.  Clear to prevent duplicate errors.
                col["suggested"] = None
                col["confidence"] = "none"

    return _render(
        request,
        "step_schema.html",
        {"step": 5, "state": state, "schema": schema_data, "error": error, "errors": {}, "canonical_groups": canonical_groups},
    )


@router.post("/step/3", response_class=HTMLResponse)
async def step3_post(request: Request) -> HTMLResponse:
    """Save column mapping choices and advance to step 6 (units)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    # Form fields are named "col_<db_column_name>" for each unmapped column.
    mapping: dict[str, str | None] = {}
    for key, value in form.multi_items():
        if key.startswith("col_"):
            db_col = key[4:]  # strip "col_" prefix
            canonical = str(value).strip() or None
            mapping[db_col] = canonical

    errors = _validate_column_mapping(mapping)
    if errors:
        # Re-use schema data from state; fall back to API if not cached.
        schema_data: dict[str, Any] | None = state.schema_data
        schema_error: str | None = None
        if schema_data is None:
            try:
                client = _get_api_client(state)
                api_schema = client.get_schema()
                schema_data = process_api_schema(api_schema)
                state.schema_data = schema_data
            except Exception as exc:  # noqa: BLE001
                schema_error = _("Could not read the database schema — check your connection settings in step 2 and try again.")
                logger.warning("get_schema error in step3_post: %s", exc)
        return _render(
            request,
            "step_schema.html",
            {
                "step": 5,
                "state": state,
                "schema": schema_data,
                "error": schema_error,
                "errors": errors,
                "canonical_groups": canonical_groups,
            },
            status_code=422,
        )

    # Collect confirmed unit assignments from form fields named "unit_<db_name>".
    # Both stock columns (hidden inputs) and custom columns submit their units.
    column_units: dict[str, str] = {}
    for key, value in form.multi_items():
        if key.startswith("unit_"):
            db_col = key[5:]  # strip "unit_" prefix
            unit_str = str(value).strip()
            if unit_str:
                column_units[db_col] = unit_str
    state.column_units = column_units

    # Merge form submissions with pre-existing state (e.g. stock columns set by
    # step 2, or custom mappings loaded from api.conf on re-run).  Form fields
    # only cover the unmapped columns, so existing entries must be preserved.
    merged = dict(state.column_mapping or {})
    merged.update(mapping)
    state.column_mapping = merged
    state.schema_data = None  # Clear cached schema data — no longer needed.
    save_wizard_state(session_id, state)
    return await step_units_get(request)


# ---------------------------------------------------------------------------
# Step 4: Station Identity
# ---------------------------------------------------------------------------


@router.get("/step/4", response_class=HTMLResponse)
async def step4_get(request: Request) -> HTMLResponse:
    """Step 4: Station identity — pre-fill from API (reads weewx.conf server-side)."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    error: str | None = None

    # Pre-fill from API only when station fields are still empty.
    if state.station_name is None:
        # Try existing config files first (wizard re-run).
        _merge_from_existing_config(state)

        # If the config merge gave us lat/lon but no timezone, auto-detect now.
        if state.latitude and state.longitude and not state.timezone:
            state.timezone = lookup_timezone(state.latitude, state.longitude)

    if state.station_name is None:
        # Ask the API to read weewx.conf server-side.
        try:
            client = _get_api_client(state)
            api_data = client.get_station()
            if api_data:
                if state.station_name is None:
                    state.station_name = api_data.get("station_name") or None
                if state.latitude is None:
                    state.latitude = _to_float(api_data.get("latitude"))
                if state.longitude is None:
                    state.longitude = _to_float(api_data.get("longitude"))
                if state.altitude_meters is None:
                    raw_alt = _to_float(api_data.get("altitude_meters"))
                    alt_unit = str(api_data.get("altitude_unit", "meter")).strip().lower()
                    # The API returns the raw numeric value from weewx.conf without
                    # converting units.  Convert feet → meters so state.altitude_meters
                    # is always in meters (matching the field name and step4_post logic).
                    if raw_alt is not None and ("foot" in alt_unit or "feet" in alt_unit or alt_unit == "ft"):
                        state.altitude_meters = raw_alt * 0.3048
                        state.altitude_unit = "feet"
                    else:
                        state.altitude_meters = raw_alt
                        state.altitude_unit = "meters"
                if state.timezone is None and api_data.get("timezone"):
                    state.timezone = str(api_data["timezone"])
                if state.latitude and state.longitude and not state.timezone:
                    state.timezone = lookup_timezone(state.latitude, state.longitude)
        except ValueError:
            # API not connected — show blank form (user navigated directly).
            pass
        except ApiClientError as exc:
            if exc.status_code == 401:
                return await step1_api_get(request)
            error = _("Could not fetch station details from the API. Fill in the fields below manually.")
            logger.warning("get_station failed in step4_get: %s", exc)
        except Exception:  # noqa: BLE001
            error = _("Could not reach the API to fetch station details. Fill in the fields below manually.")
            logger.warning("get_station network error in step4_get", exc_info=True)

    # On re-run, altitude_meters may already be set (from old progress file)
    # but altitude_unit stuck at the default "meters".  Fetch the actual unit
    # from the API so the template shows the station's native unit.
    if state.altitude_meters is not None and state.altitude_unit == "meters":
        try:
            client = _get_api_client(state)
            api_data = client.get_station()
            if api_data:
                alt_unit = str(api_data.get("altitude_unit", "meter")).strip().lower()
                if "foot" in alt_unit or "feet" in alt_unit or alt_unit == "ft":
                    state.altitude_unit = "feet"
        except Exception:  # noqa: BLE001
            pass

    # About content lives in branding.json (static, not API data).  Populate
    # unconditionally — the station_name guard above skips _merge_from_existing_config
    # when station_name is already set from the API, so about_content never loads.
    if not state.about_content and _config_dir:
        from weewx_clearskies_config.wizard.state_persistence import populate_from_branding_json
        populate_from_branding_json(state, _config_dir)

    # Default the dashboard language to the wizard's UI locale if not already
    # set — the operator likely wants visitors to see the same language.
    if not state.default_locale:
        state.default_locale = get_current_locale()

    return _render(
        request,
        "step_station.html",
        {
            "step": 7,
            "state": state,
            "error": error,
            "schema_skipped": state.schema_skipped,
            "timezones": _TIMEZONE_LIST,
            "locales": _SUPPORTED_LOCALES,
        },
    )


@router.post("/step/4", response_class=HTMLResponse)
async def step4_post(request: Request) -> HTMLResponse:
    """Save station identity and advance to step 8 (providers)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    state.station_name = str(form.get("station_name", "")).strip() or None
    state.latitude = _to_float(form.get("latitude"))
    state.longitude = _to_float(form.get("longitude"))

    # Altitude is always stored in meters internally.  The form includes an
    # altitude_unit field ("feet" or "meters") so we can convert as needed.
    alt_raw = _to_float(form.get("altitude_meters"))
    alt_unit = str(form.get("altitude_unit", "meters")).strip().lower()
    if alt_raw is not None and ("foot" in alt_unit or "feet" in alt_unit or "ft" in alt_unit):
        state.altitude_meters = alt_raw * 0.3048
        state.altitude_unit = "feet"
    else:
        state.altitude_meters = alt_raw
        state.altitude_unit = "meters"

    state.timezone = str(form.get("timezone", "")).strip() or None

    # Auto-lookup timezone if coordinates provided but timezone not set.
    if state.latitude and state.longitude and not state.timezone:
        state.timezone = lookup_timezone(state.latitude, state.longitude)

    # Default locale — validate against the ADR-021 allowed set; fall back to "en".
    submitted_locale = str(form.get("default_locale", "en")).strip()
    state.default_locale = submitted_locale if submitted_locale in _VALID_LOCALES else "en"

    # --- Station photo (optional) ---
    photo_url, photo_err = await _handle_branding_upload(form, "station_photo_file")
    if photo_err:
        return _render(
            request,
            "step_station.html",
            {
                "step": 7,
                "state": state,
                "error": photo_err,
                "schema_skipped": state.schema_skipped,
                "timezones": _TIMEZONE_LIST,
                "locales": _SUPPORTED_LOCALES,
            },
            status_code=422,
        )
    if photo_url is not None:
        state.station_photo_url = photo_url
    state.station_photo_alt = str(form.get("station_photo_alt", "")).strip()

    # About This Station content (FIX-008) — textarea present in the template.
    state.about_content = str(form.get("about_content", "")).strip()

    save_wizard_state(session_id, state)
    return await step6_get(request)


@router.post("/step/4/timezone", response_class=HTMLResponse)
async def step4_timezone(request: Request) -> HTMLResponse:
    """Return a pre-filled timezone input fragment for the given lat/lon.

    Called by HTMX when the user changes the latitude or longitude fields.
    Responds with a replacement <input> element so HTMX can swap it directly
    into the DOM.
    """
    _require_session(request)
    form = await request.form()
    lat = _to_float(form.get("latitude"))
    lon = _to_float(form.get("longitude"))

    tz: str = ""
    if lat is not None and lon is not None:
        detected = lookup_timezone(lat, lon)
        if detected:
            tz = detected

    assert _templates is not None
    return _templates.TemplateResponse(
        request=request,
        name="wizard/fragment_timezone_input.html",
        context={"timezone": tz, "timezones": _TIMEZONE_LIST},
    )


# ---------------------------------------------------------------------------
# Step 6: Provider Selection + Inline API Key Entry
# ---------------------------------------------------------------------------


def _render_step6(
    request: Request,
    state: Any,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the providers step. Shared by the GET handler and the POST error path."""
    return _render(
        request,
        "step_providers.html",
        {
            "step": 8,
            "state": state,
            "providers_by_domain": providers_by_domain(),
            "aqi_suggestion": _aqi_suggestion_from_state(state),
            "marine_same_host_url": MARINE_SAME_HOST_URL,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/step/6", response_class=HTMLResponse)
async def step6_get(request: Request) -> HTMLResponse:
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.providers:
        _merge_from_existing_config(state)

    return _render_step6(request, state)


@router.get("/step/6/key-fields/{domain}/{provider_id}", response_class=HTMLResponse)
async def step6_key_fields(request: Request, domain: str, provider_id: str) -> HTMLResponse:
    """Return inline key input fields for a provider that requires credentials."""
    session_id = _require_session(request)
    info = get_provider(provider_id)
    if not info or not info.auth_fields:
        assert _templates is not None
        return HTMLResponse(content="", status_code=200)

    state = get_wizard_state(session_id)

    return _render(
        request,
        "step_provider_key_fields.html",
        {"provider": info, "state": state},
    )


# ---------------------------------------------------------------------------
# AQI regional configuration — lat/lon-based suggestion helpers (ADR-059)
# ---------------------------------------------------------------------------

# Bounding boxes for regional AQI scale suggestions.
# These are coarse checks; the operator can always override.
# Each region is (lat_min, lat_max, lon_min, lon_max).
_AQI_REGIONS: list[tuple[str, float, float, float, float]] = [
    # North America: covers CONUS, Canada, Mexico, and Caribbean
    ("north_america", 15.0,  72.0, -170.0,  -50.0),
    # Europe: covers EU + UK + surrounding
    ("europe",        35.0,  72.0,  -25.0,   45.0),
    # China
    ("china",         18.0,  54.0,   73.0,  135.0),
    # India
    ("india",          6.0,  36.0,   68.0,   98.0),
]


def _suggest_aqi_region(lat: float, lon: float) -> str:
    """Return a region name for the given lat/lon using bounding box checks.

    Returns one of: "north_america", "europe", "china", "india", or "other".
    The first matching region wins (north_america is checked first so the US
    default applies across the Americas).
    """
    for region_name, lat_min, lat_max, lon_min, lon_max in _AQI_REGIONS:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return region_name
    return "other"


def _aqi_suggestion_from_state(state: "WizardState") -> dict[str, str]:
    """Build provider-specific AQI default suggestions from station coordinates.

    Returns a dict with keys "filter", "index", "scale" containing the
    suggested values for Aeris, Open-Meteo, and IQAir respectively.
    Returns an empty dict when coordinates are not yet known.
    """
    if state.latitude is None or state.longitude is None:
        return {}

    region = _suggest_aqi_region(state.latitude, state.longitude)

    _AERIS_FILTER: dict[str, str] = {
        "north_america": "airnow",
        "europe":        "eaqi",
        "china":         "china",
        "india":         "india",
        "other":         "airnow",  # US EPA is the broadest global fallback
    }
    _OPENMETEO_INDEX: dict[str, str] = {
        "north_america": "us_aqi",
        "europe":        "european_aqi",
        "china":         "us_aqi",   # Open-Meteo has no Chinese index
        "india":         "us_aqi",   # Open-Meteo has no Indian index
        "other":         "us_aqi",
    }
    _IQAIR_SCALE: dict[str, str] = {
        "north_america": "us",
        "europe":        "us",   # IQAir only offers us/cn
        "china":         "cn",
        "india":         "us",
        "other":         "us",
    }

    return {
        "filter": _AERIS_FILTER[region],
        "index":  _OPENMETEO_INDEX[region],
        "scale":  _IQAIR_SCALE[region],
    }


@router.get("/step/6/aqi-regional/{provider_id}", response_class=HTMLResponse)
async def step6_aqi_regional(request: Request, provider_id: str) -> HTMLResponse:
    """Return the AQI regional configuration fragment for the selected AQI provider.

    Called by HTMX when the operator selects an AQI provider on step 6.
    Also pre-rendered on page load via the step_providers.html template.
    Responds with an HTML fragment that is swapped into #aqi-regional-fields.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    # Validate provider_id is an AQI provider to prevent arbitrary template injection.
    _VALID_AQI_PROVIDERS = {"aeris_aqi", "openmeteo_aqi", "iqair"}
    if provider_id not in _VALID_AQI_PROVIDERS:
        assert _templates is not None
        return HTMLResponse(content="", status_code=200)

    aqi_suggestion = _aqi_suggestion_from_state(state)

    return _render(
        request,
        "step_aqi_regional_fields.html",
        {
            "provider_id": provider_id,
            "state": state,
            "aqi_suggestion": aqi_suggestion,
        },
    )


@router.post("/step/6/test-key/{provider_id}", response_class=HTMLResponse)
async def step6_test_key(request: Request, provider_id: str) -> HTMLResponse:
    """Test one provider's API key; return a result fragment."""
    session_id = _require_session(request)
    form = await request.form()
    info = get_provider(provider_id)

    if not info:
        return _render(
            request,
            "step_provider_test_result.html",
            {
                "test_result": {"success": False, "error": _("This provider is not available. Please go back and choose a different provider.")},
                "test_provider_id": provider_id,
                "test_provider_name": _("Unknown provider"),
            },
        )

    credentials: dict[str, str] = {}
    for field_name in info.auth_fields:
        credentials[field_name] = str(form.get(f"{provider_id}_{field_name}", "")).strip()

    # Pass station coordinates from wizard state so location-dependent providers
    # (IQAir, OpenWeatherMap AQI) use a real location instead of 0,0 (Gulf of Guinea).
    state = get_wizard_state(session_id)
    lat = state.latitude if state.latitude is not None else 0
    lon = state.longitude if state.longitude is not None else 0

    result = test_provider(info, credentials, latitude=lat, longitude=lon)
    return _render(
        request,
        "step_provider_test_result.html",
        {
            "test_result": result,
            "test_provider_id": provider_id,
            "test_provider_name": info.display_name,
        },
    )


@router.post("/step/6", response_class=HTMLResponse)
async def step6_post(request: Request) -> HTMLResponse:
    """Save provider selections and inline API keys, advance to step 7."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    providers: dict[str, str] = {}
    for domain in ("forecast", "alerts", "aqi", "earthquakes", "radar"):
        provider_id = str(form.get(f"provider_{domain}", "")).strip()
        if provider_id:
            providers[domain] = provider_id
    state.providers = providers

    # Collect inline API keys submitted alongside the provider selection.
    # Form fields are namespaced "{provider_id}_{field_name}".
    # Secret fields also send a "{provider_id}_{field_name}_unchanged" sentinel
    # when the user has not re-typed the key on re-render — keep the existing
    # stored value in that case rather than overwriting with an empty string.
    existing_api_keys = state.api_keys or {}
    api_keys: dict[str, dict[str, str]] = {}
    for provider_id in state.providers.values():
        info = get_provider(provider_id)
        if info and info.auth_fields:
            creds: dict[str, str] = {}
            for field_name in info.auth_fields:
                is_secret = "secret" in field_name or "password" in field_name or "key" in field_name
                submitted_value = str(form.get(f"{provider_id}_{field_name}", "")).strip()
                if submitted_value:
                    creds[field_name] = submitted_value
                elif is_secret:
                    # Check the sentinel: if unchanged=1 and field is empty, keep existing.
                    sentinel_name = f"{provider_id}_{field_name}_unchanged"
                    key_unchanged = str(form.get(sentinel_name, "0")).strip() == "1"
                    if key_unchanged:
                        existing_value = existing_api_keys.get(provider_id, {}).get(field_name, "")
                        if existing_value:
                            creds[field_name] = existing_value
                    # else: flag cleared and field empty — user intentionally cleared the key.
            if creds:
                api_keys[provider_id] = creds
    state.api_keys = api_keys

    # Collect AQI regional configuration (ADR-059).
    # Fields are present only when the operator has selected an AQI provider that
    # supports regional configuration.  Missing fields keep their state defaults.
    _VALID_AERIS_FILTERS = {"airnow", "china", "india", "eaqi", "caqi", "uk", "de", "cai"}
    _VALID_OPENMETEO_INDEXES = {"us_aqi", "european_aqi"}
    _VALID_IQAIR_SCALES = {"us", "cn"}

    submitted_aeris_filter = str(form.get("aeris_aqi_filter", "")).strip()
    if submitted_aeris_filter in _VALID_AERIS_FILTERS:
        state.aeris_aqi_filter = submitted_aeris_filter

    submitted_openmeteo_index = str(form.get("openmeteo_aqi_index", "")).strip()
    if submitted_openmeteo_index in _VALID_OPENMETEO_INDEXES:
        state.openmeteo_aqi_index = submitted_openmeteo_index

    submitted_iqair_scale = str(form.get("iqair_aqi_scale", "")).strip()
    if submitted_iqair_scale in _VALID_IQAIR_SCALES:
        state.iqair_aqi_scale = submitted_iqair_scale

    # Aeris forecast model selection (ADR-063)
    submitted_forecast_model = str(form.get("aeris_forecast_model", "")).strip()
    if submitted_forecast_model in ("standard", "xcast"):
        state.aeris_forecast_model = submitted_forecast_model

    # LibreWxR endpoint and bounds (radar domain)
    submitted_endpoint_mode = str(form.get("librewxr_endpoint_mode", "")).strip()
    if submitted_endpoint_mode == "selfhosted":
        submitted_url = str(form.get("librewxr_endpoint_url", "")).strip()
        if submitted_url:
            state.librewxr_endpoint = submitted_url
        else:
            state.librewxr_endpoint = "https://api.librewxr.net"
    else:
        state.librewxr_endpoint = "https://api.librewxr.net"

    submitted_bounds = str(form.get("librewxr_bounds", "")).strip()
    state.librewxr_bounds = submitted_bounds

    # Marine companion service connection (T7.2).
    # "Same host" wins over whatever is in the URL box: the checkbox makes the
    # field read-only client-side, so a submission carrying both a checked box
    # and a different URL means the read-only guard was bypassed, and the
    # explicit checkbox is the clearer statement of intent.
    if str(form.get("marine_same_host", "")).strip() == "1":
        state.marine_service_url = MARINE_SAME_HOST_URL
    else:
        state.marine_service_url = str(form.get("marine_service_url", "")).strip()

    submitted_marine_secret = str(form.get("marine_service_secret", "")).strip()
    if submitted_marine_secret:
        state.marine_service_secret = submitted_marine_secret
    elif str(form.get("marine_service_secret_unchanged", "0")).strip() == "1":
        pass  # keep existing secret in state
    else:
        state.marine_service_secret = ""

    # Unchecked checkboxes are not submitted, so absence means "do not verify".
    # The fieldset is always rendered on this step, so absence is unambiguous.
    state.marine_verify_tls = str(form.get("marine_verify_tls", "")).strip() == "1"

    # T7.4: marine features cannot work without somewhere to reach the marine
    # service.  Re-render with the error rather than saving a configuration
    # that is guaranteed to produce no marine data.
    if state.marine_enabled and not state.marine_service_url:
        return _render_step6(
            request,
            state,
            error=_(
                "Marine features are enabled, so a marine service URL is required. "
                "Enter the address of your marine service, or turn marine features "
                "off on the marine step."
            ),
            status_code=422,
        )

    save_wizard_state(session_id, state)
    return await step7_get(request)


# ---------------------------------------------------------------------------
# Step 7: Webcam Configuration
# ---------------------------------------------------------------------------


@router.get("/step/7", response_class=HTMLResponse)
async def step7_get(request: Request) -> HTMLResponse:
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.webcam_enabled and state.webcam_image_url == "/webcam/weather_cam.jpg":
        _merge_from_existing_config(state)
    from weewx_clearskies_config.registry import registry
    fields = registry.get_fields_for_section("webcam")
    values = {
        "enabled": state.webcam_enabled,
        "image_url": state.webcam_image_url,
        "video_url": state.webcam_video_url,
        "refresh_interval": state.webcam_refresh_interval,
    }
    return _render(request, "step_webcam.html", {"step": 9, "state": state, "fields": fields, "values": values, "error": None})


@router.post("/step/7", response_class=HTMLResponse)
async def step7_post(request: Request) -> HTMLResponse:
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)
    from weewx_clearskies_config.registry import registry, validate_form_against_fields, extract_field_values
    fields = registry.get_fields_for_section("webcam")
    form_data = dict(form)
    errors = validate_form_against_fields(form_data, fields)
    if errors:
        values = {
            "enabled": form_data.get("enabled") == "on",
            "image_url": str(form_data.get("image_url", "/webcam/weather_cam.jpg")).strip(),
            "video_url": str(form_data.get("video_url", "/webcam/weewx_timelapse.mp4")).strip(),
            "refresh_interval": form_data.get("refresh_interval", "60"),
        }
        return _render(
            request,
            "step_webcam.html",
            {"step": 9, "state": state, "fields": fields, "values": values, "error": " ".join(errors)},
            status_code=422,
        )
    extracted = extract_field_values(form_data, fields)
    state.webcam_enabled = bool(extracted.get("enabled", False))
    state.webcam_image_url = str(extracted.get("image_url", "/webcam/weather_cam.jpg")).strip()
    state.webcam_video_url = str(extracted.get("video_url", "/webcam/weewx_timelapse.mp4")).strip()
    try:
        state.webcam_refresh_interval = int(extracted.get("refresh_interval", 60))
    except (ValueError, TypeError):
        state.webcam_refresh_interval = 60
    save_wizard_state(session_id, state)
    return await step8_appearance_get(request)


# ---------------------------------------------------------------------------
# Step 8: Appearance (branding + seismic page settings)
# ---------------------------------------------------------------------------

# Allowed file types for branding uploads.
# Keys are the form field names; values are (allowed_extensions, allowed_mimes, max_bytes).
_BRANDING_UPLOAD_RULES: dict[str, tuple[frozenset[str], frozenset[str], int]] = {
    "logo_light_file": (
        frozenset({".png", ".svg"}),
        frozenset({"image/png", "image/svg+xml"}),
        500 * 1024,  # 500 KB
    ),
    "logo_dark_file": (
        frozenset({".png", ".svg"}),
        frozenset({"image/png", "image/svg+xml"}),
        500 * 1024,
    ),
    "favicon_file": (
        frozenset({".ico", ".png"}),
        frozenset({"image/x-icon", "image/vnd.microsoft.icon", "image/png"}),
        100 * 1024,  # 100 KB
    ),
    "station_photo_file": (
        frozenset({".jpg", ".jpeg", ".png", ".webp"}),
        frozenset({"image/jpeg", "image/png", "image/webp"}),
        2 * 1024 * 1024,  # 2 MB
    ),
    "background_file": (
        frozenset({".jpg", ".jpeg", ".png", ".webp"}),
        frozenset({"image/jpeg", "image/png", "image/webp"}),
        5 * 1024 * 1024,  # 5 MB
    ),
}

# Sanitise a filename: keep only alphanumerics, hyphens, underscores, dots.
# Prevents path traversal and shell-special-character issues.
_SAFE_FILENAME_RE = re.compile(r"[^\w.\-]")


def _sanitise_filename(name: str) -> str:
    """Return a filesystem-safe version of *name*.

    Strips directory components, replaces unsafe characters with underscores,
    and ensures the result is non-empty.
    """
    base = Path(name).name  # strip any directory component
    safe = _SAFE_FILENAME_RE.sub("_", base)
    return safe or "upload"


async def _handle_branding_upload(
    form: Any,
    field_name: str,
    subdirectory: str | None = None,
) -> tuple[str | None, str | None]:
    """Process one branding file upload field.

    Returns ``(url, error)`` where *url* is the served URL to store in state
    (``/wizard/branding/<filename>``, or ``/wizard/branding/<subdirectory>/
    <filename>`` when *subdirectory* is given), or None if no file was
    uploaded. *error* is a human-readable message or None.

    The caller uses None to mean "keep the URL text-field value instead."
    """
    if _config_dir is None:
        return None, _("Configuration directory not set — cannot save uploaded files.")

    allowed_exts, _allowed_mimes, max_bytes = _BRANDING_UPLOAD_RULES[field_name]
    upload = form.get(field_name)

    # Starlette represents a non-selected file input as either None or a
    # UploadFile with an empty .filename.  Both mean "no file chosen."
    if upload is None or not hasattr(upload, "filename") or not upload.filename:
        return None, None

    raw_filename: str = str(upload.filename)
    suffix = Path(raw_filename).suffix.lower()
    if suffix not in allowed_exts:
        return None, _(
            'Unsupported file type "{suffix}" for {field}. '
            "Allowed: {allowed}."
        ).format(suffix=suffix, field=field_name.replace('_file', ''), allowed=', '.join(sorted(allowed_exts)))

    data: bytes = await upload.read()
    if len(data) > max_bytes:
        max_kb = max_bytes // 1024
        return None, _(
            "{field}: file is {size} KB, exceeds the {limit} KB limit."
        ).format(
            field=field_name.replace('_file', '').replace('_', ' ').title(),
            size=len(data) // 1024,
            limit=max_kb,
        )

    safe_name = _sanitise_filename(raw_filename)
    dest_dir = _config_dir / "branding"
    url_prefix = "/wizard/branding"
    if subdirectory:
        dest_dir = dest_dir / subdirectory
        url_prefix = f"{url_prefix}/{subdirectory}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    dest.write_bytes(data)
    logger.info("Saved branding upload %s → %s", field_name, dest)

    return f"{url_prefix}/{safe_name}", None


@router.get("/step/8", response_class=HTMLResponse)
async def step8_appearance_get(request: Request) -> HTMLResponse:
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.site_title and not state.accent:
        _merge_from_existing_config(state)
    from weewx_clearskies_config.registry import registry
    branding_fields = registry.get_fields_for_section("branding")
    fields_by_key = {f.config_key: f for f in branding_fields}
    values = {
        "site_title": state.site_title,
        "copyright_entity": state.copyright_entity,
        "logo_light_url": state.logo_light_url,
        "logo_dark_url": state.logo_dark_url,
        "logo_alt": state.logo_alt,
        "favicon_url": state.favicon_url,
        "accent": state.accent or "blue",
        "default_theme_mode": state.default_theme_mode or "auto-os",
    }
    return _render(request, "step_appearance.html", {
        "step": 10,
        "state": state,
        "fields_by_key": fields_by_key,
        "values": values,
        "error": None,
    })


@router.post("/step/8", response_class=HTMLResponse)
async def step8_appearance_post(request: Request) -> HTMLResponse:
    """Save branding and social settings; advance to step 11 (privacy)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    # --- Branding ---
    state.site_title = str(form.get("site_title", "")).strip()
    state.copyright_entity = str(form.get("copyright_entity", "")).strip()

    # For each image field: if a file was uploaded use its saved URL,
    # otherwise fall back to the URL text input (may be empty or a prior value).
    errors: list[str] = []

    logo_light_url, err = await _handle_branding_upload(form, "logo_light_file")
    if err:
        errors.append(err)
    else:
        state.logo_light_url = logo_light_url or str(form.get("logo_light_url", "")).strip()

    logo_dark_url, err = await _handle_branding_upload(form, "logo_dark_file")
    if err:
        errors.append(err)
    else:
        state.logo_dark_url = logo_dark_url or str(form.get("logo_dark_url", "")).strip()

    favicon_url, err = await _handle_branding_upload(form, "favicon_file")
    if err:
        errors.append(err)
    else:
        state.favicon_url = favicon_url or str(form.get("favicon_url", "")).strip()

    # Custom background image (optional) — replaces the dashboard's built-in
    # day/night scene backgrounds when set.
    background_url, err = await _handle_branding_upload(form, "background_file", subdirectory="backgrounds")
    if err:
        errors.append(err)
    elif background_url:
        state.custom_background_url = background_url
    elif str(form.get("remove_background", "")).strip():
        state.custom_background_url = ""

    if errors:
        from weewx_clearskies_config.registry import registry as _reg
        _bf = _reg.get_fields_for_section("branding")
        _fbk = {f.config_key: f for f in _bf}
        _vals = {
            "site_title": state.site_title,
            "copyright_entity": state.copyright_entity,
            "logo_light_url": state.logo_light_url,
            "logo_dark_url": state.logo_dark_url,
            "logo_alt": state.logo_alt,
            "favicon_url": state.favicon_url,
            "accent": state.accent or "blue",
            "default_theme_mode": state.default_theme_mode or "auto-os",
        }
        return _render(
            request,
            "step_appearance.html",
            {"step": 10, "state": state, "fields_by_key": _fbk, "values": _vals, "error": " ".join(errors)},
            status_code=422,
        )

    # Logo alt text (WCAG requirement)
    state.logo_alt = str(form.get("logo_alt", "")).strip()

    # Accent color and theme mode — validated against registry field options.
    from weewx_clearskies_config.registry import registry
    branding_fields = registry.get_fields_for_section("branding")
    _branding_by_key = {f.config_key: f for f in branding_fields}
    _accent_field = _branding_by_key.get("accent")
    submitted_accent = str(form.get("accent", "")).strip()
    if _accent_field:
        _valid_accents = {opt.value for opt in _accent_field.options}
        state.accent = submitted_accent if submitted_accent in _valid_accents else ""
    else:
        state.accent = submitted_accent

    _theme_field = _branding_by_key.get("default_theme_mode")
    submitted_theme_mode = str(form.get("default_theme_mode", "")).strip()
    if _theme_field:
        _valid_theme_modes = {opt.value for opt in _theme_field.options}
        state.default_theme_mode = submitted_theme_mode if submitted_theme_mode in _valid_theme_modes else ""
    else:
        state.default_theme_mode = submitted_theme_mode

    save_wizard_state(session_id, state)
    return await step_privacy_legal_get(request)


# ---------------------------------------------------------------------------
# Step 12: Privacy, Legal & Analytics
# ---------------------------------------------------------------------------


def _format_txt_to_markdown(text: str, doc_type: str = "terms") -> str:
    """Convert a plain text legal document to basic Markdown.

    Adds heading structure so the document renders properly on the Legal page
    with correct typography. Lines that look like section headers (all caps,
    or numbered sections like "1. ACCEPTANCE") get ## heading markup.
    Paragraphs are separated by blank lines. Existing markdown is left as-is.

    Rules:
    - First non-empty line becomes ``# {line}`` (h1 title).
    - Lines that are ALL CAPS and longer than 3 characters become ``## {line}``.
    - Lines starting with a number followed by a period and space
      (e.g. "1. ACCEPTANCE") become ``## {line}``.
    - All other lines are left as regular paragraphs.
    - Existing blank lines are preserved as paragraph breaks.
    - Trailing whitespace is stripped from each line.

    Args:
        text: Raw plain-text content of the legal document.
        doc_type: Informational label ("terms" or "privacy") — not used for
                  formatting, reserved for future caller-provided context.

    Returns:
        A string containing valid Markdown derived from *text*.
    """
    import re as _re

    _NUMBERED_SECTION_RE = _re.compile(r"^\d+\.\s+\S")

    output_lines: list[str] = []
    first_non_empty_seen = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # First non-empty line → h1 title.
        if not first_non_empty_seen:
            if not line:
                output_lines.append("")
                continue
            first_non_empty_seen = True
            output_lines.append(f"# {line}")
            continue

        # Blank line — preserve as paragraph separator.
        if not line:
            output_lines.append("")
            continue

        # ALL CAPS lines longer than 3 chars → h2 section header.
        if line == line.upper() and len(line.strip()) > 3 and not line.strip().startswith("#"):
            output_lines.append(f"## {line}")
            continue

        # Numbered section header (e.g. "1. ACCEPTANCE") → h2.
        if _NUMBERED_SECTION_RE.match(line):
            output_lines.append(f"## {line}")
            continue

        # Ordinary line — pass through.
        output_lines.append(line)

    return "\n".join(output_lines)


@router.get("/privacy", response_class=HTMLResponse)
async def step_privacy_legal_get(request: Request) -> HTMLResponse:
    """Step 11: Privacy, Legal & Analytics — render the form."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.google_analytics_id and not state.privacy_regions:
        _merge_from_existing_config(state)
    from weewx_clearskies_config.registry import registry
    analytics_fields = registry.get_fields_for_section("analytics")
    analytics_fields_by_key = {f.config_key: f for f in analytics_fields}
    values = {
        "google_analytics_id": state.google_analytics_id,
        "privacy_regions": state.privacy_regions or "global",
    }
    return _render(request, "step_privacy_legal.html", {
        "step": 11,
        "state": state,
        "analytics_fields_by_key": analytics_fields_by_key,
        "values": values,
        "error": None,
    })


@router.post("/privacy", response_class=HTMLResponse)
async def step_privacy_legal_post(request: Request) -> HTMLResponse:
    """Save analytics and privacy/legal settings; advance to step 12 (features)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    # --- Analytics & Privacy Regions (via registry fields) ---
    from weewx_clearskies_config.registry import registry, validate_form_against_fields, extract_field_values
    analytics_fields = registry.get_fields_for_section("analytics")
    form_data = dict(form)
    reg_errors = validate_form_against_fields(form_data, analytics_fields)
    if not reg_errors:
        extracted = extract_field_values(form_data, analytics_fields)
        state.google_analytics_id = str(extracted.get("google_analytics_id", "")).strip()
        state.privacy_regions = str(extracted.get("privacy_regions", "global")).strip()

    # --- Legal Content Overrides (file upload) ---
    # For each text file field: if a file was uploaded, read its content as
    # UTF-8 text and convert .txt files to Markdown.  If no file was uploaded
    # the existing state value is left unchanged (operator can clear it only by
    # uploading a new file or removing it manually).
    _TEXT_UPLOAD_FIELDS: dict[str, tuple[str, str]] = {
        "custom_terms_file": ("custom_terms_md", "terms"),
        "custom_privacy_file": ("custom_privacy_md", "privacy"),
    }
    _ALLOWED_TEXT_EXTS = frozenset({".md", ".txt"})
    _MAX_TEXT_BYTES = 100 * 1024  # 100 KB

    text_errors: list[str] = []
    for field_name, (state_attr, doc_type) in _TEXT_UPLOAD_FIELDS.items():
        upload = form.get(field_name)
        logger.info("Legal upload field %s: type=%s, has_filename=%s, filename=%r",
                     field_name, type(upload).__name__,
                     hasattr(upload, "filename"), getattr(upload, "filename", None))
        if upload is None or not hasattr(upload, "filename") or not upload.filename:
            # No file chosen — keep existing state value unchanged.
            continue
        raw_filename = str(upload.filename)
        suffix = Path(raw_filename).suffix.lower()
        if suffix not in _ALLOWED_TEXT_EXTS:
            text_errors.append(
                _('Unsupported file type "{suffix}" for {field}. Allowed: .md, .txt.').format(
                    suffix=suffix, field=field_name.replace('_file', '')
                )
            )
            continue
        data: bytes = await upload.read()
        if len(data) > _MAX_TEXT_BYTES:
            text_errors.append(
                _("{field}: file is {size} KB, exceeds the 100 KB limit.").format(
                    field=field_name.replace('_file', '').replace('_', ' ').title(),
                    size=len(data) // 1024,
                )
            )
            continue
        content = data.decode("utf-8", errors="replace")
        if suffix == ".txt":
            content = _format_txt_to_markdown(content, doc_type=doc_type)
        setattr(state, state_attr, content)

    all_errors = reg_errors + text_errors
    if all_errors:
        analytics_fields_by_key = {f.config_key: f for f in analytics_fields}
        values = {
            "google_analytics_id": state.google_analytics_id,
            "privacy_regions": state.privacy_regions or "global",
        }
        return _render(
            request,
            "step_privacy_legal.html",
            {
                "step": 11,
                "state": state,
                "analytics_fields_by_key": analytics_fields_by_key,
                "values": values,
                "error": " ".join(all_errors),
            },
            status_code=422,
        )

    save_wizard_state(session_id, state)
    return await step_feature_settings_get(request)


# ---------------------------------------------------------------------------
# Step 13: Feature Settings (seismic page)
# ---------------------------------------------------------------------------


@router.get("/features", response_class=HTMLResponse)
async def step_feature_settings_get(request: Request) -> HTMLResponse:
    """Step 12: Feature Settings — render the seismic page settings form."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if state.earthquake_radius_km == 100.0 and state.earthquake_default_days == 7:
        _merge_from_existing_config(state)
    from weewx_clearskies_config.registry import registry
    fields = registry.get_fields_for_section("earthquakes")
    values = {
        "radius_km": state.earthquake_radius_km,
        "min_magnitude": state.earthquake_min_magnitude,
        "default_days": str(state.earthquake_default_days),
    }
    return _render(request, "step_feature_settings.html", {"step": 12, "state": state, "fields": fields, "values": values, "error": None})


@router.post("/features", response_class=HTMLResponse)
async def step_feature_settings_post(request: Request) -> HTMLResponse:
    """Save feature settings; advance to step 13 (marine)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)
    from weewx_clearskies_config.registry import registry, validate_form_against_fields, extract_field_values
    fields = registry.get_fields_for_section("earthquakes")
    form_data = dict(form)
    errors = validate_form_against_fields(form_data, fields)
    if errors:
        values = {
            "radius_km": form_data.get("radius_km", "100"),
            "min_magnitude": form_data.get("min_magnitude", "2.0"),
            "default_days": form_data.get("default_days", "7"),
        }
        return _render(
            request,
            "step_feature_settings.html",
            {"step": 12, "state": state, "fields": fields, "values": values, "error": " ".join(errors)},
            status_code=422,
        )
    extracted = extract_field_values(form_data, fields)
    try:
        state.earthquake_radius_km = max(1.0, float(extracted.get("radius_km", 100)))
    except (ValueError, TypeError):
        state.earthquake_radius_km = 100.0
    try:
        state.earthquake_min_magnitude = max(0.0, float(extracted.get("min_magnitude", 2.0)))
    except (ValueError, TypeError):
        state.earthquake_min_magnitude = 2.0
    try:
        days_val = int(extracted.get("default_days", 7))
        state.earthquake_default_days = days_val if days_val in (1, 7, 14, 30) else 7
    except (ValueError, TypeError):
        state.earthquake_default_days = 7

    marine_radius = _parse_int(str(form_data.get("marine_alert_radius_miles", "0")), default=0)
    state.marine_alert_radius_miles = max(0, min(100, marine_radius))

    save_wizard_state(session_id, state)
    return await step_marine_get(request)


# ---------------------------------------------------------------------------
# Step 13: Marine Location Configuration (T6.1)
# ---------------------------------------------------------------------------

_MARINE_VALID_ACTIVITIES = frozenset({"marine", "surf", "fishing", "beach_safety"})
_MARINE_VALID_BOTTOM_TYPES = frozenset({"sand", "rock", "coral_reef", "mixed"})
_MARINE_VALID_TOPO_FEATURES = frozenset({"point_break", "bay_break", "headland", "straight_beach"})
_MARINE_VALID_EXPOSURE = frozenset({"N", "NE", "E", "SE", "S", "SW", "W", "NW"})
_MARINE_VALID_TARGET_CATEGORIES = frozenset(
    {"saltwater_inshore", "bottom_fish", "freshwater_sport", "salmonids"}
)
_MARINE_LOC_INDEX_RE = re.compile(r"^loc_(\d+)_name$")
_MARINE_LAT_KEY_RE = re.compile(r"^loc_\d+_lat$")
_MARINE_LON_KEY_RE = re.compile(r"^loc_\d+_lon$")
_MARINE_TARGET_CATEGORY_KEY_RE = re.compile(r"^loc_(\d+)_fishing_target_categor(?:y|ies)$")
_MARINE_SPECIES_PREV_KEY_RE = re.compile(r"^loc_\d+_fishing_species_prev$")


def _marine_photos_sidecar_path(config_dir: Path) -> Path:
    return config_dir / "marine-photos.json"


def _read_marine_photos_sidecar(config_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Read marine-photos.json: local-only photo_url/photo_attribution per location slug.

    This data is never sent to the API — the API's marine location contract
    always returns ``photoUrl: null`` and the dashboard constructs photo URLs
    directly from the location id (see API-MANUAL "THE API IS NOT A FILE
    SERVER"; photos are served by Caddy from
    /etc/weewx-clearskies/marine-photos/, not by the API). Because
    state.marine_locations is otherwise reconstructed from the API's config
    on every wizard re-run (see the "Marine locations" restore block in
    wizard_apply's caller), this sidecar is the only durable home for photo
    metadata. Mirrors the branding.json / webcam.json local-file pattern.

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


def _apply_marine_photo_sidecar(state: WizardState, config_dir: Path | None) -> None:
    """Overlay locally-persisted photo_url/photo_attribution onto state.marine_locations.

    Called before rendering step_marine.html — the location dicts in
    ``state.marine_locations`` come from the API (or from this same request's
    form submission) and never carry photo fields on their own.
    """
    if config_dir is None or not state.marine_locations:
        return
    sidecar_locations = _read_marine_photos_sidecar(config_dir).get("locations", {})
    for slug, loc in state.marine_locations.items():
        meta = sidecar_locations.get(slug)
        if not isinstance(meta, dict):
            continue
        if meta.get("photo_url") and not loc.get("photo_url"):
            loc["photo_url"] = meta["photo_url"]
        if meta.get("photo_attribution") and not loc.get("photo_attribution"):
            loc["photo_attribution"] = meta["photo_attribution"]


def _slugify_location_name(name: str, existing: Any = ()) -> str:
    """Generate a URL/JSON-key-safe slug from an operator-entered location name.

    Lowercases, collapses non-alphanumeric runs to a single hyphen, and
    strips leading/trailing hyphens.  Falls back to "location" if the name
    has no alphanumeric characters.  Appends "-2", "-3", ... on collision
    with a slug already present in *existing*.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "location"
    existing_set = set(existing)
    slug = base
    n = 2
    while slug in existing_set:
        slug = f"{base}-{n}"
        n += 1
    return slug


@router.get("/marine", response_class=HTMLResponse)
async def step_marine_get(request: Request) -> HTMLResponse:
    """Step 13: Marine Locations — render marine feature configuration."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.marine_locations and _is_rerun_mode(state.api_address):
        # R1: _merge_from_api_current_config was previously reachable only
        # via the rerun-mode POST /wizard/step/1 branch (routes.py:1285), so
        # any re-entry to this step that didn't pass through that POST in
        # the current session rendered a blank form
        # (WIZARD-STUDY-AREA-RESET-INVESTIGATION-2026-08-03.md, "Path A").
        # This fallback must never raise -- a failed merge just leaves the
        # form blank as it does today.
        try:
            client = _get_api_client(state)
            _merge_from_api_current_config(client, state)
            logger.info(
                "step_marine_get: marine_locations empty; fell back to "
                "API current-config merge (rerun mode)"
            )
            save_wizard_state(session_id, state)
        except ValueError:
            logger.warning(
                "step_marine_get: rerun-mode fallback merge skipped — "
                "API client not available"
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "step_marine_get: rerun-mode fallback merge failed", exc_info=True
            )
    _apply_marine_photo_sidecar(state, _config_dir)
    return _render(
        request,
        "step_marine.html",
        {"step": 13, "state": state, "error": None},
    )


@router.post("/marine", response_class=HTMLResponse)
async def step_marine_post(request: Request) -> HTMLResponse:
    """Save marine location configuration and advance to step 14 (TLS)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    marine_enabled = str(form.get("marine_enabled", "")).strip() == "1"
    state.marine_enabled = marine_enabled

    if not marine_enabled:
        state.marine_locations = {}
        save_wizard_state(session_id, state)
        return await step_tls_get(request)

    # Discover which location indexes were submitted (one per repeatable card).
    indices: list[str] = sorted(
        {m.group(1) for key in form.keys() if (m := _MARINE_LOC_INDEX_RE.match(key))},
        key=int,
    )

    new_locations: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for idx in indices:
        name = str(form.get(f"loc_{idx}_name", "")).strip()
        if not name:
            continue  # Blank/incomplete row — skip silently (e.g. added then not filled in).

        lat = _to_float(form.get(f"loc_{idx}_lat"))
        lon = _to_float(form.get(f"loc_{idx}_lon"))
        if lat is None or lon is None:
            errors.append(_('Location "{name}" is missing latitude/longitude.').format(name=name))
            continue

        activities = [
            v for v in form.getlist(f"loc_{idx}_activities") if v in _MARINE_VALID_ACTIVITIES
        ]

        loc: dict[str, Any] = {
            "name": name,
            "lat": lat,
            "lon": lon,
            "activities": activities,
        }

        # Discovered station IDs are carried forward via hidden fields populated
        # by the "Discover Nearby Stations" HTMX call (see marine_station_results.html).
        ndbc_ids = str(form.get(f"loc_{idx}_ndbc_station_ids", "")).strip()
        if ndbc_ids:
            loc["ndbc_station_ids"] = [s.strip() for s in ndbc_ids.split(",") if s.strip()]
        coops_ids = str(form.get(f"loc_{idx}_coops_station_ids", "")).strip()
        if coops_ids:
            loc["coops_station_ids"] = [s.strip() for s in coops_ids.split(",") if s.strip()]
        marine_zone = str(form.get(f"loc_{idx}_nws_marine_zone_id", "")).strip()
        if marine_zone:
            loc["nws_marine_zone_id"] = marine_zone

        if "surf" in activities:
            seg_start_lat = _to_float(form.get(f"loc_{idx}_surf_segment_start_lat"))
            seg_start_lon = _to_float(form.get(f"loc_{idx}_surf_segment_start_lon"))
            seg_end_lat   = _to_float(form.get(f"loc_{idx}_surf_segment_end_lat"))
            seg_end_lon   = _to_float(form.get(f"loc_{idx}_surf_segment_end_lon"))
            bottom_type = str(form.get(f"loc_{idx}_surf_bottom_type", "")).strip()
            topo = str(form.get(f"loc_{idx}_surf_topographic_feature", "")).strip()
            exposure = [
                v for v in form.getlist(f"loc_{idx}_surf_exposure") if v in _MARINE_VALID_EXPOSURE
            ]
            surf_cfg: dict[str, Any] = {}
            if all(x is not None for x in [seg_start_lat, seg_start_lon, seg_end_lat, seg_end_lon]):
                surf_cfg["segment_start_lat"] = seg_start_lat
                surf_cfg["segment_start_lon"] = seg_start_lon
                surf_cfg["segment_end_lat"]   = seg_end_lat
                surf_cfg["segment_end_lon"]   = seg_end_lon
            if bottom_type in _MARINE_VALID_BOTTOM_TYPES:
                surf_cfg["bottom_type"] = bottom_type
            if topo in _MARINE_VALID_TOPO_FEATURES:
                surf_cfg["topographic_feature"] = topo
            if exposure:
                surf_cfg["directional_exposure"] = exposure

            structures: list[dict[str, Any]] = []
            si = 0
            while True:
                s_type = str(form.get(f"loc_{idx}_structure_{si}_type", "")).strip()
                if not s_type:
                    break
                s_material = str(form.get(f"loc_{idx}_structure_{si}_material", "")).strip()
                s_length = _to_float(form.get(f"loc_{idx}_structure_{si}_length_m"))
                s_bearing = _to_float(form.get(f"loc_{idx}_structure_{si}_bearing_degrees"))
                s_distance = _to_float(form.get(f"loc_{idx}_structure_{si}_distance_m"))
                if s_type and s_material and s_length and s_bearing is not None and s_distance:
                    structure: dict[str, Any] = {
                        "type": s_type,
                        "material": s_material,
                        "length_m": s_length,
                        "bearing_degrees": s_bearing,
                        "distance_m": s_distance,
                    }
                    # Way geometry carried from the discovery-card hidden input
                    # or the map-draw tool, already converted lon/lat once
                    # upstream (StructureConfig.coordinates contract). Absent
                    # geometry stays absent — never fabricated here.
                    s_coords_raw = str(
                        form.get(f"loc_{idx}_structure_{si}_coordinates", "")
                    ).strip()
                    if s_coords_raw:
                        try:
                            s_coords = json.loads(s_coords_raw)
                        except (ValueError, TypeError):
                            s_coords = None
                        if isinstance(s_coords, list) and s_coords:
                            structure["coordinates"] = s_coords
                    structures.append(structure)
                si += 1
            if structures:
                surf_cfg["structures"] = structures

            # SurfBeat toggle and cadence (T5.1).
            surfbeat_checked = form.get(f"loc_{idx}_surfbeat_enabled")
            surf_cfg["surfbeat_enabled"] = surfbeat_checked is not None
            raw_cadence = _parse_int(
                str(form.get(f"loc_{idx}_surfbeat_cadence_hours", "3")),
                default=3,
            )
            surf_cfg["surfbeat_cadence_hours"] = raw_cadence if 1 <= raw_cadence <= 6 else 3

            loc["surf"] = surf_cfg

        if "fishing" in activities:
            target_categories = [
                c.strip() for c in form.getlist(f"loc_{idx}_fishing_target_categories")
                if c.strip() in _MARINE_VALID_TARGET_CATEGORIES
            ]
            if not target_categories:
                old_single = str(form.get(f"loc_{idx}_fishing_target_category", "")).strip()
                if old_single in _MARINE_VALID_TARGET_CATEGORIES:
                    target_categories = [old_single]
            species_checked = [
                s.strip() for s in form.getlist(f"loc_{idx}_fishing_species") if s.strip()
            ]
            fishing_cfg: dict[str, Any] = {}
            if target_categories:
                fishing_cfg["target_categories"] = target_categories
            if species_checked:
                fishing_cfg["species"] = species_checked
            loc["fishing"] = fishing_cfg

        if "beach_safety" in activities:
            labels = form.getlist(f"loc_{idx}_beach_safety_link_label")
            urls = form.getlist(f"loc_{idx}_beach_safety_link_url")
            links = [
                {"label": str(lbl).strip(), "url": str(u).strip()}
                for lbl, u in zip(labels, urls)
                if str(lbl).strip() and str(u).strip()
            ]
            loc["beach_safety"] = {"external_links": links}

        slug = _slugify_location_name(name, existing=new_locations.keys())

        # Photo metadata (local-only — never sent to the API; see
        # _read_marine_photos_sidecar). Start from whatever the hidden
        # loc_{idx}_photo_url field carried forward from the previous render
        # of this form (e.g. re-submit after a validation error on another
        # card); a fresh upload below overrides it.
        photo_url = str(form.get(f"loc_{idx}_photo_url", "")).strip()
        photo_attribution = str(form.get(f"loc_{idx}_photo_attribution", "")).strip()

        # Photo upload — save to /etc/weewx-clearskies/marine-photos/{slug}.{ext}
        photo_upload = form.get(f"loc_{idx}_photo")
        if photo_upload and hasattr(photo_upload, "filename") and photo_upload.filename:
            suffix = Path(str(photo_upload.filename)).suffix.lower()
            if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                photo_data: bytes = await photo_upload.read()
                if len(photo_data) <= 200 * 1024:
                    photos_dir = Path("/etc/weewx-clearskies/marine-photos")
                    photos_dir.mkdir(parents=True, exist_ok=True)
                    for old in photos_dir.glob(f"{slug}.*"):
                        old.unlink(missing_ok=True)
                    (photos_dir / f"{slug}{suffix}").write_bytes(photo_data)
                    logger.info("Saved marine photo for %s (%d bytes)", slug, len(photo_data))
                    photo_url = f"/marine-photos/{slug}{suffix}"
                else:
                    errors.append(
                        _('Photo for "{name}" exceeds 200 KB limit.').format(name=name)
                    )

        if photo_url:
            loc["photo_url"] = photo_url
        if photo_attribution:
            loc["photo_attribution"] = photo_attribution

        new_locations[slug] = loc

    if errors:
        _apply_marine_photo_sidecar(state, _config_dir)
        return _render(
            request,
            "step_marine.html",
            {"step": 13, "state": state, "error": " ".join(errors)},
            status_code=422,
        )

    state.marine_locations = new_locations

    # Persist photo_url/photo_attribution to the local-only sidecar (never
    # sent to the API — see _read_marine_photos_sidecar). Rebuilt from
    # scratch each save so locations removed from the form are pruned here too.
    if _config_dir is not None:
        photo_sidecar = {
            slug: {
                k: v for k, v in (
                    ("photo_url", loc_data.get("photo_url", "")),
                    ("photo_attribution", loc_data.get("photo_attribution", "")),
                ) if v
            }
            for slug, loc_data in new_locations.items()
            if loc_data.get("photo_url") or loc_data.get("photo_attribution")
        }
        _write_marine_photos_sidecar(_config_dir, photo_sidecar)

    ttl_hours = _parse_int(str(form.get("marine_forecast_ttl_hours", "3")), default=3)
    state.marine_forecast_ttl_hours = ttl_hours if ttl_hours in (1, 3, 6) else 3

    ttl_minutes = _parse_int(str(form.get("marine_observation_ttl_minutes", "30")), default=30)
    state.marine_observation_ttl_minutes = ttl_minutes if ttl_minutes in (15, 30, 60) else 30

    # T7.4: the same rule the providers step enforces, applied here too.
    # Marine is enabled on this step but the marine service URL is entered on
    # the providers step, which comes earlier — so on a straight first pass the
    # providers-step check cannot fire.  Without this, an operator can enable
    # marine features, never go back, and get no marine data with nothing
    # saying why.  Checked after the location parsing above so the re-render
    # still shows the locations just entered.
    if not state.marine_service_url:
        _apply_marine_photo_sidecar(state, _config_dir)
        return _render(
            request,
            "step_marine.html",
            {
                "step": 13,
                "state": state,
                "error": _(
                    "Marine features need a marine service to talk to, and no marine "
                    "service URL is configured. Go back to the Providers step and enter "
                    "the address of your marine service under Marine Service."
                ),
            },
            status_code=422,
        )

    save_wizard_state(session_id, state)

    # Advance to the TruShore step when marine is enabled AND at least one
    # location has surf activity AND the SWAN binary is available (checked via
    # GET /setup/marine/swan-check on the API).  In all other cases skip
    # straight to TLS to preserve the existing no-surf flow.
    has_surf_spot = any(
        "surf" in loc_data.get("activities", [])
        for loc_data in new_locations.values()
    )
    if has_surf_spot:
        try:
            client = _get_api_client(state)
            swan_info = client._request("GET", "/setup/marine/swan-check").json()
            if swan_info.get("available"):
                return await step_trushore_get(request)
        except Exception:  # noqa: BLE001
            logger.debug("swan-check failed or returned unavailable — skipping trushore step")

    return await step_tls_get(request)


@router.post("/marine/discover-stations", response_class=HTMLResponse)
async def marine_discover_stations(request: Request) -> HTMLResponse:
    """HTMX: discover nearby NDBC/CO-OPS stations + NWS marine zone/WFO for one location card.

    Scoped to a single location card via hx-include="closest .marine-location-card"
    on the template's button, so exactly one loc_<idx>_lat/lon pair is present in
    the submitted form regardless of how many location cards exist — the index
    itself does not need to be known here.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    form = await request.form()

    lat_val = next((v for k, v in form.items() if _MARINE_LAT_KEY_RE.match(k)), None)
    lon_val = next((v for k, v in form.items() if _MARINE_LON_KEY_RE.match(k)), None)
    lat = _to_float(lat_val)
    lon = _to_float(lon_val)
    if lat is None or lon is None:
        return _render(
            request,
            "marine_station_results.html",
            {"error": _("Enter latitude and longitude before discovering stations."), "result": None},
        )

    try:
        client = _get_api_client(state)
        result = client.discover_marine_stations(lat, lon, radius_miles=50)
    except ValueError:
        return _render(
            request,
            "marine_station_results.html",
            {"error": _("API not connected. Go back to step 1 and reconnect."), "result": None},
        )
    except ApiClientError as exc:
        return _render(
            request,
            "marine_station_results.html",
            {"error": _api_error_message(exc), "result": None},
        )
    except Exception:  # noqa: BLE001
        logger.warning("marine_discover_stations: network error", exc_info=True)
        return _render(
            request,
            "marine_station_results.html",
            {"error": _("Could not reach the API to discover marine stations."), "result": None},
        )

    return _render(request, "marine_station_results.html", {"error": None, "result": result})


@router.post("/marine/coverage", response_class=HTMLResponse)
async def marine_coverage(request: Request) -> HTMLResponse:
    """HTMX: check data coverage for a marine location's coordinates (T3.6)."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    form = await request.form()

    lat_val = next((v for k, v in form.items() if _MARINE_LAT_KEY_RE.match(k)), None)
    lon_val = next((v for k, v in form.items() if _MARINE_LON_KEY_RE.match(k)), None)
    lat = _to_float(lat_val)
    lon = _to_float(lon_val)
    if lat is None or lon is None:
        return HTMLResponse(
            '<span style="color:var(--pico-muted-color);font-size:0.85rem">'
            + html_escape(_("Enter coordinates to check data source availability."))
            + "</span>"
        )

    try:
        client = _get_api_client(state)
        cov = client.get_marine_coverage(lat, lon)
    except Exception:  # noqa: BLE001
        logger.warning("marine_coverage: API error", exc_info=True)
        return HTMLResponse(
            '<span style="color:var(--pico-del-color);font-size:0.85rem">'
            + html_escape(_("Coverage check failed."))
            + "</span>"
        )

    return HTMLResponse(_render_wizard_coverage_html(cov))


def _render_wizard_coverage_html(cov: dict) -> str:
    """Render coverage panel HTML for wizard HTMX swap."""

    def _check(ok: bool, label: str, detail: str = "") -> str:
        icon = "&#x2705;" if ok else "&#x274C;"
        detail_html = (
            f' <span style="color:var(--pico-muted-color)">({html_escape(detail)})</span>'
            if detail else ""
        )
        return (
            f'<div style="display:flex;align-items:center;gap:0.4rem;padding:0.15rem 0">'
            f'<span aria-hidden="true">{icon}</span>'
            f'<span>{html_escape(label)}{detail_html}</span></div>'
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

    parts = []
    tier_label = tier_labels.get(tier, tier)
    tier_color = (
        "var(--pico-ins-color,#16a34a)"
        if tier in ("ofs", "regional_erddap")
        else "var(--pico-color-amber-500,#f59e0b)"
    )
    parts.append(
        f'<div style="font-weight:600;font-size:0.85rem;margin-bottom:0.4rem;color:{tier_color}">'
        f'{html_escape(str(tier_label))}</div>'
    )

    if ofs_model:
        res = cov.get("ofs_model_resolution_deg")
        res_str = f"~{res}" + "\u00b0" if res else ""
        parts.append(_check(True, _("OFS model: {model}").format(model=ofs_model), res_str))
        if ofs_fallback:
            parts.append(_check(True, _("Fallback: {model}").format(model=ofs_fallback)))
    else:
        parts.append(_check(False, _("No OFS coastal model coverage")))

    for cap, cap_label in [
        ("surface_temp", _("Surface temperature")),
        ("water_column", _("Water column profiles")),
        ("currents", _("Ocean currents")),
        ("salinity", _("Salinity")),
        ("modeled_water_levels", _("Modeled water levels")),
        ("forecast", _("Ocean forecast")),
    ]:
        parts.append(_check(cap in available, str(cap_label)))

    parts.append('<hr style="margin:0.3rem 0">')

    if ndbc:
        parts.append(_check(True, _("NDBC buoy: {id}").format(id=ndbc["station_id"]),
                            _("{dist} mi").format(dist=ndbc["distance_miles"])))
    else:
        parts.append(_check(False, _("No NDBC buoy within range")))

    if coops:
        parts.append(_check(True, _("CO-OPS station: {id}").format(id=coops["station_id"]),
                            _("{dist} mi").format(dist=coops["distance_miles"])))
    else:
        parts.append(_check(False, _("No CO-OPS station within range")))

    parts.append(_check(bool(nws_zone), _("NWS marine zone: {zone}").format(zone=nws_zone or "\u2014")))

    prem_labels = {
        "within_threshold": _("Weather station nearby"),
        "too_far": _("Weather station too far"),
        "not_configured": _("Not configured"),
    }
    parts.append(_check(on_prem == "within_threshold", str(prem_labels.get(on_prem, on_prem))))

    return "".join(parts)


@router.post("/marine/discover-structures", response_class=HTMLResponse)
async def marine_discover_structures(request: Request) -> HTMLResponse:
    """HTMX: discover nearby coastal structures via OSM Overpass for one location card."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    form = await request.form()

    lat_val = next((v for k, v in form.items() if _MARINE_LAT_KEY_RE.match(k)), None)
    lon_val = next((v for k, v in form.items() if _MARINE_LON_KEY_RE.match(k)), None)
    idx_val = next((k for k in form.keys() if _MARINE_LAT_KEY_RE.match(k)), None)
    card_idx = idx_val.split("_")[1] if idx_val else "0"
    lat = _to_float(lat_val)
    lon = _to_float(lon_val)
    if lat is None or lon is None:
        return HTMLResponse(
            content='<div class="alert-error" role="alert">Enter coordinates before discovering structures.</div>',
            status_code=200,
        )

    try:
        client = _get_api_client(state)
        result = client.discover_structures(lat, lon, radius_m=2000)
    except Exception:  # noqa: BLE001
        logger.warning("marine_discover_structures: error", exc_info=True)
        return HTMLResponse(
            content='<div class="alert-error" role="alert">Could not discover structures. Check API connection.</div>',
            status_code=200,
        )

    structures = result.get("structures", [])
    if not structures:
        return HTMLResponse(
            content='<div class="alert-info" role="status">No structures found within 2 km. Use "Add Structure Manually" if you know of structures nearby.</div>',
            status_code=200,
        )

    count = len(structures)
    label = "structure" if count == 1 else "structures"
    html_parts = [
        '<div class="alert-success" role="status">',
        f'<p><strong>Found {count} {label}</strong> within 2 km. '
        'Check the ones that affect waves at your spot:</p>',
        '</div>',
        '<div class="discovered-structures-list" style="display:flex;flex-direction:column;gap:0.5rem;margin:0.5rem 0;">',
    ]
    type_labels = {
        "breakwater": "Breakwater",
        "pier": "Pier",
        "groin": "Groin",
        "seawall": "Seawall",
        "jetty": "Jetty",
    }
    mat_labels = {
        "impermeable": "Impermeable",
        "semi_permeable": "Semi-permeable",
        "permeable": "Permeable",
    }
    # Collect obstacle geometries to pass to window.csMapStructures.
    # geometry is [[lat, lon], ...] per MarineDiscoveredStructure.
    map_structures: list[dict[str, Any]] = []

    for i, s in enumerate(structures):
        stype = s.get("type", "breakwater")
        name = s.get("name") or type_labels.get(stype, stype.title())
        dist = s.get("distance_m", 0)
        length = s.get("length_m", 0)
        bearing = s.get("bearing_degrees", 0)
        mat = s.get("material") or ""
        mat_source = s.get("material_source", "operator")
        mat_display = mat_labels.get(mat, "")
        if mat_display and mat_source == "osm":
            mat_html = f'{mat_display}'
        else:
            mat_html = '<span style="color:var(--pico-del-color);">&#9888; Needs input</span>'
        bearing_html = f'{bearing:.0f}°'
        geometry = s.get("geometry") or []
        geometry_json = html_escape(json.dumps(geometry))
        html_parts.append(
            f'<label style="display:flex;align-items:flex-start;gap:0.75rem;padding:0.75rem;'
            f'border:1px solid var(--pico-muted-border-color);border-radius:0.375rem;cursor:pointer;">'
            f'<input type="checkbox" class="discovered-structure-check" '
            f'style="margin-top:0.25rem;flex-shrink:0;" '
            f'data-idx="{card_idx}" '
            f'data-type="{stype}" '
            f'data-material="{mat}" '
            f'data-material-source="{mat_source}" '
            f'data-length="{length:.1f}" '
            f'data-bearing="{bearing:.1f}" '
            f'data-distance="{dist:.1f}" '
            f'data-geometry="{geometry_json}">'
            f'<div style="flex:1;line-height:1.4;">'
            f'<strong>{name}</strong>'
            f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(10rem,1fr));'
            f'gap:0.25rem 1rem;margin-top:0.25rem;">'
            f'<small style="color:var(--pico-muted-color);">Type: {type_labels.get(stype, stype.title())}</small>'
            f'<small style="color:var(--pico-muted-color);">Distance: {dist:.0f} m</small>'
            f'<small style="color:var(--pico-muted-color);">Length: {length:.0f} m</small>'
            f'<small style="color:var(--pico-muted-color);">Bearing: {bearing_html}</small>'
            f'<small style="color:var(--pico-muted-color);">Material: {mat_html}</small>'
            f'</div>'
            f'<small style="color:var(--pico-muted-color);font-style:italic;margin-top:0.25rem;display:block;">'
            f'Length, bearing, and distance computed from OpenStreetMap geometry.'
            f'</small></div></label>'
        )
        map_structures.append({"type": stype, "geometry": geometry})
    html_parts.append('</div>')

    # Emit a <script> that calls window.csMapStructures to paint discovered
    # OBSTACLE geometries on the map and refresh transect colors.
    html_parts.append(
        f'<script>'
        f'(function(){{'
        f'  var s={json.dumps(map_structures)};'
        f'  if(typeof window.csMapStructures==="function"){{window.csMapStructures({json.dumps(card_idx)},s);}}'
        f'}})();'
        f'</script>'
    )
    html_parts.append('<script>')
    html_parts.append('document.querySelectorAll(".discovered-structure-check").forEach(function(cb) {')
    html_parts.append('  cb.addEventListener("change", function() {')
    html_parts.append('    var card = this.closest(".marine-location-card");')
    html_parts.append('    var container = card ? card.querySelector(".structure-cards") : null;')
    html_parts.append('    if (!container) return;')
    html_parts.append('    if (this.checked) {')
    html_parts.append('      var si = container.querySelectorAll(".structure-card").length;')
    html_parts.append('      var idx = this.getAttribute("data-idx");')
    html_parts.append('      var fs = document.createElement("fieldset");')
    html_parts.append('      fs.className = "structure-card";')
    html_parts.append('      fs.setAttribute("data-structure-idx", si);')
    html_parts.append('      fs.setAttribute("data-discovered-cb", this.value || si);')
    html_parts.append('      var matSrc = this.dataset.materialSource;')
    html_parts.append('      var matWarn = (matSrc !== "osm") ? " &#9888;" : "";')
    html_parts.append('      var matVal = this.dataset.material || "semi_permeable";')
    html_parts.append('      var geomRaw = this.dataset.geometry;')
    html_parts.append('      var coordsInputHtml = "";')
    html_parts.append('      if (geomRaw) {')
    html_parts.append('        try {')
    html_parts.append('          var geomArr = JSON.parse(geomRaw);')
    html_parts.append('          if (geomArr && geomArr.length) {')
    html_parts.append('            var lonLatArr = geomArr.map(function (pt) { return [pt[1], pt[0]]; });')
    html_parts.append('            coordsInputHtml = \'<input type="hidden" name="loc_\' + idx + \'_structure_\' + si + \'_coordinates" value="\' + JSON.stringify(lonLatArr) + \'">\';')
    html_parts.append('          }')
    html_parts.append('        } catch (e) {}')
    html_parts.append('      }')
    html_parts.append('      fs.innerHTML = \'<div style="display:flex;justify-content:space-between;align-items:center;">\'')
    html_parts.append('        + \'<legend>Structure \' + (si+1) + \' (discovered)</legend>\'')
    html_parts.append('        + \'<button type="button" class="remove-structure-btn" style="font-size:0.8rem;padding:0.2rem 0.5rem;cursor:pointer;">&times;</button></div>\'')
    html_parts.append('        + \'<div class="grid"><div><label>Type<select name="loc_\' + idx + \'_structure_\' + si + \'_type" class="struct-type">\'')
    html_parts.append('        + \'<option value="jetty"\' + (this.dataset.type==="jetty"?" selected":"") + \'>Jetty</option>\'')
    html_parts.append('        + \'<option value="pier"\' + (this.dataset.type==="pier"?" selected":"") + \'>Pier</option>\'')
    html_parts.append('        + \'<option value="breakwater"\' + (this.dataset.type==="breakwater"?" selected":"") + \'>Breakwater</option>\'')
    html_parts.append('        + \'<option value="seawall"\' + (this.dataset.type==="seawall"?" selected":"") + \'>Seawall</option>\'')
    html_parts.append('        + \'<option value="groin"\' + (this.dataset.type==="groin"?" selected":"") + \'>Groin</option>\'')
    html_parts.append('        + \'</select></label></div><div><label>Material\' + matWarn + \'<select name="loc_\' + idx + \'_structure_\' + si + \'_material" class="struct-material"\' + (matSrc !== "osm" ? \' style="border-color:var(--pico-del-color);"\' : \'\') + \'>\'')
    html_parts.append('        + \'<option value="impermeable"\' + (matVal==="impermeable"?" selected":"") + \'>Impermeable</option>\'')
    html_parts.append('        + \'<option value="semi_permeable"\' + (matVal==="semi_permeable"?" selected":"") + \'>Semi-permeable</option>\'')
    html_parts.append('        + \'<option value="permeable"\' + (matVal==="permeable"?" selected":"") + \'>Permeable</option>\'')
    html_parts.append('        + \'</select></label></div></div>\'')
    html_parts.append('        + \'<div class="grid"><div><label>Length (m)<input type="number" step="0.1" min="1" name="loc_\' + idx + \'_structure_\' + si + \'_length_m" value="\' + this.dataset.length + \'"></label></div>\'')
    html_parts.append('        + \'<div><label>Bearing (&deg;)<input type="number" step="0.1" min="0" max="360" name="loc_\' + idx + \'_structure_\' + si + \'_bearing_degrees" value="\' + this.dataset.bearing + \'"></label></div>\'')
    html_parts.append('        + \'<div><label>Distance (m)<input type="number" step="0.1" min="1" name="loc_\' + idx + \'_structure_\' + si + \'_distance_m" value="\' + this.dataset.distance + \'"></label></div></div>\'')
    html_parts.append('        + coordsInputHtml;')
    html_parts.append('      container.appendChild(fs);')
    html_parts.append('    } else {')
    html_parts.append('      var cards = container.querySelectorAll(".structure-card");')
    html_parts.append('      if (cards.length > 0) cards[cards.length - 1].remove();')
    html_parts.append('    }')
    html_parts.append('  });')
    html_parts.append('});')
    html_parts.append('</script>')

    return HTMLResponse(content="\n".join(html_parts), status_code=200)



@router.get("/marine/species", response_class=HTMLResponse)
async def marine_species(request: Request) -> HTMLResponse:
    """HTMX: load the species checklist for one location card's fishing section (T2.5).

    Fires on the species container's own hx-trigger="load" (initial card render,
    including JS-cloned cards — see step_marine.html) and again whenever the
    target-category <select> changes. Scoped to a single location card via
    hx-include="closest .marine-location-card", same pattern as
    marine_discover_stations() above — but this route is a
    GET, so htmx serializes the included card fields as query params rather
    than a form body.

    Missing/incomplete inputs (no coordinates yet, no category chosen) are not
    treated as errors — a brand-new location card legitimately has neither on
    first render. Only a reachability/API failure renders as an error.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    params = request.query_params

    idx = "0"
    categories: list[str] = []
    for key, value in params.multi_items():
        m = _MARINE_TARGET_CATEGORY_KEY_RE.match(key)
        if m:
            idx = m.group(1)
            val = value.strip()
            if val in _MARINE_VALID_TARGET_CATEGORIES and val not in categories:
                categories.append(val)

    lat_val = next((v for k, v in params.items() if _MARINE_LAT_KEY_RE.match(k)), None)
    lon_val = next((v for k, v in params.items() if _MARINE_LON_KEY_RE.match(k)), None)
    lat = _to_float(lat_val)
    lon = _to_float(lon_val)

    prev_raw = next(
        (v for k, v in params.items() if _MARINE_SPECIES_PREV_KEY_RE.match(k)), ""
    ).strip()
    prev_species = {s.strip() for s in prev_raw.split(",") if s.strip()} or None

    ctx: dict[str, Any] = {"error": None, "prompt": None, "result": None, "idx": idx, "prev_species": prev_species}

    if lat is None or lon is None or not categories:
        ctx["prompt"] = _("Enter coordinates and select at least one target category to load available species.")
        return _render(request, "marine_species_result.html", ctx)

    category_param = ",".join(categories)
    try:
        client = _get_api_client(state)
        result = client.get_marine_species(lat, lon, category_param)
    except ValueError:
        ctx["error"] = _("API not connected. Go back to step 1 and reconnect.")
        return _render(request, "marine_species_result.html", ctx)
    except ApiClientError as exc:
        ctx["error"] = _api_error_message(exc)
        return _render(request, "marine_species_result.html", ctx)
    except Exception:  # noqa: BLE001
        logger.warning("marine_species: network error", exc_info=True)
        ctx["error"] = _("Could not reach the API to load species data.")
        return _render(request, "marine_species_result.html", ctx)

    ctx["result"] = result
    return _render(request, "marine_species_result.html", ctx)


# ---------------------------------------------------------------------------
# Step 14: TLS / HTTPS Configuration
# ---------------------------------------------------------------------------

# Allowed file types for TLS certificate/key uploads (Manual mode).
# These are NEVER written to the web-served branding/ directory — see
# _handle_tls_upload.  MIME types are advisory only: browsers frequently
# send application/octet-stream (or nothing at all) for PEM files, so a
# missing/generic content_type is not treated as an error — only a content_type
# that is present AND recognisably wrong is rejected.
_TLS_UPLOAD_EXTS = frozenset({".pem", ".crt", ".key"})
_TLS_UPLOAD_MIMES = frozenset({"text/plain", "application/x-pem-file", "application/octet-stream"})
_TLS_UPLOAD_MAX_BYTES = 100 * 1024  # 100 KB


async def _handle_tls_upload(form: Any, field_name: str) -> tuple[str | None, str | None]:
    """Process one TLS certificate/key file upload (Manual mode).

    Returns ``(file_path, error)`` where *file_path* is the full filesystem
    path the file was written to (NOT a URL — unlike branding uploads, TLS
    private keys must never live under the web-served branding/ directory),
    or None if no file was uploaded.  *error* is a human-readable message or
    None.

    Files are written to ``{config_dir}/tls/`` with mode 0600.  The caller
    uses (None, None) to mean "keep the typed path-field value instead."
    """
    if _config_dir is None:
        return None, _("Configuration directory not set — cannot save uploaded files.")

    upload = form.get(field_name)

    # Starlette represents a non-selected file input as either None or a
    # UploadFile with an empty .filename.  Both mean "no file chosen."
    if upload is None or not hasattr(upload, "filename") or not upload.filename:
        return None, None

    raw_filename: str = str(upload.filename)
    suffix = Path(raw_filename).suffix.lower()
    if suffix not in _TLS_UPLOAD_EXTS:
        return None, _(
            'Unsupported file type "{suffix}" for {field}. Allowed: {allowed}.'
        ).format(
            suffix=suffix,
            field=field_name.replace("_file", ""),
            allowed=", ".join(sorted(_TLS_UPLOAD_EXTS)),
        )

    content_type = getattr(upload, "content_type", None)
    if content_type and content_type not in _TLS_UPLOAD_MIMES:
        return None, _(
            'Unsupported content type "{content_type}" for {field}.'
        ).format(content_type=content_type, field=field_name.replace("_file", ""))

    data: bytes = await upload.read()
    if len(data) > _TLS_UPLOAD_MAX_BYTES:
        max_kb = _TLS_UPLOAD_MAX_BYTES // 1024
        return None, _(
            "{field}: file is {size} KB, exceeds the {limit} KB limit."
        ).format(
            field=field_name.replace("_file", "").replace("_", " ").title(),
            size=len(data) // 1024,
            limit=max_kb,
        )

    safe_name = _sanitise_filename(raw_filename)
    dest_dir = _config_dir / "tls"
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o700)
    except OSError:
        pass
    dest = dest_dir / safe_name
    dest.write_bytes(data)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    logger.info("Saved TLS upload %s → %s", field_name, dest)

    return str(dest), None


@router.get("/trushore", response_class=HTMLResponse)
async def step_trushore_get(request: Request) -> HTMLResponse:
    """Step 14: SWAN+TruShore — render the nearshore model setup form.

    This step is shown only when:
    - marine_enabled is True AND at least one location has surf activity
    - The SWAN binary is available (checked via GET /setup/marine/swan-check)

    If SWAN is not available, the marine POST skips this step entirely and
    advances directly to TLS.  The step can also be navigated to directly
    (e.g. after a validation error on the TruShore POST).
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    # Query SWAN availability from the API.
    swan_info: dict[str, Any] = {"available": False, "version": None, "path": None, "cpu_cores": None}
    error: str | None = None
    try:
        client = _get_api_client(state)
        swan_info = client._request("GET", "/setup/marine/swan-check").json()
    except ValueError:
        error = _("API not connected. Go back to step 1 and reconnect.")
    except Exception:  # noqa: BLE001
        logger.debug("step_trushore_get: swan-check failed", exc_info=True)
        error = _("Could not check SWAN availability — API may be unreachable.")

    # Collect surf-enabled location names for the per-spot section.
    surf_locations: dict[str, dict[str, Any]] = {
        slug: loc
        for slug, loc in state.marine_locations.items()
        if "surf" in loc.get("activities", [])
    }

    # Pre-compute default SWAN grid bbox from marine location coordinates.
    lats = [loc["lat"] for loc in state.marine_locations.values() if loc.get("lat") is not None]
    lons = [loc["lon"] for loc in state.marine_locations.values() if loc.get("lon") is not None]
    if lats and lons:
        default_bbox = {
            "south": round(min(lats) - 0.2, 2),
            "north": round(max(lats) + 0.2, 2),
            "west": round(min(lons) - 0.2, 2),
            "east": round(max(lons) + 0.2, 2),
        }
    else:
        default_bbox = {"south": "", "north": "", "west": "", "east": ""}

    return _render(
        request,
        "step_trushore.html",
        {
            "step": 14,
            "state": state,
            "swan_info": swan_info,
            "surf_locations": surf_locations,
            "default_bbox": default_bbox,
            "error": error,
        },
    )


@router.post("/trushore", response_class=HTMLResponse)
async def step_trushore_post(request: Request) -> HTMLResponse:
    """Save SWAN+TruShore configuration and advance to step 15 (TLS)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)

    # Deployment mode and service URL are gone (T7.2): where SWAN runs is no
    # longer a per-step choice.  SWAN lives in the marine service, which is
    # reached at the single marine_service_url set on the providers step.

    # OMP threads
    omp_threads_raw = str(form.get("trushore_omp_num_threads", "0")).strip()
    try:
        omp_threads = max(0, int(omp_threads_raw))
    except ValueError:
        omp_threads = 0
    state.trushore_omp_num_threads = omp_threads

    # Outer grid resolution (km) — coarse continental shelf grid
    outer_res_raw = str(form.get("trushore_outer_grid_resolution_km", "3")).strip()
    try:
        outer_res = float(outer_res_raw)
        if not (1.0 <= outer_res <= 10.0):
            outer_res = 3.0
    except ValueError:
        outer_res = 3.0
    state.trushore_outer_grid_resolution_km = outer_res

    # Inner nest resolution (m) — fine grid focused on surf spots
    inner_res_raw = str(form.get("trushore_inner_nest_resolution_m", "200")).strip()
    try:
        inner_res = int(inner_res_raw)
        if not (50 <= inner_res <= 1000):
            inner_res = 200
    except ValueError:
        inner_res = 200
    state.trushore_inner_nest_resolution_m = inner_res

    # Per-spot surf settings: breaker_formula and surf_height_display
    # Stored in marine_locations[slug]["surf"] — same dict used by build_marine_payload().
    for slug, loc in state.marine_locations.items():
        if "surf" not in loc.get("activities", []):
            continue
        surf = dict(loc.get("surf", {}))

        bf = str(form.get(f"surf_{slug}_breaker_formula", "")).strip()
        if bf in ("komar_gaughan", "caldwell"):
            surf["breaker_formula"] = bf

        shd = str(form.get(f"surf_{slug}_surf_height_display", "")).strip()
        if shd in ("face", "hawaiian"):
            surf["surf_height_display"] = shd

        loc["surf"] = surf
        state.marine_locations[slug] = loc

    # 2026-08-03 operator ruling (C9): marks this step submitted this
    # session so the apply payload's "swan" block is only sent when the
    # operator actually entered it -- see swan_step_completed (state.py).
    state.swan_step_completed = True

    save_wizard_state(session_id, state)
    return await step_tls_get(request)


@router.post("/providers/test-marine", response_class=HTMLResponse)
async def providers_test_marine(request: Request) -> HTMLResponse:
    """HTMX: ask the API to test the marine service connection (T7.3).

    The wizard never contacts the marine service itself — that would break the
    invariant that the marine service is reached only through the API
    (ARCHITECTURE.md).  This proxies a POST to the API's
    ``/setup/providers/test-marine`` endpoint, which probes the marine service
    and returns ``{ok, version, spots, last_run, status, error}``.

    The API also decides whether a failure was a rejected secret; this handler
    renders what it is told and never infers that classification itself.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    form = await request.form()
    marine_url = str(form.get("marine_service_url", "")).strip()
    marine_secret = str(form.get("marine_service_secret", "")).strip()
    verify_tls = str(form.get("marine_verify_tls", "")).strip() == "1"

    # The secret box renders empty when a secret is already saved, so an empty
    # box means "test with the secret I already have."
    if not marine_secret and state.marine_service_secret:
        marine_secret = state.marine_service_secret

    if not marine_url:
        return HTMLResponse(
            '<span class="error-text">'
            + html_escape(_("Enter a marine service URL before testing."))
            + "</span>"
        )

    try:
        client = _get_api_client(state)
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
        logger.debug("providers_test_marine: error", exc_info=True)
        return HTMLResponse(
            '<span class="error-text">'
            + html_escape(_("Could not reach the API to test the marine service."))
            + "</span>"
        )

    if not data.get("ok"):
        detail = data.get("error") or _("Unknown error")
        return HTMLResponse(
            '<span class="error-text">'
            + html_escape(
                _("The API could not use the marine service: {error}").format(error=detail)
            )
            + "</span>",
            status_code=200,
        )

    # Success — report what the API learned about the service.
    lines = ['<span class="success-text">'
             + html_escape(_("The API reached the marine service."))
             + "</span>"]
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
        lines.append(
            '<br><small>' + html_escape(" · ".join(str(d) for d in details)) + "</small>"
        )
    return HTMLResponse("".join(lines))


@router.get("/tls", response_class=HTMLResponse)
async def step_tls_get(request: Request) -> HTMLResponse:
    """Step 15: TLS — render the certificate mode selection form."""
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if not state.tls_mode:
        _merge_from_existing_config(state)
    from weewx_clearskies_config.registry import registry
    fields = registry.get_fields_for_section("tls")
    values = {
        "mode": state.tls_mode,
        "domain": state.tls_domain,
        "acme_email": state.tls_acme_email,
        "dns_provider": state.tls_dns_provider,
        "dns_api_token": state.tls_dns_api_token,
        "cert_path": state.tls_cert_path,
        "key_path": state.tls_key_path,
    }
    return _render(request, "step_tls.html", {"step": 14, "state": state, "fields": fields, "values": values, "error": None})


@router.post("/tls", response_class=HTMLResponse)
async def step_tls_post(request: Request) -> HTMLResponse:
    """Save TLS configuration and advance to step 15 (review)."""
    session_id = _require_session(request)
    form = await request.form()
    state = get_wizard_state(session_id)
    from weewx_clearskies_config.registry import registry
    fields = registry.get_fields_for_section("tls")

    state.tls_mode = str(form.get("mode", "")).strip()
    state.tls_domain = str(form.get("domain", "")).strip()
    state.tls_acme_email = str(form.get("acme_email", "")).strip()
    state.tls_dns_provider = str(form.get("dns_provider", "")).strip()
    state.tls_dns_api_token = str(form.get("dns_api_token", "")).strip()

    # --- Manual mode: certificate/key, either uploaded or typed as a path ---
    # A hidden "cert_was_uploaded"/"key_was_uploaded" marker is rendered by
    # the template when the current value came from a prior upload; it lets
    # us distinguish "operator re-submitted the same uploaded file's path
    # unchanged" from "operator is typing a fresh path" without requiring a
    # new file on every submit.
    cert_path_input = str(form.get("cert_path", "")).strip()
    key_path_input = str(form.get("key_path", "")).strip()
    cert_prev_uploaded = str(form.get("cert_was_uploaded", "")).strip() == "1"
    key_prev_uploaded = str(form.get("key_was_uploaded", "")).strip() == "1"

    upload_errors: list[str] = []

    cert_upload_path, cert_err = await _handle_tls_upload(form, "cert_file")
    if cert_err:
        upload_errors.append(cert_err)
    if cert_upload_path:
        state.tls_cert_path = cert_upload_path
        state.tls_cert_uploaded = True
    elif cert_prev_uploaded and cert_path_input:
        # No new file this submission — carry forward the previously
        # uploaded file's path and uploaded status.
        state.tls_cert_path = cert_path_input
        state.tls_cert_uploaded = True
    else:
        state.tls_cert_path = cert_path_input
        state.tls_cert_uploaded = False

    key_upload_path, key_err = await _handle_tls_upload(form, "key_file")
    if key_err:
        upload_errors.append(key_err)
    if key_upload_path:
        state.tls_key_path = key_upload_path
        state.tls_key_uploaded = True
    elif key_prev_uploaded and key_path_input:
        state.tls_key_path = key_path_input
        state.tls_key_uploaded = True
    else:
        state.tls_key_path = key_path_input
        state.tls_key_uploaded = False

    def _tls_error(msg: str) -> HTMLResponse:
        values = {
            "mode": state.tls_mode,
            "domain": state.tls_domain,
            "acme_email": state.tls_acme_email,
            "dns_provider": state.tls_dns_provider,
            "dns_api_token": state.tls_dns_api_token,
            "cert_path": state.tls_cert_path,
            "key_path": state.tls_key_path,
        }
        return _render(
            request,
            "step_tls.html",
            {"step": 14, "state": state, "fields": fields, "values": values, "error": msg},
            status_code=422,
        )

    if upload_errors:
        return _tls_error(" ".join(upload_errors))

    _VALID_TLS_MODES = {opt.value for f in fields for opt in (f.options or ()) if f.config_key == "mode"}
    if not _VALID_TLS_MODES:
        _VALID_TLS_MODES = {"self-signed", "acme_http01", "acme_dns01", "manual", "behind_proxy"}
    if state.tls_mode not in _VALID_TLS_MODES:
        return _tls_error(_("Please select a TLS configuration mode."))

    if state.tls_mode in ("acme_http01", "acme_dns01") and not state.tls_domain:
        return _tls_error(_("Domain name is required for automated TLS."))

    if state.tls_mode == "acme_http01" and not state.tls_acme_email:
        return _tls_error(_("Email address is required for Let's Encrypt."))

    if state.tls_mode == "acme_dns01" and not state.tls_dns_provider:
        return _tls_error(_("DNS provider is required for DNS-01 challenge."))

    if state.tls_mode == "acme_dns01" and not state.tls_dns_api_token:
        return _tls_error(_("DNS provider API token is required."))

    if state.tls_mode == "manual" and not state.tls_cert_path:
        return _tls_error(_("Certificate path is required for Manual mode. Upload a certificate file or enter its path."))

    if state.tls_mode == "manual" and not state.tls_key_path:
        return _tls_error(_("Key path is required for Manual mode. Upload a key file or enter its path."))

    save_wizard_state(session_id, state)
    return await step9_review_get(request)


# ---------------------------------------------------------------------------
# Step 9 (display 15): Review + Apply
# ---------------------------------------------------------------------------


@router.get("/step/9", response_class=HTMLResponse)
async def step9_review_get(request: Request) -> HTMLResponse:
    session_id = _require_session(request)
    state = get_wizard_state(session_id)
    if state.db_host is None and state.station_name is None:
        _merge_from_existing_config(state)
    return _render(
        request,
        "step_review.html",
        {"step": 15, "state": state, "error": None},
    )


# Map wizard-internal provider IDs to the names expected by the API.
# The wizard stores user-facing identifiers (e.g. "nws_alerts") but the API
# schema uses shorter canonical names (e.g. "nws").  Add entries here as new
# providers are discovered to have a mismatch.
_PROVIDER_NAME_MAP: dict[str, str] = {
    "nws_alerts": "nws",
    "aeris_alerts": "aeris",
    "openweathermap_alerts": "openweathermap",
    "aeris_aqi": "aeris",
    "openmeteo_aqi": "openmeteo",
    "openweathermap_aqi": "openweathermap",
}


def _check_write_permissions(paths: list[str]) -> list[str]:
    """Return a list of paths that are not writable by the current process.

    For each path: if the path already exists, test it directly.  If it does
    not exist yet, test the parent directory (which must be writable for the
    file to be created).  Paths whose parent directory also does not exist are
    checked at the nearest existing ancestor — the directory write must succeed
    for os.makedirs to be able to create the missing chain.
    """
    failed: list[str] = []
    for path in paths:
        # Walk up to the nearest existing ancestor to test write access.
        check = Path(path)
        while not check.exists():
            check = check.parent
            if check == check.parent:
                # Reached filesystem root without finding an existing path.
                # Conservatively mark as failed.
                failed.append(path)
                break
        else:
            if not os.access(check, os.W_OK):
                failed.append(path)
    return failed


@router.post("/apply", response_class=HTMLResponse)
async def wizard_apply(request: Request) -> HTMLResponse:
    """Send config to the API, write local config files, display the completion page.

    Flow (ADR-038):
      1. Build the ApplyRequest payload from wizard state and POST it to the API.
         The API writes its own api.conf and secrets.env (DB password, provider
         API keys).  If this step fails, render the review page with the error so
         the operator can retry without re-entering all settings.
      2. Write local config files (stack.conf, secrets.env with
         local secrets only — proxy secret).
      3. Clear wizard state and render the completion page.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    if _config_dir is None:
        assert _templates is not None
        return _templates.TemplateResponse(
            request=request,
            name="wizard/step_complete.html",
            context={
                "step": 12,
                "error": _("The configuration directory has not been set. Please restart the setup tool with the correct --config-dir option."),
                "result": None,
            },
            status_code=500,
        )

    # ------------------------------------------------------------------
    # Step 1: Send configuration to the API (POST /setup/apply).
    # The API writes its own api.conf and stores DB password / provider keys
    # in its own secrets.env.  Fail early and show the review page on error.
    # ------------------------------------------------------------------

    # Build the column_mapping for the API: canonical → db_col (inverted from
    # state which stores db_col → canonical).  Skip unmapped columns (None value).
    api_column_mapping: dict[str, str] = {
        canonical: db_col
        for db_col, canonical in state.column_mapping.items()
        if canonical is not None
    }
    # Persist explicitly "not mapped" columns so re-runs don't re-suggest them.
    unmapped = [db_col for db_col, canonical in state.column_mapping.items() if canonical is None]
    if unmapped:
        api_column_mapping["_excluded"] = ",".join(sorted(unmapped))

    # Build providers dict: domain → ProviderConfig-shaped dict.
    # state.providers maps domain → provider_id.
    # state.api_keys maps provider_id → {field_name → value}.
    api_providers: dict[str, Any] = {}
    for domain, provider_id in state.providers.items():
        creds = state.api_keys.get(provider_id, {})
        # Normalise the provider name: the wizard may store an internal ID that
        # differs from the short name the API expects (e.g. "nws_alerts" → "nws").
        api_provider_name = _PROVIDER_NAME_MAP.get(provider_id, provider_id)
        provider_entry: dict[str, Any] = {"provider": api_provider_name}
        api_key_val = creds.get("api_key") or creds.get("client_id")
        api_secret_val = creds.get("api_secret") or creds.get("client_secret")
        if api_key_val:
            provider_entry["api_key"] = api_key_val
        if api_secret_val:
            provider_entry["api_secret"] = api_secret_val
        if creds.get("pws_station_id"):
            provider_entry["pws_station_id"] = creds["pws_station_id"]
        if creds.get("nws_user_agent_contact"):
            provider_entry["nws_user_agent_contact"] = creds["nws_user_agent_contact"]
        if creds.get("iframe_url"):
            provider_entry["iframe_url"] = creds["iframe_url"]
        # AQI regional configuration (ADR-059) — saved locally in stack.conf
        # for wizard re-run persistence.  NOT sent in the API payload because
        # the API's ApplyRequest schema uses extra="forbid" and doesn't accept
        # these fields yet.  When the API schema is updated, uncomment the
        # lines below to pass them through.
        # if domain == "aqi":
        #     if api_provider_name == "aeris":
        #         provider_entry["aqi_filter"] = state.aeris_aqi_filter
        #     elif api_provider_name == "openmeteo":
        #         provider_entry["aqi_index"] = state.openmeteo_aqi_index
        #     elif api_provider_name in ("iqair", "iq_air"):
        #         provider_entry["aqi_scale"] = state.iqair_aqi_scale
        # Aeris forecast model selection (ADR-063) — written to [forecast]
        # aeris_forecast_model in api.conf via the API's apply handler.
        if domain == "forecast" and api_provider_name == "aeris":
            provider_entry["aeris_forecast_model"] = state.aeris_forecast_model
        # LibreWxR endpoint and bounds — added to the radar provider entry
        # so the API writes them to [radar] in api.conf.
        if domain == "radar" and api_provider_name == "librewxr":
            provider_entry["librewxr_endpoint"] = state.librewxr_endpoint
            if state.librewxr_bounds:
                provider_entry["librewxr_bounds"] = state.librewxr_bounds
        # Marine alert radius (T6.4) — attached to the alerts provider entry so
        # the API can discover nearby NWS marine zones for alert display.
        # Only sent when an alerts provider is actually selected (this loop
        # only runs for domains present in state.providers) and the radius
        # is non-zero (0 means "disabled" — omit rather than send a no-op).
        if domain == "alerts" and state.marine_alert_radius_miles > 0:
            provider_entry["marine_alert_radius_miles"] = state.marine_alert_radius_miles
        api_providers[domain] = provider_entry

    api_payload: dict[str, Any] = {
        "database": {
            "kind": state.db_kind,
            "host": state.db_host or "",
            "port": state.db_port,
            "user": state.db_user or "",
            "password": state.db_password or "",
            "name": state.db_name,
            "path": state.db_path or "",
        },
        "column_mapping": api_column_mapping,
        "station": {
            "name": state.station_name,
            "latitude": state.latitude,
            "longitude": state.longitude,
            "altitude_meters": state.altitude_meters,
            "timezone": state.timezone,
            "default_locale": state.default_locale,
        },
    }

    # Confirmed unit assignments from step 3 — the API writes these to
    # [column_units] in api.conf (T2.6).
    if state.column_units:
        api_payload["column_units"] = state.column_units

    if api_providers:
        api_payload["providers"] = api_providers

    if state.proxy_secret:
        api_payload["proxy_secret"] = state.proxy_secret

    api_payload["skin_conf"] = build_skin_conf_payload(state)

    # Branding and social fields are no longer sent to the API (ADR-022 amendment).
    # They are written to branding.json (a static file served by Caddy) in the
    # local write step below.  The API payload now contains only database,
    # column mapping, station, providers, and earthquake settings.

    # Earthquake provider settings (sent even when no earthquakes provider is
    # selected, so the API can initialise defaults without a second apply call).
    api_payload["earthquakes"] = {
        "default_radius_km": state.earthquake_radius_km,
        "min_magnitude": state.earthquake_min_magnitude,
        "default_days": state.earthquake_default_days,
    }

    # Marine locations (T6.1) — omitted entirely when marine features are
    # disabled or no locations were configured (see build_marine_payload()).
    marine_payload = build_marine_payload(state)
    if marine_payload:
        api_payload["marine"] = marine_payload

    # SWAN+TruShore nearshore model (T4.4) — included when marine is enabled,
    # the operator has at least one surf location, AND the swan/TruShore step
    # was actually submitted this session (state.swan_step_completed).
    # 2026-08-03 operator ruling (C9): previously this block sent rendered
    # DEFAULTS unconditionally whenever the first two conditions held, even
    # when the trushore step was skipped (SWAN unavailable) or never
    # reached -- overwriting any real [swan] values already on disk on
    # re-run. Gating on swan_step_completed means a session that never
    # POSTed the step never sends the block, and a resumed session that
    # previously completed it still sends real values (the flag is
    # persisted with the rest of WizardState — see state_persistence.py).
    if (
        marine_payload
        and any(
            "surf" in loc.get("activities", [])
            for loc in state.marine_locations.values()
        )
        and state.swan_step_completed
    ):
        # Key is "swan", not "trushore": the API renamed this ApplyRequest
        # field in the TruShore->SWAN rename (API 0685121) and the stack was
        # never updated.  ApplyRequest is extra="forbid", so the old key 422s
        # the entire apply.  See C-67.
        api_payload["swan"] = build_trushore_payload(state)

    # Marine companion service connection (T7.2).
    # Top-level fields, matching where the compute fields used to sit: the API
    # writes marine_service_url and marine_verify_tls to [providers] in
    # api.conf, and marine_service_secret to secrets.env as
    # MARINE_SERVICE_SECRET (API-MANUAL §19.2).
    if state.marine_service_url:
        api_payload["marine_service_url"] = state.marine_service_url
        api_payload["marine_verify_tls"] = state.marine_verify_tls
    if state.marine_service_secret:
        api_payload["marine_service_secret"] = state.marine_service_secret

    # Unit configuration — sent to the API so it writes to api.conf [units].
    # This is the single unit authority (T2A.5, ADR-042).
    if state.units is not None:
        units_payload: dict[str, Any] = {"groups": state.units}
        # Include imported string_formats, labels, ordinates if available
        if state.imported_config is not None:
            imp_units = state.imported_config.get("units", {})
            sf = imp_units.get("string_formats")
            if sf:
                units_payload["string_formats"] = sf
            lb = imp_units.get("labels")
            if lb:
                units_payload["labels"] = lb
            ords = imp_units.get("ordinates", {})
            dirs = ords.get("directions", [])
            if dirs:
                if isinstance(dirs, str):
                    units_payload["ordinates"] = [d.strip() for d in dirs.split(",")]
                else:
                    units_payload["ordinates"] = list(dirs)
        api_payload["units"] = units_payload

    apply_response: dict[str, Any] | None = None
    try:
        client = _get_api_client(state)
        apply_response = client.apply(api_payload)
    except ValueError:
        # API not connected — state.api_address or api_session_id is missing.
        return _render(
            request,
            "step_review.html",
            {
                "step": 15,
                "state": state,
                "error": _("API not connected. Go back to step 1 and reconnect before applying."),
            },
            status_code=422,
        )
    except ApiClientError as exc:
        logger.error("wizard_apply: API apply call failed (%s): %s", exc.status_code, exc.detail)
        # 401/410/503 get the same friendly framing used by every other wizard
        # step (see _api_error_message); 422 and 5xx get apply-specific
        # messages so the operator sees *what* failed (validation vs. server
        # error) rather than a generic "API returned an error" string.
        if exc.status_code == 422:
            error_msg = _("Configuration validation failed: {detail}").format(detail=exc.detail)
        elif exc.status_code >= 500:
            error_msg = _("API error: {detail}").format(detail=exc.detail)
        elif exc.status_code in (401, 410, 503):
            error_msg = _api_error_message(exc)
        else:
            error_msg = _("API rejected the configuration (HTTP {status_code}): {detail}").format(
                status_code=exc.status_code, detail=exc.detail
            )
        return _render(
            request,
            "step_review.html",
            {
                "step": 15,
                "state": state,
                "error": error_msg,
            },
            status_code=422,
        )
    except httpx.ConnectError:
        logger.error("wizard_apply: connection refused calling API apply")
        return _render(
            request,
            "step_review.html",
            {
                "step": 15,
                "state": state,
                "error": _("The API is not running. Start weewx-clearskies-api and try again."),
            },
            status_code=422,
        )
    except httpx.TimeoutException:
        logger.error("wizard_apply: API apply call timed out after %ss", APPLY_TIMEOUT_SECONDS)
        return _render(
            request,
            "step_review.html",
            {
                "step": 15,
                "state": state,
                "error": _(
                    "The API took too long to respond (>{timeout:.0f}s). Check API logs for errors."
                ).format(timeout=APPLY_TIMEOUT_SECONDS),
            },
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("wizard_apply: unexpected error calling API apply")
        return _render(
            request,
            "step_review.html",
            {
                "step": 15,
                "state": state,
                "error": _("Could not connect to the API: {error}").format(error=exc),
            },
            status_code=422,
        )

    # Extract the one-time restart token issued by /setup/apply.  Present on
    # first-run; absent on re-run (proxy auth is used instead).
    restart_token: str | None = None
    if apply_response and isinstance(apply_response, dict):
        restart_token = apply_response.get("restart_token") or None

    # Marine config push outcome (T7.6): {"attempted": bool, "ok": bool,
    # "error": str | None}.  The API pushes the marine config subset to the
    # marine service after writing api.conf; a failed push never fails the
    # apply (API-MANUAL §19.5), so this is surfaced on the completion page as
    # a reachability warning and never as a failed save.  Absent or
    # attempted=False means no marine service is configured — say nothing.
    marine_config_push: dict[str, Any] | None = None
    if apply_response and isinstance(apply_response, dict):
        push_raw = apply_response.get("marine_config_push")
        if isinstance(push_raw, dict):
            marine_config_push = push_raw

    # Resolve any unresolved imported images via the API (ADR-043).
    # The API is confirmed connected at this point (apply succeeded above).
    if state.imported_images:
        unresolved_imgs = {
            k: v for k, v in state.imported_images.items()
            if v.get("status") == "unresolved"
        }
        if unresolved_imgs and _config_dir is not None:
            from weewx_clearskies_config.wizard.image_import import resolve_images_api
            try:
                img_client = _get_api_client(state)
                updated_imgs = resolve_images_api(
                    state.imported_images,
                    state.source_skin or "Belchertown",
                    _config_dir / "branding",
                    img_client,
                )
                state.imported_images = updated_imgs
            except Exception:  # noqa: BLE001
                logger.warning("Image API resolution failed", exc_info=True)

    # ------------------------------------------------------------------
    # Pre-flight permission check: verify every write target is accessible
    # BEFORE any file is written.  A permission failure here means no files
    # have been touched yet, so the operator sees a clean actionable error
    # rather than a partial-write state.
    # ------------------------------------------------------------------
    _effective_config_dir_pre = _config_dir or Path("/etc/weewx-clearskies")
    _write_targets: list[str] = [
        str(_effective_config_dir_pre / "webcam.json"),
        str(_effective_config_dir_pre / "branding.json"),
        str(_effective_config_dir_pre / "stack.conf"),
        str(_effective_config_dir_pre / "secrets.env"),
        str(_effective_config_dir_pre / "bootstrap-summary.md"),
    ]
    if state.custom_terms_md:
        _write_targets.append(str(_effective_config_dir_pre / "content" / "terms.md"))
    if state.custom_privacy_md:
        _write_targets.append(str(_effective_config_dir_pre / "content" / "privacy.md"))
    if state.about_content:
        _write_targets.append(str(_effective_config_dir_pre / "content" / "about.md"))

    _perm_failed = _check_write_permissions(_write_targets)
    if _perm_failed:
        try:
            _proc_user = getpass.getuser()
        except Exception:  # noqa: BLE001
            _proc_user = "clearskies"
        _failed_list = "\n".join(f"  • {p}" for p in _perm_failed)
        _perm_error = (
            "API configuration saved successfully.\n"
            "Cannot write local config files — permission denied for:\n"
            f"{_failed_list}\n\n"
            "Fix: Run these commands on the server, then click Apply again:\n"
            f"  sudo chown -R {_proc_user}:{_proc_user} {_effective_config_dir_pre}/\n"
            f"  sudo chmod 750 {_effective_config_dir_pre}/"
        )
        logger.error(
            "wizard_apply: pre-flight permission check failed for %d path(s): %s",
            len(_perm_failed),
            ", ".join(_perm_failed),
        )
        return _render(
            request,
            "step_review.html",
            {"step": 15, "state": state, "error": _perm_error},
            status_code=422,
        )

    # Write webcam config as a static JSON file for the dashboard.
    # This is a UI concern — the API does not manage webcam settings.
    webcam_config = {
        "enabled": state.webcam_enabled,
        "imageUrl": state.webcam_image_url,
        "videoUrl": state.webcam_video_url,
        "refreshInterval": state.webcam_refresh_interval,
    }
    webcam_json_path = (_config_dir or Path("/etc/weewx-clearskies")) / "webcam.json"
    try:
        with open(webcam_json_path, "w") as f:
            json.dump(webcam_config, f, indent=2)
        logger.info("Wrote webcam config to %s", webcam_json_path)
    except OSError:
        logger.warning(
            "Failed to write webcam config to %s — the dashboard webcam card "
            "will not appear until this file is created manually.",
            webcam_json_path,
            exc_info=True,
        )

    # Write branding.json as a static file for the dashboard (ADR-022 amendment).
    # Caddy serves this at /branding.json; the dashboard fetches it directly.
    try:
        write_branding_json(state, _config_dir)
        logger.info("Wrote branding.json to %s", _config_dir)
    except OSError:
        logger.warning(
            "Failed to write branding.json to %s — the dashboard will use "
            "default branding until this file is created manually.",
            _config_dir,
            exc_info=True,
        )

    # Write policy override files if provided (T4.2).
    # /etc/weewx-clearskies/content/terms.md and privacy.md replace the default
    # templates on the dashboard's Legal page when present.
    _effective_config_dir = _config_dir or Path("/etc/weewx-clearskies")
    if state.custom_terms_md or state.custom_privacy_md:
        content_dir = _effective_config_dir / "content"
        try:
            os.makedirs(content_dir, exist_ok=True)
            if state.custom_terms_md:
                terms_path = content_dir / "terms.md"
                terms_path.write_text(state.custom_terms_md, encoding="utf-8")
                logger.info("Wrote custom terms.md to %s", terms_path)
            if state.custom_privacy_md:
                privacy_path = content_dir / "privacy.md"
                privacy_path.write_text(state.custom_privacy_md, encoding="utf-8")
                logger.info("Wrote custom privacy.md to %s", privacy_path)
        except OSError:
            logger.warning(
                "Failed to write policy override files to %s",
                content_dir,
                exc_info=True,
            )

    # Write About This Station content if provided (FIX-008).
    # /etc/weewx-clearskies/content/about.md is served by the dashboard's
    # About page when present; absent means the default template is used.
    if state.about_content:
        about_content_dir = _effective_config_dir / "content"
        try:
            os.makedirs(about_content_dir, exist_ok=True)
            about_path = about_content_dir / "about.md"
            about_path.write_text(state.about_content, encoding="utf-8")
            logger.info("Wrote about.md to %s", about_path)
        except OSError:
            logger.warning(
                "Failed to write about.md to %s",
                about_content_dir,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Step 2: Write local config files (stack.conf, secrets.env with
    # local secrets only).
    #
    # If this step fails, the API side is already done — its config was
    # consumed by the apply call above.  We return the *review page* (not
    # the completion page) so the operator can click Apply again after
    # fixing the permissions issue.  On retry the API may return 200
    # (idempotent) or 410 (already set up) — either way we can proceed to
    # the local write without re-doing the API call.
    # ------------------------------------------------------------------

    try:
        result = apply_wizard(state, _config_dir)
        # Save the final state rather than deleting it.  The progress file
        # (mode 0600) serves as a backup for session recovery and pre-populates
        # the wizard on the next re-run.  clear_wizard_state is intentionally not
        # called here so the operator's passwords and API keys survive a restart.
        save_wizard_state(session_id, state)
    except OSError as exc:
        try:
            _exc_user = getpass.getuser()
        except Exception:  # noqa: BLE001
            _exc_user = "clearskies"
        _exc_config_dir = _config_dir or Path("/etc/weewx-clearskies")
        local_error = _(
            "API configuration saved successfully. "
            "Local config write failed for: {failed_path}.\n"
            "The API is configured but local files are out of sync.\n"
            "Fix permissions and click Apply again to write local files.\n\n"
            "Fix: sudo chown -R {user}:{user} {config_dir}/\n"
            "     sudo chmod 750 {config_dir}/"
        ).format(
            failed_path=exc.filename or _exc_config_dir,
            user=_exc_user,
            config_dir=_exc_config_dir,
        )
        logger.error("apply_wizard OSError: %s", exc)
        return _render(
            request,
            "step_review.html",
            {"step": 15, "state": state, "error": local_error},
            status_code=422,
        )
    except Exception:  # noqa: BLE001
        local_error = (
            "API configuration saved successfully. "
            "Something went wrong writing the local config files — "
            "check the server log for details, fix the issue, and click Apply again."
        )
        logger.exception("apply_wizard unexpected error")
        return _render(
            request,
            "step_review.html",
            {"step": 15, "state": state, "error": local_error},
            status_code=422,
        )

    # ------------------------------------------------------------------
    # Step 3: Trigger service restarts so the new config takes effect.
    #
    # API restart: POST /setup/restart.  The API may drop the connection
    # before the response completes (it exits and lets systemd restart it).
    # Both outcomes are treated as success.
    # ------------------------------------------------------------------
    api_restart_triggered = False
    try:
        restart_client = _get_api_client(state)
        # Pass the one-time restart_token so the API can authenticate the
        # restart request even on first-run, before the proxy secret has been
        # loaded into the running process's environment.
        api_restart_triggered = restart_client.restart(restart_token=restart_token)
    except Exception:  # noqa: BLE001
        logger.warning("wizard_apply: could not send restart request to API", exc_info=True)

    # Reload Caddy so it picks up the updated caddy.env (API URL may have changed).
    caddy_reloaded = False
    try:
        import subprocess

        subprocess.run(
            ["sudo", "systemctl", "reload", "caddy"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        caddy_reloaded = True
        logger.info("wizard_apply: Caddy reloaded to pick up caddy.env")
    except Exception:  # noqa: BLE001
        logger.warning("wizard_apply: could not reload Caddy", exc_info=True)

    assert _templates is not None
    return _templates.TemplateResponse(
        request=request,
        name="wizard/step_complete.html",
        context={
            "step": 12,
            "error": None,
            "result": result,
            "api_restart_triggered": api_restart_triggered,
            "imported_images": state.imported_images,
            "marine_config_push": marine_config_push,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Restart status polling endpoint
# ---------------------------------------------------------------------------


@router.get("/restart-status", response_class=HTMLResponse)
async def wizard_restart_status(request: Request) -> HTMLResponse:
    """Return an HTML fragment reporting the current state of the API service.

    Called repeatedly by HTMX on the completion page until the API is up.
    The fragment wraps its content in a ``<div class="all-done">`` only
    when the API is confirmed active, which is the condition the HTMX
    polling expression watches to stop polling.

    The API health check is unauthenticated (GET /health) so it works even
    after the setup session has expired.

    Poll count (T4.5): ``state.restart_poll_count`` tracks how many times
    this endpoint has been hit while the API was still down. Once it
    exceeds 150 (~5 minutes at the 2s poll interval), the fragment renders
    a timeout message with a Retry button instead of the spinner, wrapped
    in ``<div class="poll-timeout">`` — a second condition the HTMX
    polling expression watches to stop polling. ``?retry=1`` resets the
    counter so polling can resume.
    """
    session_id = _require_session(request)
    state = get_wizard_state(session_id)

    if request.query_params.get("retry"):
        state.restart_poll_count = 0

    api_up = False
    if state.api_address:
        try:
            client = ApiClient(state.api_address)
            api_up = client.health()
        except Exception:  # noqa: BLE001
            api_up = False

    if api_up:
        state.restart_poll_count = 0
    else:
        state.restart_poll_count += 1

    timed_out = (not api_up) and state.restart_poll_count > 150

    return _render(
        request,
        "restart_status_fragment.html",
        {
            "api_up": api_up,
            "poll_count": state.restart_poll_count,
            "timed_out": timed_out,
        },
    )


# ---------------------------------------------------------------------------
# Column mapping validation
# ---------------------------------------------------------------------------


def _validate_column_mapping(mapping: dict[str, str | None]) -> dict[str, str]:
    """Validate the column mapping submitted from step 2.

    Checks:
    - No two DB columns may map to the same canonical name (duplicate).
    - Each non-blank canonical name must exist in the known field registry.

    Returns a dict of ``{db_column_name: error_message}`` for each offending
    column.  An empty dict means the mapping is valid.
    """
    # Use the module-level comprehensive registry as the primary source.
    # Fall back to STOCK_COLUMN_MAP from the API package if loaded (adds any
    # newly promoted columns not yet reflected here), but the registry already
    # covers all canonical entities so the API import is supplementary only.
    valid_canonicals: set[str] = set(_ALL_CANONICAL_NAMES)
    try:
        from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP  # type: ignore[import-untyped]
        valid_canonicals |= set(STOCK_COLUMN_MAP.values())
    except Exception:  # noqa: BLE001
        # API package unavailable — the module-level registry is sufficient.
        pass

    errors: dict[str, str] = {}

    # Duplicate-canonical check: build reverse map of canonical → [db_col, ...]
    seen: dict[str, list[str]] = {}
    for db_col, canonical in mapping.items():
        if canonical:
            seen.setdefault(canonical, []).append(db_col)
    for canonical, db_cols in seen.items():
        if len(db_cols) > 1:
            for db_col in db_cols:
                errors[db_col] = f'"{canonical}" is already used by another column — each column must have a unique name.'

    # Unknown canonical name check
    if valid_canonicals:
        for db_col, canonical in mapping.items():
            if canonical and canonical not in valid_canonicals and db_col not in errors:
                errors[db_col] = f'"{canonical}" is not a recognised weewx field name. Check the spelling or leave blank to skip this column.'

    return errors


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


# Note: CANONICAL_FIELD_GROUPS, canonical_groups, and _ALL_CANONICAL_NAMES
# are defined in wizard/schema.py and imported at the top of this module.
# _validate_column_mapping uses _ALL_CANONICAL_NAMES imported from schema.


def _parse_int(value: str, *, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _existing_configs_present() -> bool:
    """Return True if stack.conf exists in config_dir (wizard has run before).

    api.conf is written by the API itself (ADR-038) and is not a reliable sentinel.
    realtime.conf is deprecated (ADR-058) but checked for backward compatibility.
    """
    if _config_dir is None:
        return False
    return (_config_dir / "stack.conf").exists() or (_config_dir / "realtime.conf").exists()


def _merge_from_existing_config(state: WizardState) -> None:
    """Merge fields from existing config files into *state* for any empty fields.

    Only fills fields that are still at their default/empty values so user edits
    made during the current wizard run are never overwritten.
    """
    if not _existing_configs_present():
        return
    from weewx_clearskies_config.wizard.state_persistence import populate_from_config
    assert _config_dir is not None
    try:
        existing = populate_from_config(_config_dir)
    except Exception:  # noqa: BLE001
        logger.warning("populate_from_config failed; skipping pre-populate", exc_info=True)
        return

    if state.db_kind == "mysql" and existing.db_kind != "mysql":
        state.db_kind = existing.db_kind
    if state.db_host is None and existing.db_host is not None:
        state.db_host = existing.db_host
    if state.db_port == 3306 and existing.db_port != 3306:
        state.db_port = existing.db_port
    if state.db_user is None and existing.db_user is not None:
        state.db_user = existing.db_user
    if state.db_password is None and existing.db_password is not None:
        state.db_password = existing.db_password
    if state.db_name == "weewx" and existing.db_name != "weewx":
        state.db_name = existing.db_name
    if not state.db_path and existing.db_path:
        state.db_path = existing.db_path

    if not state.column_mapping and existing.column_mapping:
        state.column_mapping = existing.column_mapping

    if state.station_name is None and existing.station_name is not None:
        state.station_name = existing.station_name
    if state.latitude is None and existing.latitude is not None:
        state.latitude = existing.latitude
    if state.longitude is None and existing.longitude is not None:
        state.longitude = existing.longitude
    if state.altitude_meters is None and existing.altitude_meters is not None:
        state.altitude_meters = existing.altitude_meters
    if state.altitude_unit == "meters" and existing.altitude_unit != "meters":
        state.altitude_unit = existing.altitude_unit
    if state.timezone is None and existing.timezone is not None:
        state.timezone = existing.timezone
    if state.default_locale == "en" and existing.default_locale != "en":
        state.default_locale = existing.default_locale

    if not state.providers and existing.providers:
        state.providers = existing.providers

    if not state.api_keys and existing.api_keys:
        state.api_keys = existing.api_keys

    if state.topology == "same-host" and existing.topology != "same-host":
        state.topology = existing.topology
    if state.proxy_secret is None and existing.proxy_secret is not None:
        state.proxy_secret = existing.proxy_secret

    if state.api_bind_host == "127.0.0.1" and existing.api_bind_host != "127.0.0.1":
        state.api_bind_host = existing.api_bind_host
    if state.api_bind_port == 8765 and existing.api_bind_port != 8765:
        state.api_bind_port = existing.api_bind_port
    if not state.webcam_enabled and existing.webcam_enabled:
        state.webcam_enabled = existing.webcam_enabled
    if state.webcam_image_url == "/webcam/weather_cam.jpg" and existing.webcam_image_url != "/webcam/weather_cam.jpg":
        state.webcam_image_url = existing.webcam_image_url
    if state.webcam_video_url == "/webcam/weewx_timelapse.mp4" and existing.webcam_video_url != "/webcam/weewx_timelapse.mp4":
        state.webcam_video_url = existing.webcam_video_url
    if state.webcam_refresh_interval == 60 and existing.webcam_refresh_interval != 60:
        state.webcam_refresh_interval = existing.webcam_refresh_interval

    if not state.site_title and existing.site_title:
        state.site_title = existing.site_title
    if not state.copyright_entity and existing.copyright_entity:
        state.copyright_entity = existing.copyright_entity
    if not state.logo_light_url and existing.logo_light_url:
        state.logo_light_url = existing.logo_light_url
    if not state.logo_dark_url and existing.logo_dark_url:
        state.logo_dark_url = existing.logo_dark_url
    if not state.logo_alt and existing.logo_alt:
        state.logo_alt = existing.logo_alt
    if not state.favicon_url and existing.favicon_url:
        state.favicon_url = existing.favicon_url
    if not state.custom_background_url and existing.custom_background_url:
        state.custom_background_url = existing.custom_background_url
    if not state.accent and existing.accent:
        state.accent = existing.accent
    if not state.default_theme_mode and existing.default_theme_mode:
        state.default_theme_mode = existing.default_theme_mode
    if not state.google_analytics_id and existing.google_analytics_id:
        state.google_analytics_id = existing.google_analytics_id
    if not state.privacy_regions and existing.privacy_regions:
        state.privacy_regions = existing.privacy_regions
    if state.earthquake_radius_km == 100.0 and existing.earthquake_radius_km != 100.0:
        state.earthquake_radius_km = existing.earthquake_radius_km
    if state.earthquake_min_magnitude == 2.0 and existing.earthquake_min_magnitude != 2.0:
        state.earthquake_min_magnitude = existing.earthquake_min_magnitude
    if state.earthquake_default_days == 7 and existing.earthquake_default_days != 7:
        state.earthquake_default_days = existing.earthquake_default_days

    if not state.custom_terms_md and existing.custom_terms_md:
        state.custom_terms_md = existing.custom_terms_md
    if not state.custom_privacy_md and existing.custom_privacy_md:
        state.custom_privacy_md = existing.custom_privacy_md
    if not state.about_content and existing.about_content:
        state.about_content = existing.about_content
    if not state.station_photo_url and existing.station_photo_url:
        state.station_photo_url = existing.station_photo_url
    if not state.station_photo_alt and existing.station_photo_alt:
        state.station_photo_alt = existing.station_photo_alt

    # AQI regional configuration (ADR-059)
    if state.aeris_aqi_filter == "airnow" and existing.aeris_aqi_filter != "airnow":
        state.aeris_aqi_filter = existing.aeris_aqi_filter
    if state.openmeteo_aqi_index == "us_aqi" and existing.openmeteo_aqi_index != "us_aqi":
        state.openmeteo_aqi_index = existing.openmeteo_aqi_index
    if state.iqair_aqi_scale == "us" and existing.iqair_aqi_scale != "us":
        state.iqair_aqi_scale = existing.iqair_aqi_scale

    # LibreWxR radar configuration
    if state.librewxr_endpoint == "https://api.librewxr.net" and existing.librewxr_endpoint != "https://api.librewxr.net":
        state.librewxr_endpoint = existing.librewxr_endpoint
    if not state.librewxr_bounds and existing.librewxr_bounds:
        state.librewxr_bounds = existing.librewxr_bounds

    # TLS configuration (step 14)
    if not state.tls_mode and existing.tls_mode:
        state.tls_mode = existing.tls_mode
    if not state.tls_domain and existing.tls_domain:
        state.tls_domain = existing.tls_domain
    if not state.tls_acme_email and existing.tls_acme_email:
        state.tls_acme_email = existing.tls_acme_email
    if not state.tls_dns_provider and existing.tls_dns_provider:
        state.tls_dns_provider = existing.tls_dns_provider
    if not state.tls_dns_api_token and existing.tls_dns_api_token:
        state.tls_dns_api_token = existing.tls_dns_api_token
    if not state.tls_cert_path and existing.tls_cert_path:
        state.tls_cert_path = existing.tls_cert_path
        state.tls_cert_uploaded = existing.tls_cert_uploaded
    if not state.tls_key_path and existing.tls_key_path:
        state.tls_key_path = existing.tls_key_path
        state.tls_key_uploaded = existing.tls_key_uploaded


def _merge_from_api_current_config(client: ApiClient, state: WizardState) -> None:
    """Call GET /setup/current-config and merge the response into *state*.

    Only used in re-run mode (proxy auth available).  The API response is
    authoritative for values it provides — it overwrites state fields that are
    still at their defaults.  Fields that the user has already filled in during
    this wizard session are not overwritten.

    Failures are caught and logged; a failed fetch does not abort the re-run —
    the wizard falls back to the existing per-step config-file reads.
    """
    try:
        config = client.get_current_config()
    except ApiClientError as exc:
        logger.warning("get_current_config failed (%s): %s", exc.status_code, exc.detail)
        return
    except Exception:  # noqa: BLE001
        logger.warning("get_current_config network error", exc_info=True)
        return

    if not isinstance(config, dict):
        logger.warning("get_current_config returned unexpected type: %s", type(config).__name__)
        return

    # --- Database ---
    db = config.get("database", {})
    if isinstance(db, dict):
        if state.db_kind == "mysql" and db.get("kind") and db["kind"] != "mysql":
            state.db_kind = str(db["kind"])
        if state.db_host is None and db.get("host"):
            state.db_host = str(db["host"])
        if state.db_port == 3306 and db.get("port"):
            try:
                state.db_port = int(db["port"])
            except (ValueError, TypeError):
                pass
        if state.db_user is None and db.get("user"):
            state.db_user = str(db["user"])
        if state.db_password is None and db.get("password"):
            state.db_password = str(db["password"])
        if state.db_name == "weewx" and db.get("name") and db["name"] != "weewx":
            state.db_name = str(db["name"])
        if not state.db_path and db.get("path"):
            state.db_path = str(db["path"])

    # --- Providers + API keys ---
    # Response: {"forecast": {"provider": "nws", "credentials": {...}}, ...}
    # Reverse mapping: API provider names → wizard provider IDs.  AQI and
    # alerts providers use a domain-suffixed ID in the wizard (e.g.
    # "aeris_aqi", "aeris_alerts") but the API stores the short canonical
    # name (e.g. "aeris").  Domain context resolves ambiguity (forecast
    # "aeris" stays "aeris"; AQI "aeris" → "aeris_aqi"; alerts "aeris" →
    # "aeris_alerts").
    _API_TO_WIZARD_AQI_MAP: dict[str, str] = {
        "aeris": "aeris_aqi",
        "openmeteo": "openmeteo_aqi",
        "openweathermap": "openweathermap_aqi",
    }
    _API_TO_WIZARD_ALERTS_MAP: dict[str, str] = {
        "nws": "nws_alerts",
        "aeris": "aeris_alerts",
        "openweathermap": "openweathermap_alerts",
    }
    api_providers = config.get("providers", {})
    if isinstance(api_providers, dict):
        merged_providers: dict[str, str] = dict(state.providers)
        merged_keys: dict[str, dict[str, str]] = dict(state.api_keys)
        for domain, pd in api_providers.items():
            if not isinstance(pd, dict):
                continue
            provider_id = str(pd.get("provider", "")).strip()
            if not provider_id:
                continue
            if domain == "aqi":
                provider_id = _API_TO_WIZARD_AQI_MAP.get(provider_id, provider_id)
            elif domain == "alerts":
                provider_id = _API_TO_WIZARD_ALERTS_MAP.get(provider_id, provider_id)
            # Only fill if the domain has no provider set yet in state.
            if domain not in merged_providers:
                merged_providers[domain] = provider_id
            # Merge credentials — map from the API response fields to the
            # wizard's internal api_keys format {provider_id: {field: value}}.
            creds_raw = pd.get("credentials", {})
            if isinstance(creds_raw, dict):
                existing_creds = dict(merged_keys.get(provider_id, {}))
                # API response uses field names that match the keys in
                # CurrentConfigProviderCredentials; map them to the wizard's
                # auth_fields naming (api_key, api_secret, pws_station_id, etc.)
                _FIELD_REMAP: dict[str, str] = {
                    "client_id": "client_id",             # Aeris
                    "client_secret": "client_secret",     # Aeris
                    "appid": "api_key",           # OpenWeatherMap
                    "api_key": "api_key",         # Wunderground primary
                    "pws_station_id": "pws_station_id",
                    "key": "api_key",             # IQAir
                }
                for resp_field, wizard_field in _FIELD_REMAP.items():
                    val = creds_raw.get(resp_field)
                    if val and not existing_creds.get(wizard_field):
                        existing_creds[wizard_field] = str(val)
                if existing_creds:
                    merged_keys[provider_id] = existing_creds
        state.providers = merged_providers
        state.api_keys = merged_keys

        # AQI regional configuration (ADR-059) — restore from API config on re-run.
        aqi_pd = api_providers.get("aqi", {})
        if isinstance(aqi_pd, dict):
            aqi_provider_name = str(aqi_pd.get("provider", "")).strip()
            if aqi_provider_name == "aeris":
                val = str(aqi_pd.get("aqi_filter", "")).strip()
                _VALID_AERIS = {"airnow", "china", "india", "eaqi", "caqi", "uk", "de", "cai"}
                if val in _VALID_AERIS and state.aeris_aqi_filter == "airnow":
                    state.aeris_aqi_filter = val
            elif aqi_provider_name == "openmeteo":
                val = str(aqi_pd.get("aqi_index", "")).strip()
                if val in {"us_aqi", "european_aqi"} and state.openmeteo_aqi_index == "us_aqi":
                    state.openmeteo_aqi_index = val
            elif aqi_provider_name in ("iqair", "iq_air"):
                val = str(aqi_pd.get("aqi_scale", "")).strip()
                if val in {"us", "cn"} and state.iqair_aqi_scale == "us":
                    state.iqair_aqi_scale = val

        # LibreWxR endpoint and bounds — restore from API config on re-run.
        radar_pd = api_providers.get("radar", {})
        if isinstance(radar_pd, dict):
            radar_provider_name = str(radar_pd.get("provider", "")).strip()
            if radar_provider_name == "librewxr":
                endpoint_val = str(radar_pd.get("librewxr_endpoint", "")).strip()
                if endpoint_val and state.librewxr_endpoint == "https://api.librewxr.net":
                    state.librewxr_endpoint = endpoint_val
                bounds_val = str(radar_pd.get("librewxr_bounds", "")).strip()
                if bounds_val and not state.librewxr_bounds:
                    state.librewxr_bounds = bounds_val

    # Marine service connection — restore from API current-config on re-run
    # (T7.2), so the operator never retypes an address or a secret the API
    # already holds.  The TLS flag is only adopted alongside the URL: a flag
    # applied on its own would silently overwrite a choice made this session.
    marine_url_val = config.get("marine_service_url")
    if marine_url_val and not state.marine_service_url:
        state.marine_service_url = str(marine_url_val).strip()
        verify_val = config.get("marine_verify_tls")
        if isinstance(verify_val, bool):
            state.marine_verify_tls = verify_val
    marine_secret_val = config.get("marine_service_secret")
    if marine_secret_val and not state.marine_service_secret:
        state.marine_service_secret = str(marine_secret_val).strip()

    # --- Station ---
    station = config.get("station", {})
    if isinstance(station, dict):
        if state.station_name is None and station.get("name"):
            state.station_name = str(station["name"])
        if state.latitude is None and station.get("latitude") is not None:
            state.latitude = _to_float(station["latitude"])
        if state.longitude is None and station.get("longitude") is not None:
            state.longitude = _to_float(station["longitude"])
        if state.altitude_meters is None and station.get("altitude_meters") is not None:
            raw_alt = _to_float(station["altitude_meters"])
            alt_unit = str(station.get("altitude_unit", "meter")).strip().lower()
            if raw_alt is not None and ("foot" in alt_unit or "feet" in alt_unit or alt_unit == "ft"):
                state.altitude_meters = raw_alt * 0.3048
                state.altitude_unit = "feet"
            else:
                state.altitude_meters = raw_alt
                state.altitude_unit = "meters"
        if state.timezone is None and station.get("timezone"):
            state.timezone = str(station["timezone"])
        if state.default_locale == "en" and station.get("default_locale"):
            locale_val = str(station["default_locale"]).strip()
            if locale_val in _VALID_LOCALES:
                state.default_locale = locale_val

    # --- Branding and Social media ---
    # Branding and social fields are no longer fetched from the API (ADR-022
    # amendment).  branding.json is now the authoritative source; pre-populate
    # is handled by populate_from_branding_json() in state_persistence.py.

    # --- Earthquake settings ---
    earthquakes = config.get("earthquakes", {})
    if isinstance(earthquakes, dict):
        if state.earthquake_radius_km == 100.0 and earthquakes.get("radius_km") is not None:
            try:
                state.earthquake_radius_km = float(earthquakes["radius_km"])
            except (ValueError, TypeError):
                pass
        if state.earthquake_min_magnitude == 2.0 and earthquakes.get("min_magnitude") is not None:
            try:
                state.earthquake_min_magnitude = float(earthquakes["min_magnitude"])
            except (ValueError, TypeError):
                pass
        if state.earthquake_default_days == 7 and earthquakes.get("default_days") is not None:
            try:
                days = int(earthquakes["default_days"])
                if days in (1, 7, 14, 30):
                    state.earthquake_default_days = days
            except (ValueError, TypeError):
                pass

    # --- Column mapping ---
    # API format: {canonical_name: db_column_name}
    # Wizard format (state.column_mapping): {db_column_name: canonical_name}
    # Only populate if state has no mapping yet (re-run scenario).
    if not state.column_mapping:
        api_col_mapping = config.get("column_mapping")
        if isinstance(api_col_mapping, dict) and api_col_mapping:
            state.column_mapping = {str(v): str(k) for k, v in api_col_mapping.items() if v}

    # --- Marine alert radius (ADR-089) ---
    # Restore marine_alert_radius_miles from the alerts provider config so the
    # wizard's alerts step pre-populates the marine radius field on re-run.
    alerts_pd = api_providers.get("alerts", {}) if isinstance(api_providers, dict) else {}
    if isinstance(alerts_pd, dict) and state.marine_alert_radius_miles == 0:
        raw_radius = alerts_pd.get("marine_alert_radius_miles")
        if raw_radius is not None:
            try:
                state.marine_alert_radius_miles = int(raw_radius)
            except (ValueError, TypeError):
                pass

    # --- Marine locations (T6.1/T6.3) ---
    # Restore marine_enabled + marine_locations from the API's [marine] config
    # so the wizard's marine step pre-populates on re-run.
    api_marine = config.get("marine")
    if isinstance(api_marine, dict) and not state.marine_locations:
        locations_raw = api_marine.get("locations", {})
        if isinstance(locations_raw, dict) and locations_raw:
            state.marine_enabled = True
            restored: dict[str, dict[str, Any]] = {}
            for loc_id, loc_data in locations_raw.items():
                if not isinstance(loc_data, dict):
                    continue
                entry: dict[str, Any] = {
                    "name": str(loc_data.get("name", loc_id)),
                    "lat": float(loc_data.get("lat", 0)),
                    "lon": float(loc_data.get("lon", 0)),
                    "activities": list(loc_data.get("activities", [])),
                }
                ndbc = loc_data.get("ndbc_station_ids")
                if isinstance(ndbc, list):
                    entry["ndbc_station_ids"] = [str(s) for s in ndbc]
                elif isinstance(ndbc, str):
                    entry["ndbc_station_ids"] = [s.strip() for s in ndbc.split(",") if s.strip()]
                coops = loc_data.get("coops_station_ids")
                if isinstance(coops, list):
                    entry["coops_station_ids"] = [str(s) for s in coops]
                elif isinstance(coops, str):
                    entry["coops_station_ids"] = [s.strip() for s in coops.split(",") if s.strip()]
                if loc_data.get("nws_marine_zone_id"):
                    entry["nws_marine_zone_id"] = str(loc_data["nws_marine_zone_id"])
                surf = loc_data.get("surf")
                if isinstance(surf, dict):
                    surf_copy = dict(surf)
                    # R2: normalize structures from ConfigObj dict-of-dicts
                    # ({"0": {...}, "1": {...}}) to the list-of-dicts shape
                    # state.marine_locations documents (state.py ~229-253)
                    # and step_marine.html's server-rendered loop requires
                    # (`for struct in surf.get("structures", [])`) — mirrors
                    # admin/routes.py's _marine_structures_list normalization.
                    # Unlike that function, coordinates is carried through
                    # UNPARSED (whatever raw value is on the key, typically
                    # the JSON string _build_marine_conf_section() writes)
                    # rather than requiring isinstance(list) -- the wizard's
                    # hidden input (step_marine.html) renders it verbatim and
                    # step_marine_post() parses it back with json.loads(), so
                    # the restore must round-trip the same string shape the
                    # form field would have produced, not a parsed list.
                    raw_structures = surf_copy.get("structures")
                    if isinstance(raw_structures, dict):
                        def _struct_sort_key(k: str) -> tuple[int, str]:
                            try:
                                return (0, "%020d" % int(k))
                            except (TypeError, ValueError):
                                return (1, str(k))
                        normalized_structures: list[dict[str, Any]] = []
                        for key in sorted(raw_structures, key=_struct_sort_key):
                            struct = raw_structures[key]
                            if not isinstance(struct, dict):
                                continue
                            struct_entry: dict[str, Any] = {
                                "type": struct.get("type", ""),
                                "material": struct.get("material", ""),
                                "length_m": struct.get("length_m"),
                                "bearing_degrees": struct.get("bearing_degrees"),
                                "distance_m": struct.get("distance_m"),
                            }
                            coords = struct.get("coordinates")
                            if coords:
                                struct_entry["coordinates"] = coords
                            normalized_structures.append(struct_entry)
                        surf_copy["structures"] = normalized_structures
                    elif not isinstance(raw_structures, list):
                        surf_copy.pop("structures", None)
                    # C9b: normalize directional_exposure to the list-of-checked-
                    # directions shape the wizard templates expect (matching
                    # admin's _marine_exposure_list tolerance for dict / bare-list
                    # / ConfigObj "DIR:true" on-disk shapes), and record whether
                    # the source config carried the key at all as an explicit
                    # override -- presentation-only. Without this, a dict shape
                    # like {"N": true, "SE": true, ...} rendered via `dir in
                    # exposure` (membership on dict keys) showed every direction
                    # checked regardless of its bool value. The wire shape sent by
                    # build_marine_payload() is unaffected -- it still keys off
                    # the normalized list being non-empty.
                    raw_exposure = surf_copy.get("directional_exposure")
                    is_override = raw_exposure is not None
                    if isinstance(raw_exposure, dict):
                        directions = [
                            d for d, v in raw_exposure.items()
                            if v is True or str(v).lower() == "true"
                        ]
                    else:
                        if raw_exposure is None:
                            raw_list: list[str] = []
                        elif isinstance(raw_exposure, list | tuple):
                            raw_list = [str(v).strip() for v in raw_exposure if str(v).strip()]
                        else:
                            text = str(raw_exposure).strip()
                            raw_list = [text] if text else []
                        directions = []
                        for item in raw_list:
                            if ":" in item:
                                dir_part, bool_part = item.split(":", 1)
                                if bool_part.strip().lower() == "true":
                                    directions.append(dir_part.strip())
                            else:
                                directions.append(item.strip())
                    normalized_directions = [
                        d for d in directions if d in _MARINE_VALID_EXPOSURE
                    ]
                    # Only set the key when non-empty -- matches every other
                    # write path's truthy gate (wizard/routes.py step_marine_post
                    # `if exposure: surf_cfg["directional_exposure"] = exposure`,
                    # admin's `if s.get("directional_exposure"):` in
                    # _build_marine_apply_payload). config_writer.py's
                    # build_marine_payload() branches on isinstance(list), not
                    # truthiness -- an explicit "present but empty" list here
                    # would make a restored-then-untouched Auto location send
                    # an empty-dict OVERRIDE on next apply. Leaving the key
                    # absent when empty keeps state.marine_locations in the
                    # same shape a fresh (never-restored) location would have.
                    if normalized_directions:
                        surf_copy["directional_exposure"] = normalized_directions
                    else:
                        surf_copy.pop("directional_exposure", None)
                    entry["surf"] = surf_copy
                    # Kept OUTSIDE "surf" (entry-level, not surf_copy) so that
                    # build_marine_payload()'s explicit key-by-key surf_out
                    # whitelist (config_writer.py) can never accidentally pick
                    # it up if that whitelist is ever changed to a broader
                    # copy -- matches admin/routes.py's _parse_marine_locations
                    # placement of the same field for the same reason.
                    entry["directional_exposure_is_override"] = is_override
                fishing = loc_data.get("fishing")
                if isinstance(fishing, dict):
                    entry["fishing"] = dict(fishing)
                beach_safety = loc_data.get("beach_safety")
                if isinstance(beach_safety, dict):
                    entry["beach_safety"] = dict(beach_safety)
                restored[str(loc_id)] = entry
            state.marine_locations = restored

    # --- Units (T2B-A.3) ---
    # Restore state.units from api.conf [units] so the unit step is pre-filled
    # on wizard re-run.  Only populate if state has no units set yet.
    if state.units is None:
        api_units = config.get("units")
        if api_units is not None:
            groups = api_units.get("groups")
            if groups:
                state.units = dict(groups)
            # Also restore imported_config units subsections for round-trip fidelity.
            if state.imported_config is None:
                state.imported_config = {}
            imp_units = state.imported_config.setdefault("units", {})
            sf = api_units.get("string_formats")
            if sf:
                imp_units["string_formats"] = dict(sf)
            lb = api_units.get("labels")
            if lb:
                imp_units["labels"] = dict(lb)
            ords = api_units.get("ordinates")
            if ords:
                imp_units["ordinates"] = {"directions": ords}


