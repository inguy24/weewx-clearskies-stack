"""Guard tests: the ADR-078 single-file geographic-features admin section is
removed (M5, ADR-078 Amendment 2).

Contract: docs/planning/briefs/M5-ADR078-REMOVAL-BRIEF-2026-08-27.md "Lead
mechanics" item 3 (test-author's assertion list, stack side). Module under
test (pre-change): weewx_clearskies_config/admin/routes.py
(`_fetch_geographic_features_status()`, the `_CUSTOM_SECTIONS` landing card
entry with `section_id == "geographic-features"`, the landing status rows,
the two routes `GET /admin/geographic-features` / `POST
/admin/geographic-features/update`), weewx_clearskies_config/templates/
admin/geographic_features.html, and the `help.admin.geographic_features.*`
keys in every translations/*.json locale file.

Pins the four assertions named in the brief item 3 (stack side):
  1. `GET /admin/geographic-features` -> 404 for an authed client.
  2. The landing section list (`_CUSTOM_SECTIONS`, the real module-level
     list that drives the admin landing page — not a synthetic stand-in)
     has no entry with `section_id == "geographic-features"`.
  3. No locale file under weewx_clearskies_config/translations/*.json
     contains the `help.admin.geographic_features` key prefix (checked
     against every one of the 13 real locale files present in this tree,
     not a subset).
  4. `GET /admin/basemap` still renders (200) — the M1 basemap section is
     untouched by this removal (brief §STAYS).

PRE-CHANGE EVIDENCE: this file was authored and run BEFORE m5-api/m5-stack
were told to start (per brief item 3). Live pytest transcript against clean
pre-change stack HEAD (d3d8bec) — a real run that happened, captured
verbatim below (not a reconstruction; full raw stdout also pasted into the
m5-test closeout report per rules/verification.md "A transcript is pasted
only from a run that happened"):

    $ .venv_local/Scripts/python.exe -m pytest tests/test_m5_geographic_features_removed.py -q -p no:cacheprovider
    FFF.FFFFFFFFFFFFF.                                                       [100%]
    FAILED ...::TestGeographicFeaturesRouteGone::test_get_geographic_features_returns_404
        assert 200 == 404   (no known API in fresh tmp config_dir -> error
        render, still 200 -- pre-change route is present and declines, not
        gone)
    FAILED ...::TestGeographicFeaturesRouteGone::test_post_geographic_features_update_returns_404
        assert 500 == 404   (error path renders status_code=500 per the
        route's own `status_code=500 if error else 200`; route is present)
    FAILED ...::TestLandingSectionListHasNoGeographicFeatures::test_custom_sections_has_no_geographic_features_entry
        AssertionError: 'geographic-features' in ['status', 'station-identity', ...]
    FAILED ...::TestNoLocaleFileHasGeographicFeaturesHelpKeys::test_locale_has_no_geographic_features_help_keys[de..zh-TW]
        (13 parametrized failures, one per locale file -- each has the
        three help.admin.geographic_features.{body,tip,title} keys)
    PASSED ...::TestNoLocaleFileHasGeographicFeaturesHelpKeys::test_thirteen_locale_files_present
        (fixture-shape sanity check; unaffected by the removal)
    PASSED ...::TestBasemapStillRenders::test_admin_basemap_get_still_200
        (untouched neighbor, brief §STAYS -- correctly green pre-change too)
    16 failed, 2 passed, 1 warning in 0.74s

16 of 18 assertions FAIL pre-change as required; the 2 PASSes are
by-design non-regression checks (locale-file-count sanity, and the
untouched basemap neighbor) that are expected to stay green on both sides
of the change. Exit code 1.

POST-CHANGE RUN (this file, against HEAD after m5-stack lands) is pasted
into the closeout report, not here (this docstring's guard-run evidence is
authored once, before the post-change run, per "guard must fail pre-change"
discipline).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_TRANSLATIONS_DIR = (
    Path(__file__).resolve().parent.parent
    / "weewx_clearskies_config"
    / "translations"
)


# ===========================================================================
# 1. GET /admin/geographic-features -> 404 for an authed client.
# ===========================================================================


class TestGeographicFeaturesRouteGone:
    def test_get_geographic_features_returns_404(self, authed_client) -> None:
        """Pre-change this route returns 200 with an error-state render (no
        known API configured in the fresh tmp config_dir -> `_get_api_client()`
        returns None -> the page renders 'Cannot connect to the API', still
        status 200) -- so this guard fails pre-change on the status code
        itself, not merely on route presence."""
        resp = authed_client.get("/admin/geographic-features")
        assert resp.status_code == 404

    def test_post_geographic_features_update_returns_404(self, authed_client) -> None:
        resp = authed_client.post("/admin/geographic-features/update")
        assert resp.status_code == 404


# ===========================================================================
# 2. Landing section list has no geographic-features entry.
# ===========================================================================


class TestLandingSectionListHasNoGeographicFeatures:
    def test_custom_sections_has_no_geographic_features_entry(self) -> None:
        from weewx_clearskies_config.admin.routes import _CUSTOM_SECTIONS

        section_ids = [s.get("section_id") for s in _CUSTOM_SECTIONS]
        assert "geographic-features" not in section_ids


# ===========================================================================
# 3. No locale file contains help.admin.geographic_features.*
# ===========================================================================


class TestNoLocaleFileHasGeographicFeaturesHelpKeys:
    def test_thirteen_locale_files_present(self) -> None:
        """Sanity check on the fixture shape itself: the brief names exactly
        13 translations/*.json files. If this count drifts, the loop below
        would silently check fewer files than the brief requires."""
        locale_files = sorted(_TRANSLATIONS_DIR.glob("*.json"))
        assert len(locale_files) == 13, (
            f"expected 13 locale files per brief, found {len(locale_files)}: "
            f"{[f.name for f in locale_files]}"
        )

    @pytest.mark.parametrize(
        "locale_name",
        [
            "de", "en", "es", "fil", "fr", "it", "ja", "nl",
            "pt-BR", "pt-PT", "ru", "zh-CN", "zh-TW",
        ],
    )
    def test_locale_has_no_geographic_features_help_keys(self, locale_name: str) -> None:
        path = _TRANSLATIONS_DIR / f"{locale_name}.json"
        assert path.exists(), f"expected locale file missing: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        offending = [k for k in data if k.startswith("help.admin.geographic_features")]
        assert offending == [], (
            f"{locale_name}.json still has geographic_features help keys: {offending}"
        )


# ===========================================================================
# 4. admin/basemap still renders (untouched neighbor, brief §STAYS).
# ===========================================================================


class TestBasemapStillRenders:
    def test_admin_basemap_get_still_200(self, authed_client) -> None:
        resp = authed_client.get("/admin/basemap", headers={"HX-Request": "true"})
        assert resp.status_code == 200
