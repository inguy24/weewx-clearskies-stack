"""Guard tests for the admin Status page (B4, Marine Model Restoration Plan).

Regression guard only -- not evidence the live system works. Mocks
_get_api_client() so no real network call is ever made; the marine service
is never contacted directly, per ARCHITECTURE.md's "add-on reached only
through the API" invariant.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

import pytest


class _FakeResponse:
    """Minimal stand-in for httpx.Response -- only .json() is used."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeApiClient:
    """Stand-in for ApiClient -- only .health() and ._request() are used
    by the status page.
    """

    def __init__(
        self,
        *,
        api_healthy: bool = True,
        health_raises: bool = False,
        marine_payload: dict[str, Any] | None = None,
        request_raises: bool = False,
    ) -> None:
        self._api_healthy = api_healthy
        self._health_raises = health_raises
        self._marine_payload = marine_payload
        self._request_raises = request_raises

    def health(self) -> bool:
        if self._health_raises:
            raise RuntimeError("boom")
        return self._api_healthy

    def _request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
        assert method == "GET"
        assert path == "/setup/marine/health"
        if self._request_raises:
            raise RuntimeError("connection failed")
        assert self._marine_payload is not None
        return _FakeResponse(self._marine_payload)


_FULL_B3_PAYLOAD = {
    "reachable": True,
    "error": None,
    "health": {
        "status": "degraded",
        "version": "0.2.0",
        "last_run": "2026-07-27T10:00:00Z",
        "spots": ["huntington"],
        "run_in_progress": False,
        "reasons": ["invariant_3_fired", "bathymetry stale"],
        "inputs": {
            "ww3_boundary": {"available": True, "age_s": 120},
            "wind": {"available": True, "age_s": 30},
            "bathymetry": {"available": False, "age_s": 999999},
            "tide": {"available": True, "age_s": 5},
        },
        "invariants": {
            "fired_total": 2,
            "last_fired_at": "2026-07-27T09:59:00Z",
            "last_fired_names": ["invariant_3", "invariant_8"],
        },
    },
}

_PRE_B3_PAYLOAD = {
    "reachable": True,
    "error": None,
    "health": {
        "status": "ok",
        "version": "0.1.0",
        "last_run": "2026-07-27T10:00:00Z",
        "spots": ["huntington"],
        "run_in_progress": False,
    },
}


def _model_health_stage(*, state: str, reason: str, actual_start: str, actual_end: str) -> dict[str, Any]:
    return {
        "state": state,
        "reasonCodes": [reason] if reason else [],
        "observedAt": "2026-09-01T03:04:00Z",
        "attemptId": "attempt-full-2026-09-01T03:00:00Z",
        "coverage": {
            "requiredStart": "2026-09-01T03:00:00Z",
            "requiredEnd": "2026-09-01T04:00:00Z",
            "actualStart": actual_start,
            "actualEnd": actual_end,
            "complete": state == "ok",
        },
        "provenance": {
            "source": "NOAA-WW3-2026-09-01",
            "modelTime": "2026-09-01T03:00:00Z",
        },
        "output": {
            "artifact": f"/var/lib/marine/{reason or 'stage'}-artifact",
            "bytes": 4096,
            "hash": "sha256-stage-test",
            "published": state == "ok",
        },
    }


_MODEL_HEALTH_PAYLOAD = {
    "schemaVersion": 1,
    "overall": {"state": "degraded", "reasonCodes": ["ww3_horizon_waiting"]},
    "serving": {
        "state": "stale",
        "reasonCodes": ["ww3_horizon_waiting"],
        "attemptId": "attempt-full-2026-09-01T03:00:00Z",
        "selectedFullCycleId": "2026-09-01T03:00:00Z",
        "firstValidTime": "2026-09-01T03:00:00Z",
        "lastValidTime": "2026-09-04T03:00:00Z",
        "modelTime": "2026-09-01T03:00:00Z",
        "ageSeconds": 9876,
        "lastGoodFallback": True,
    },
    "attempts": {
        "active": {
            "attemptId": "attempt-full-2026-09-01T03:00:00Z",
            "kind": "full",
            "state": "running",
            "startedAt": "2026-09-01T03:00:01Z",
            "runtime": {
                "marinePackage": "marine-package-test-2026",
                "swanBinarySha256": "swan-binary-sha-test",
                "ww3PinIdentity": "ww3-pin-test",
                "modelConfigGeneration": "model-config-generation-test",
                "gridGeneration": "grid-generation-test",
            },
        },
        "latestByKind": {"full": None, "fast": None, "horizon": None, "recovery": None},
    },
    "stages": {
        "providerInputs": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "ww3Leg": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "ww3Horizon": _model_health_stage(
            state="blocked", reason="ww3_horizon_waiting", actual_start=None, actual_end=None,
        ),
        "boundaryMerge": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "swan": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "swellTrack": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "cache": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "publication": _model_health_stage(
            state="ok", reason="", actual_start="2026-09-01T03:00:00Z", actual_end="2026-09-01T04:00:00Z",
        ),
        "recovery": _model_health_stage(
            state="skipped", reason="topology_not_required", actual_start=None, actual_end=None,
        ),
    },
}


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeApiClient | None) -> None:
    import weewx_clearskies_config.admin.routes as routes

    monkeypatch.setattr(routes, "_get_api_client", lambda: client)


