"""Tests for the admin "Basemap" section (MARINE-AND-MAPS-PLAN, task M1-STACK).

One "update basemap" action + status for the product basemap: three PMTiles
tiers (world/local/radar) extracted from OpenStreetMap data (Protomaps),
served by the API and consumed by every Clear Skies map surface.

Mirrors test_admin_status.py's pattern: monkeypatch admin.routes._get_api_client
with a fake client so no real network call is ever made -- the API is the only
channel this page ever talks to (no direct call to the marine service, per
directive 14 / PRIME DIRECTIVE 11 -- there is no operator-typed bounds/zoom
field on this page at all).

Covers:
  (a) GET renders the three tier rows from a mocked status
  (b) POST -> API 202 "started" -> started flash
  (c) POST -> API 409 "already_running" -> already-running flash
  (d) status.updating == True -> update button disabled + polling attribute present
  (e) status.last_error set -> alert shown
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeResponse:
    """Minimal stand-in for httpx.Response -- only .json() is used."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeBasemapApiClient:
    """Stand-in for ApiClient -- only ._request() is used by the basemap page."""

    def __init__(
        self,
        *,
        status_payload: dict[str, Any] | None = None,
        update_raises: Exception | None = None,
    ) -> None:
        self._status_payload = status_payload
        self._update_raises = update_raises

    def _request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
        if method == "GET" and path == "/api/v1/basemap/status":
            assert self._status_payload is not None
            return _FakeResponse(self._status_payload)
        if method == "POST" and path == "/setup/basemap/update":
            if self._update_raises is not None:
                raise self._update_raises
            return _FakeResponse({"status": "started"})
        raise AssertionError(f"unexpected request {method} {path}")


_TIER_WORLD = {
    "available": True,
    "size_bytes": 5_242_880,
    "updated_at": "2026-08-27T10:00:00Z",
    "bounds": [-180, -85, 180, 85],
    "minzoom": 0,
    "maxzoom": 6,
}
_TIER_LOCAL = {
    "available": True,
    "size_bytes": 524_288_000,
    "updated_at": "2026-08-27T10:05:00Z",
    "bounds": [-118.5, 33.0, -117.0, 34.2],
    "minzoom": 7,
    "maxzoom": 15,
}
_TIER_RADAR_NOT_EXTRACTED = {
    "available": False,
    "size_bytes": None,
    "updated_at": None,
    "bounds": None,
    "minzoom": None,
    "maxzoom": None,
}

_FULL_STATUS = {
    "world": _TIER_WORLD,
    "local": _TIER_LOCAL,
    "radar": _TIER_RADAR_NOT_EXTRACTED,
    "updating": False,
    "last_error": None,
    "last_started_at": None,
    "last_finished_at": "2026-08-27T10:06:00Z",
}


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeBasemapApiClient | None) -> None:
    import weewx_clearskies_config.admin.routes as routes

    monkeypatch.setattr(routes, "_get_api_client", lambda: client)


# ---------------------------------------------------------------------------
# (a) GET renders the three tier rows from a mocked status
# ---------------------------------------------------------------------------


def test_basemap_get_renders_three_tier_rows(authed_client, monkeypatch):
    client = _FakeBasemapApiClient(status_payload=_FULL_STATUS)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/basemap", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "World" in body
    assert "Local" in body
    assert "Radar" in body
    # World and local are available with a size; radar is not extracted.
    assert "5.0 MB" in body
    assert "500.0 MB" in body
    assert "Not extracted" in body
    # Zoom ranges render.
    assert "0" in body and "6" in body
    assert "7" in body and "15" in body


def test_basemap_get_api_unreachable_shows_status_unavailable(authed_client, monkeypatch):
    """No known API / no proxy secret -- _get_api_client() returns None.

    Page must render cleanly with the 'Status unavailable' path, never crash.
    """
    _patch_client(monkeypatch, None)

    resp = authed_client.get("/admin/basemap", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "Basemap" in body
    assert "Status unavailable" in body
    assert "Cannot connect to the API" in body


# ---------------------------------------------------------------------------
# (b) POST -> API 202 "started" -> started flash
# ---------------------------------------------------------------------------


def test_basemap_update_started_shows_started_flash(authed_client, monkeypatch):
    client = _FakeBasemapApiClient(status_payload=_FULL_STATUS)
    _patch_client(monkeypatch, client)

    resp = authed_client.post("/admin/basemap/update", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    assert "Basemap update started" in resp.text


# ---------------------------------------------------------------------------
# (c) POST -> API 409 "already_running" -> already-running flash
# ---------------------------------------------------------------------------


def test_basemap_update_already_running_shows_flash(authed_client, monkeypatch):
    from weewx_clearskies_config.wizard.api_client import ApiClientError

    client = _FakeBasemapApiClient(
        status_payload=_FULL_STATUS,
        update_raises=ApiClientError(409, "already_running"),
    )
    _patch_client(monkeypatch, client)

    resp = authed_client.post("/admin/basemap/update", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    assert "already running" in resp.text


def test_basemap_update_other_api_error_shows_error_path(authed_client, monkeypatch):
    """Any other error takes the existing error path (500), not the flash path."""
    from weewx_clearskies_config.wizard.api_client import ApiClientError

    client = _FakeBasemapApiClient(
        status_payload=_FULL_STATUS,
        update_raises=ApiClientError(500, "extract failed"),
    )
    _patch_client(monkeypatch, client)

    resp = authed_client.post("/admin/basemap/update", headers={"HX-Request": "true"})

    assert resp.status_code == 500
    assert "extract failed" in resp.text


def test_basemap_update_api_unreachable(authed_client, monkeypatch):
    _patch_client(monkeypatch, None)

    resp = authed_client.post("/admin/basemap/update", headers={"HX-Request": "true"})

    assert resp.status_code == 500
    assert "Cannot connect to the API" in resp.text


# ---------------------------------------------------------------------------
# (d) status.updating == True -> update button disabled + polling attribute present
# ---------------------------------------------------------------------------


def test_basemap_updating_disables_button_and_polls(authed_client, monkeypatch):
    status = dict(_FULL_STATUS)
    status["updating"] = True
    status["last_started_at"] = "2026-08-27T10:10:00Z"
    client = _FakeBasemapApiClient(status_payload=status)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/basemap", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "disabled" in body
    assert 'hx-trigger="every 10s"' in body
    assert "Update in progress" in body
    assert "2026-08-27T10:10:00Z" in body


def test_basemap_not_updating_button_enabled_no_poll(authed_client, monkeypatch):
    client = _FakeBasemapApiClient(status_payload=_FULL_STATUS)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/basemap", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "<button" in body and "disabled" not in body.split('id="basemap-update-btn"', 1)[1].split(">", 1)[0]
    assert 'hx-trigger="every 10s"' not in body


# ---------------------------------------------------------------------------
# (e) status.last_error set -> alert shown
# ---------------------------------------------------------------------------


def test_basemap_last_error_shown_as_alert(authed_client, monkeypatch):
    status = dict(_FULL_STATUS)
    status["last_error"] = "radar tier: pmtiles extract failed: disk full"
    client = _FakeBasemapApiClient(status_payload=status)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/basemap", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert 'role="alert"' in body
    assert "radar tier: pmtiles extract failed: disk full" in body


# ---------------------------------------------------------------------------
# Session guard (mirrors test_status_requires_session)
# ---------------------------------------------------------------------------


def test_basemap_requires_session(client):
    resp = client.get("/admin/basemap", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers
