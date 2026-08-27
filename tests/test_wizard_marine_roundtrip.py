"""Wizard marine step GET-render round-trip regression net (ROUND WIZ-RT, R7).

Closes the coverage gap identified in
docs/planning/briefs/WIZARD-STUDY-AREA-RESET-INVESTIGATION-2026-08-03.md
§2 "Why CI is blind": tests/test_wizard_marine_structures.py exercises the
POST direction only, and the one existing GET-render test
(test_marine_exposure_override.py::test_wizard_marine_get_renders_override_location)
hand-seeds state with no ``structures`` key and float segment values -- the
shape the restore path *doesn't* produce. Neither path was covered.

Covers:
  (a) an api.conf-SHAPED [marine] section (ConfigObj dict-of-dicts
      structures, STRING numerics, coordinates as a JSON string) restored
      through the real _merge_from_api_current_config, then rendered by the
      real GET /wizard/marine route -- asserts 200, the restored segment
      values, and the coordinates hidden input (R2 dict->list normalization
      + R3 coordinates hidden input).
  (b) POSTing that same rendered-form shape back through /wizard/marine and
      asserting the built apply payload's segment + coordinates match the
      seed (full restore -> render -> resubmit -> payload round trip).
  (c) an empty-state, rerun-available GET falls back to the API merge (R1).
  (d)/(e) the apply payload's "swan" block is gated on
      state.swan_step_completed (2026-08-03 operator ruling, C9) -- (d) is
      a source-inspection KAT (wizard_apply is a large integration route
      with real network side effects, not practical to exercise end-to-end
      in this suite -- same documented rationale as
      test_wizard_earthquake_config.py::test_apply_payload_uses_default_radius_km_key
      (test_wizard_imagery.py's precedent of the same rationale was removed
      2026-08-27, M4-B, Q10-6 -- the imagery wizard fieldset it tested no
      longer exists));
      (e) is a route-level behavioral test of the flag itself.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from weewx_clearskies_config.wizard.config_writer import build_marine_payload
from weewx_clearskies_config.wizard.routes import _merge_from_api_current_config
from weewx_clearskies_config.wizard.state import get_wizard_state, save_wizard_state

# ---------------------------------------------------------------------------
# Fixtures / seed data
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for ApiClient -- get_current_config() returns a fixed dict,
    matching the pattern used by test_marine_exposure_override.py."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def get_current_config(self) -> dict[str, Any]:
        return self._config


_STRUCTURE_COORDS: list[list[float]] = [
    [-118.0067, 33.6553],
    [-118.0090, 33.6551],
    [-118.0113, 33.6549],
]
_STRUCTURE_COORDS_JSON = json.dumps(_STRUCTURE_COORDS)


def _seed_marine_config() -> dict[str, Any]:
    """api.conf-shaped [marine] section, as returned verbatim by
    GET /setup/current-config (weewx_clearskies_api/endpoints/setup.py:2842
    `marine_config = dict(marine_section)` -- a passthrough of the ConfigObj
    section, so structures are dict-of-dicts, numerics are strings, and
    coordinates is the JSON string _build_marine_conf_section() writes to
    disk, NOT a parsed list)."""
    return {
        "marine": {
            "locations": {
                "test-beach": {
                    "name": "Test Beach", "lat": "33.6553", "lon": "-118.0067",
                    "activities": ["surf"],
                    "surf": {
                        "segment_start_lat": "33.65", "segment_start_lon": "-118.00",
                        "segment_end_lat": "33.66", "segment_end_lon": "-118.01",
                        "bottom_type": "sand", "topographic_feature": "straight_beach",
                        "structures": {
                            "0": {
                                "type": "pier", "material": "permeable",
                                "length_m": "610.0", "bearing_degrees": "245.0",
                                "distance_m": "45.2",
                                "coordinates": _STRUCTURE_COORDS_JSON,
                            },
                        },
                    },
                },
            },
        },
        "database": {}, "station": {},
    }


# ---------------------------------------------------------------------------
# (a) restore -> real GET /wizard/marine round trip
# ---------------------------------------------------------------------------


def test_get_marine_step_renders_restored_segment_and_coordinates(authed_client):
    session_cookie = authed_client.cookies.get("clearskies_session")
    assert session_cookie
    state = get_wizard_state(session_cookie)

    # Real restore path (R2's fix lives here) -- not a hand-seeded shape.
    _merge_from_api_current_config(_FakeClient(_seed_marine_config()), state)
    save_wizard_state(session_cookie, state)

    # R2: structures must have been normalized to a list, not left as the
    # ConfigObj dict-of-dicts shape (that shape crashes the template, see
    # test_get_marine_step_... below via the route call itself).
    restored_surf = state.marine_locations["test-beach"]["surf"]
    assert isinstance(restored_surf["structures"], list)
    assert restored_surf["structures"][0]["coordinates"] == _STRUCTURE_COORDS_JSON

    resp = authed_client.get("/wizard/marine")
    assert resp.status_code == 200
    body = resp.text

    # Segment values pre-filled (already-working path, still ConfigObj strings).
    assert 'name="loc_0_surf_segment_start_lat" class="surf-segment-start-lat" value="33.65"' in body
    assert 'name="loc_0_surf_segment_start_lon" class="surf-segment-start-lon" value="-118.00"' in body
    assert 'name="loc_0_surf_segment_end_lat" class="surf-segment-end-lat" value="33.66"' in body
    assert 'name="loc_0_surf_segment_end_lon" class="surf-segment-end-lon" value="-118.01"' in body

    # R3: the coordinates hidden input, absent before this round, present
    # now with the JSON string rendered verbatim (no |tojson double-encode).
    assert 'name="loc_0_structure_0_coordinates"' in body
    assert f'value="{_STRUCTURE_COORDS_JSON}"' in body


# ---------------------------------------------------------------------------
# (b) restore -> render -> resubmit -> apply-payload round trip
# ---------------------------------------------------------------------------


def test_post_marine_step_rebuilt_from_restored_form_matches_seed(authed_client):
    session_cookie = authed_client.cookies.get("clearskies_session")
    assert session_cookie
    state = get_wizard_state(session_cookie)
    state.marine_service_url = "http://fake-marine.invalid:8780"  # T7.4 guard, routes.py:3139
    _merge_from_api_current_config(_FakeClient(_seed_marine_config()), state)
    save_wizard_state(session_cookie, state)

    # GET first -- confirms the values under test are the ones actually
    # rendered by the real template (not values invented independently of
    # the render path exercised in (a)).
    get_resp = authed_client.get("/wizard/marine")
    assert get_resp.status_code == 200
    body = get_resp.text
    assert 'value="33.65"' in body
    assert f'value="{_STRUCTURE_COORDS_JSON}"' in body

    # Resubmit the same values the rendered form carries.
    fields = {
        "marine_enabled": "1",
        "loc_0_name": "Test Beach",
        "loc_0_lat": "33.6553",
        "loc_0_lon": "-118.0067",
        "loc_0_activities": "surf",
        "loc_0_surf_segment_start_lat": "33.65",
        "loc_0_surf_segment_start_lon": "-118.00",
        "loc_0_surf_segment_end_lat": "33.66",
        "loc_0_surf_segment_end_lon": "-118.01",
        "loc_0_surf_bottom_type": "sand",
        "loc_0_surf_topographic_feature": "straight_beach",
        "loc_0_structure_0_type": "pier",
        "loc_0_structure_0_material": "permeable",
        "loc_0_structure_0_length_m": "610.0",
        "loc_0_structure_0_bearing_degrees": "245.0",
        "loc_0_structure_0_distance_m": "45.2",
        "loc_0_structure_0_coordinates": _STRUCTURE_COORDS_JSON,
    }
    post_resp = authed_client.post("/wizard/marine", data=fields)
    assert post_resp.status_code == 200

    state2 = get_wizard_state(session_cookie)
    surf = state2.marine_locations["test-beach"]["surf"]
    assert surf["segment_start_lat"] == 33.65
    assert surf["segment_start_lon"] == -118.00
    assert surf["segment_end_lat"] == 33.66
    assert surf["segment_end_lon"] == -118.01
    assert surf["structures"][0]["coordinates"] == _STRUCTURE_COORDS

    payload = build_marine_payload(state2)
    sent_surf = payload["locations"][0]["surf"]
    assert sent_surf["segment_start_lat"] == 33.65
    assert sent_surf["segment_start_lon"] == -118.00
    assert sent_surf["segment_end_lat"] == 33.66
    assert sent_surf["segment_end_lon"] == -118.01
    assert sent_surf["structures"][0]["coordinates"] == _STRUCTURE_COORDS
    assert json.loads(json.dumps(sent_surf["structures"][0]["coordinates"])) == _STRUCTURE_COORDS


# ---------------------------------------------------------------------------
# (c) empty state + rerun-available GET falls back to the API merge (R1)
# ---------------------------------------------------------------------------


def test_get_marine_step_falls_back_to_api_merge_when_empty_and_rerun_available(
    authed_client, monkeypatch, caplog
):
    import weewx_clearskies_config.wizard.routes as routes_mod

    session_cookie = authed_client.cookies.get("clearskies_session")
    assert session_cookie
    state = get_wizard_state(session_cookie)
    state.api_address = "https://fake-api.invalid:8765"
    save_wizard_state(session_cookie, state)
    assert state.marine_locations == {}

    monkeypatch.setattr(routes_mod, "_is_rerun_mode", lambda api_address: True)
    monkeypatch.setattr(
        routes_mod, "_get_api_client", lambda state: _FakeClient(_seed_marine_config())
    )

    with caplog.at_level("INFO", logger=routes_mod.logger.name):
        resp = authed_client.get("/wizard/marine")

    assert resp.status_code == 200
    state2 = get_wizard_state(session_cookie)
    assert "test-beach" in state2.marine_locations
    assert any(
        "fell back to API current-config merge" in r.getMessage() for r in caplog.records
    )


def test_get_marine_step_rerun_fallback_never_raises_when_client_unavailable(
    authed_client, monkeypatch
):
    """D2's fallback must never raise -- when the API client can't be built
    (e.g. proxy secret missing despite a pinned fingerprint), the form
    renders blank exactly as it does today, not a 500."""
    import weewx_clearskies_config.wizard.routes as routes_mod

    session_cookie = authed_client.cookies.get("clearskies_session")
    assert session_cookie
    state = get_wizard_state(session_cookie)
    state.api_address = "https://fake-api.invalid:8765"
    save_wizard_state(session_cookie, state)

    monkeypatch.setattr(routes_mod, "_is_rerun_mode", lambda api_address: True)

    def _raise_not_connected(state: Any) -> Any:
        raise ValueError("API not connected")

    monkeypatch.setattr(routes_mod, "_get_api_client", _raise_not_connected)

    resp = authed_client.get("/wizard/marine")
    assert resp.status_code == 200

    state2 = get_wizard_state(session_cookie)
    assert state2.marine_locations == {}


# ---------------------------------------------------------------------------
# (d)/(e) apply-payload "swan" block gated on swan_step_completed (C9)
# ---------------------------------------------------------------------------


def test_apply_payload_swan_gate_requires_step_completed_source():
    """Source-inspection KAT: wizard_apply() is a large integration route
    with real network side effects (client.apply(), local file writes) --
    not practical to exercise end-to-end in this suite, same rationale as
    test_wizard_earthquake_config.py's apply-payload tests
    (test_wizard_imagery.py carried the same rationale before it was
    removed 2026-08-27, M4-B, Q10-6). Pre-change, the swan-block condition
    has no reference to swan_step_completed anywhere in this bounded span,
    so this assertion is genuinely falsifiable against the prior
    unconditional-send behavior (C9)."""
    import weewx_clearskies_config.wizard.routes as routes_mod

    source = inspect.getsource(routes_mod.wizard_apply)
    start_marker = "# SWAN+TruShore nearshore model (T4.4)"
    end_marker = 'api_payload["swan"] = build_trushore_payload(state)'
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    swan_gate_block = source[start:end]
    assert "state.swan_step_completed" in swan_gate_block


def test_trushore_post_sets_swan_step_completed(authed_client):
    session_cookie = authed_client.cookies.get("clearskies_session")
    assert session_cookie
    state = get_wizard_state(session_cookie)
    assert state.swan_step_completed is False

    resp = authed_client.post("/wizard/trushore", data={})
    assert resp.status_code == 200

    state2 = get_wizard_state(session_cookie)
    assert state2.swan_step_completed is True