@pytest.mark.parametrize("path", ["/admin/status", "/admin/status/panel"])
def test_status_full_b3_payload_renders(authed_client, monkeypatch, path):
    """Full B3 payload: status/reasons/inputs/invariants all render."""
    client = _FakeApiClient(api_healthy=True, marine_payload=_FULL_B3_PAYLOAD)
    _patch_client(monkeypatch, client)

    resp = authed_client.get(path, headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "invariant_3_fired" in body
    assert "bathymetry stale" in body
    assert "ww3_boundary" in body
    assert "wind" in body
    assert "bathymetry" in body
    assert "tide" in body
    assert "invariant_3" in body
    assert "invariant_8" in body


def test_status_pre_b3_payload_does_not_crash(authed_client, monkeypatch):
    """Load-bearing case: only the five pre-B3 keys present.

    Must render a quiet 'not reported' note for reasons/inputs/invariants,
    never a KeyError, a blank panel, or a crash.
    """
    client = _FakeApiClient(api_healthy=True, marine_payload=_PRE_B3_PAYLOAD)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "not reported by this version" in body.lower()
    # Pre-B3 fields still present and rendered.
    assert "0.1.0" in body
    assert "2026-07-27T10:00:00Z" in body


@pytest.mark.parametrize(
    "error_string",
    [
        "Marine service is not configured",
        "Connection refused",
        "Connection timed out",
        "Connection failed: OSError",
        "Marine service returned HTTP 503",
        "Marine service returned a non-JSON response",
    ],
)
def test_status_marine_unreachable_shows_error_verbatim(authed_client, monkeypatch, error_string):
    """reachable=false: error string shown verbatim; page chrome + API health still render."""
    payload = {"reachable": False, "error": error_string, "health": None}
    client = _FakeApiClient(api_healthy=True, marine_payload=payload)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert error_string in body
    # The page's own chrome (breadcrumb/header) still renders.
    assert "Status" in body
    # API-health section still renders.
    assert "Reachable" in body or "Not reachable" in body


def test_status_api_client_unavailable(authed_client, monkeypatch):
    """No known API / no proxy secret -- _get_api_client() returns None.

    Page must still render its own chrome without crashing.
    """
    _patch_client(monkeypatch, None)

    resp = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "Status" in body
    assert "Cannot connect to the API" in body


def test_status_reasons_all_entries_shown_verbatim(authed_client, monkeypatch):
    """Every reasons entry appears -- not truncated, not collapsed to a count."""
    payload = {
        "reachable": True,
        "error": None,
        "health": {
            "status": "failed",
            "version": "0.2.0",
            "last_run": "2026-07-27T10:00:00Z",
            "spots": [],
            "run_in_progress": False,
            "reasons": [
                "reason_one_required_input_missing",
                "reason_two_invariant_4_fired",
                "reason_three_cycle_incomplete",
            ],
            "inputs": {},
            "invariants": {"fired_total": 0, "last_fired_at": None, "last_fired_names": []},
        },
    }
    client = _FakeApiClient(api_healthy=True, marine_payload=payload)
    _patch_client(monkeypatch, client)

    resp = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "reason_one_required_input_missing" in body
    assert "reason_two_invariant_4_fired" in body
    assert "reason_three_cycle_incomplete" in body


@pytest.mark.parametrize("path", ["/admin/status", "/admin/status/panel"])
def test_status_model_health_attempt_and_stage_matrix_render_opaque_evidence(
    authed_client, monkeypatch, path,
):
    """R8c: the operator view exposes model-health identity and evidence."""
    payload = {
        "reachable": True,
        "error": None,
        "health": {
            **_FULL_B3_PAYLOAD["health"],
            "modelHealth": _MODEL_HEALTH_PAYLOAD,
        },
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload=payload))

    resp = authed_client.get(path, headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    for token in (
        "attempt-full-2026-09-01T03:00:00Z",
        "running",
        "ww3Leg",
        "ww3Horizon",
        "boundaryMerge",
        "swellTrack",
        "publication",
        "ww3_horizon_waiting",
        "2026-09-01T03:00:00Z",
        "2026-09-04T03:00:00Z",
        "9876",
        "marine-package-test-2026",
        "swan-binary-sha-test",
        "ww3-pin-test",
        "model-config-generation-test",
        "grid-generation-test",
    ):
        assert token in body, token


def test_status_model_health_malformed_payload_renders_unavailable_fallback(
    authed_client, monkeypatch,
):
    """R8c: malformed opaque model health never crashes or renders as healthy."""
    payload = {
        "reachable": True,
        "error": None,
        "health": {
            **_FULL_B3_PAYLOAD["health"],
            "modelHealth": {"schemaVersion": "not-an-integer", "stages": []},
        },
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload=payload))

    resp = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = resp.text
    assert "Status" in body
    assert "model health" in body.lower()
    assert "unavailable" in body.lower() or "not reported by this version" in body.lower()


def test_status_model_health_uses_terminal_latest_attempt_when_no_active_attempt(
    authed_client, monkeypatch,
):
    """R8c: a completed latest-by-kind attempt drives the operator header."""
    payload = {
        "reachable": True,
        "error": None,
        "health": {
            **_FULL_B3_PAYLOAD["health"],
            "modelHealth": {
                **deepcopy(_MODEL_HEALTH_PAYLOAD),
                "attempts": {
                    "active": None,
                    "latestByKind": {
                        "full": {
                            "attemptId": "terminal-full-2026-09-01T03:00:00Z",
                            "kind": "full",
                            "state": "failed",
                            "startedAt": "2026-09-01T03:00:01Z",
                            "endedAt": "2026-09-01T03:04:00Z",
                            "reasonCodes": ["publication_refused"],
                            "runtime": {
                                "marinePackage": "terminal-marine-package",
                                "swanBinarySha256": "terminal-swan-sha",
                                "ww3PinIdentity": "terminal-ww3-pin",
                                "modelConfigGeneration": "terminal-model-generation",
                                "gridGeneration": "terminal-grid-generation",
                            },
                        },
                        "fast": None, "horizon": None, "recovery": None,
                    },
                },
            },
        },
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload=payload))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "terminal-full-2026-09-01T03:00:00Z" in body
    assert "publication_refused" in body
    assert "No active attempt" in body or "active" not in body.lower()


def test_status_model_health_renders_provider_and_swan_child_coverage(
    authed_client, monkeypatch,
):
    """R8c: child rows retain required/actual coverage and source evidence."""
    health = deepcopy(_FULL_B3_PAYLOAD["health"])
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health["stages"]["providerInputs"]["children"] = {
        "wcofsCurrents": {
            **_model_health_stage(
                state="ok", reason="", actual_start="2026-09-01T03:00:00Z",
                actual_end="2026-09-01T04:00:00Z",
            ),
            "provenance": {"source": "WCOFS-cycle-20260901", "modelTime": "2026-09-01T03:00:00Z"},
        },
    }
    model_health["stages"]["swan"]["children"] = {
        "l2": {
            **_model_health_stage(
                state="ok", reason="", actual_start="2026-09-01T03:00:00Z",
                actual_end="2026-09-01T04:00:00Z",
            ),
            "provenance": {"source": "SWAN-L2", "modelTime": "2026-09-01T03:00:00Z"},
        },
    }
    health["modelHealth"] = model_health
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status/panel", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    for token in (
        "wcofsCurrents", "WCOFS-cycle-20260901", "SWAN-L2",
        "requiredStart", "actualStart", "2026-09-01T04:00:00Z",
    ):
        assert token in body, token


def test_status_model_health_renders_compact_current_tail_provenance(
    authed_client, monkeypatch,
):
    """R8c: held-tail forcing stays distinct from a failed source gap."""
    health = {
        **_FULL_B3_PAYLOAD["health"],
        "modelHealth": deepcopy(_MODEL_HEALTH_PAYLOAD),
        "currentForcing": {
            "status": "available",
            "cycles": ["WCOFS-20260901T03Z"],
            "coverageStart": "2026-09-01T03:00:00Z",
            "coverageEnd": "2026-09-04T03:00:00Z",
            "fieldCount": 73,
            "heldTailHours": 3,
            "refusal": None,
            "updatedAt": "2026-09-01T03:04:00Z",
        },
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status/panel", headers={"HX-Request": "true"})

    assert response.status_code == 200
    for token in ("Current forcing", "WCOFS-20260901T03Z", "Held tail", "3"):
        assert token in response.text, token


def test_status_model_health_malformed_nested_evidence_is_safe_and_redacted(
    authed_client, monkeypatch,
):
    """R8c: malformed nested evidence cannot crash or leak URLs/paths."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health["stages"]["ww3Leg"].update({
        "reasonCodes": {"secret": "bad-shape"},
        "coverage": "bad-shape",
        "provenance": ["bad-shape"],
        "output": {
            "artifact": "/var/lib/weewx-clearskies/private.ww3",
            "bytes": "not-an-integer",
            "hash": "hash",
            "published": "not-a-boolean",
        },
    })
    model_health["attempts"]["active"]["runtime"] = ["bad-runtime-shape"]
    model_health["stages"]["ww3Horizon"]["provenance"] = {
        "sourceUrl": "https://user:password@private.example/forecast?token=secret-token",
    }
    health = {
        **_FULL_B3_PAYLOAD["health"],
        "modelHealth": model_health,
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "private.example" not in body
    assert "secret-token" not in body
    assert "/var/lib/weewx-clearskies/private.ww3" not in body
    assert "unavailable" in body.lower() or "not reported" in body.lower()


def test_status_component_matrix_marks_missing_revision_unknown(
    authed_client, monkeypatch,
):
    """R8c: absent authoritative deployment revision is displayed as unknown."""
    health = {
        **_FULL_B3_PAYLOAD["health"],
        "modelHealth": deepcopy(_MODEL_HEALTH_PAYLOAD),
        "componentMatrix": {
            "marine": {"reachable": True, "buildState": "ok", "revision": None},
            "api": {"reachable": True, "buildState": "ok", "revision": None},
            "dashboard": {"reachable": True, "buildState": "ok", "revision": None},
            "stack": {"reachable": True, "buildState": "ok", "revision": None},
        },
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "unknown" in body.lower()
    assert "marine" in body.lower()
    assert "dashboard" in body.lower()


def test_status_full_failure_remains_header_precedence_over_later_horizon_success(
    authed_client, monkeypatch,
):
    """R8c: a later horizon success cannot hide the failed full attempt."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health["overall"] = {"state": "failed", "reasonCodes": ["full_publication_failed"]}
    model_health["attempts"] = {
        "active": None,
        "latestByKind": {
            "full": {
                "attemptId": "full-failed-header-attempt",
                "kind": "full",
                "state": "failed",
                "startedAt": "2026-09-01T03:00:00Z",
                "endedAt": "2026-09-01T03:04:00Z",
                "reasonCodes": ["full_publication_failed"],
                "runtime": {},
            },
            "fast": None,
            "horizon": {
                "attemptId": "horizon-later-success-attempt",
                "kind": "horizon",
                "state": "ok",
                "startedAt": "2026-09-01T04:00:00Z",
                "endedAt": "2026-09-01T04:04:00Z",
                "reasonCodes": [],
                "runtime": {},
            },
            "recovery": None,
        },
    }
    health = {**_FULL_B3_PAYLOAD["health"], "modelHealth": model_health}
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "full-failed-header-attempt" in body
    assert "full_publication_failed" in body
    assert "horizon-later-success-attempt" in body
    assert "FAILED" in body or "failed" in body


@pytest.mark.parametrize(
    "malformed",
    [
        {"overall": {"reasonCodes": "not-a-list"}},
        {"serving": {"reasonCodes": {"unexpected": True}}},
    ],
)
def test_status_model_health_malformed_reason_codes_render_safe_fallback(
    authed_client, monkeypatch, malformed,
):
    """Malformed overall/serving reason lists never crash or look healthy."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health.update(malformed)
    health = {**_FULL_B3_PAYLOAD["health"], "modelHealth": model_health}
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status/panel", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "unavailable" in body.lower() or "unknown" in body.lower() or "not reported" in body.lower()


@pytest.mark.parametrize(
    "component_matrix",
    [
        {},
        {"marine": {"reachable": True, "buildState": "ok", "revision": None}},
    ],
)
def test_status_partial_or_absent_component_matrix_has_four_unknown_rows(
    authed_client, monkeypatch, component_matrix,
):
    """Missing deployment evidence leaves all four component rows unknown."""
    health = {
        **_FULL_B3_PAYLOAD["health"],
        "modelHealth": deepcopy(_MODEL_HEALTH_PAYLOAD),
        "componentMatrix": component_matrix,
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    for component in ("marine", "api", "dashboard", "stack"):
        assert component in body.lower(), component
    assert body.lower().count("unknown") >= 4


def test_status_model_health_source_fields_never_expose_url_path_or_query_token(
    authed_client, monkeypatch,
):
    """Source provenance is an identity, not a credential-bearing link/path."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health["stages"]["ww3Leg"]["provenance"] = {
        "sourceUrl": "https://operator:password@source.invalid/model?token=direct-secret",
        "sourcePath": "/srv/marine/private/ww3/restart.ww3",
    }
    health = {**_FULL_B3_PAYLOAD["health"], "modelHealth": model_health}
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "source.invalid" not in body
    assert "direct-secret" not in body
    assert "/srv/marine/private/ww3/restart.ww3" not in body


def test_status_help_defines_model_states_and_operator_actions(authed_client):
    """R8c: status help explains model-health states and immediate action."""
    response = authed_client.get("/admin/help/status")

    assert response.status_code == 200
    body = response.text.lower()
    for phrase in (
        "model health", "terminal attempt", "stage matrix", "component matrix",
        "waiting", "failed", "stale", "degraded", "action",
    ):
        assert phrase in body, phrase


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("overall", 7),
        ("overall", "not-a-list"),
        ("overall", {"unexpected": True}),
        ("serving", 7),
        ("serving", "not-a-list"),
        ("serving", {"unexpected": True}),
    ],
)
def test_status_model_health_reason_code_type_errors_are_unavailable(
    authed_client, monkeypatch, field, bad_value,
):
    """Malformed reason-code types never render as a healthy model state."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health[field]["reasonCodes"] = bad_value
    health = {**_FULL_B3_PAYLOAD["health"], "modelHealth": model_health}
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status/panel", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text.lower()
    assert "unavailable" in body or "unknown" in body or "not reported" in body


def test_status_absent_dashboard_component_is_unknown_not_no(
    authed_client, monkeypatch,
):
    """Missing dashboard deployment evidence is not falsely reported as No."""
    health = {
        **_FULL_B3_PAYLOAD["health"],
        "modelHealth": deepcopy(_MODEL_HEALTH_PAYLOAD),
        "componentMatrix": {},
    }
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    dashboard_row = re.search(r"dashboard.*?(?:unknown|Unknown).*?(?:</tr>|$)", body, re.IGNORECASE | re.DOTALL)
    assert dashboard_row is not None
    assert not re.search(r"dashboard.*?\bno\b.*?(?:</tr>|$)", body, re.IGNORECASE | re.DOTALL)


def test_status_provenance_keeps_safe_identifier_and_redacts_direct_sources(
    authed_client, monkeypatch,
):
    """Only a safe source identifier may reach the operator HTML."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health["stages"]["ww3Leg"]["provenance"] = {
        "source": "NOAA-WW3",
        "url": "https://operator:password@source.invalid/model?token=direct-secret",
        "path": "/srv/marine/private/ww3/restart.ww3",
        "query": "?api_key=direct-secret",
    }
    health = {**_FULL_B3_PAYLOAD["health"], "modelHealth": model_health}
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status/panel", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "NOAA-WW3" in body
    assert "source.invalid" not in body
    assert "direct-secret" not in body
    assert "/srv/marine/private/ww3/restart.ww3" not in body


@pytest.mark.parametrize(
    "unsafe_source",
    [
        "https://source.invalid/model?token=direct-secret",
        "operator:password@source.invalid",
        "/srv/marine/private/ww3/restart.ww3",
        r"C:\\secrets\\ww3\\restart.ww3",
    ],
)
def test_status_provenance_source_unsafe_variants_are_redacted(
    authed_client, monkeypatch, unsafe_source,
):
    """Direct provenance.source accepts only safe identifiers, never URL/path data."""
    model_health = deepcopy(_MODEL_HEALTH_PAYLOAD)
    model_health["stages"]["ww3Leg"]["provenance"] = {
        "source": unsafe_source,
        "sourceCycle": "NOAA-WW3-cycle-20260901",
    }
    health = {**_FULL_B3_PAYLOAD["health"], "modelHealth": model_health}
    _patch_client(monkeypatch, _FakeApiClient(api_healthy=True, marine_payload={
        "reachable": True, "error": None, "health": health,
    }))

    response = authed_client.get("/admin/status/panel", headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.text
    assert "NOAA-WW3-cycle-20260901" in body
    assert unsafe_source not in body


def test_status_requires_session(client):
    """Unauthenticated request is rejected.

    The app's global 401 handler (app.py) converts this into an HTMX
    redirect (200 + HX-Redirect header) rather than a raw 401 -- the same
    behaviour every other _require_session route exercises (verified here
    against /admin/marine-service, an existing route with an identical
    _require_session() guard).
    """
    resp = client.get("/admin/status", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers

    baseline = client.get("/admin/marine-service", headers={"HX-Request": "true"})
    assert baseline.status_code == 200
    assert "HX-Redirect" in baseline.headers
