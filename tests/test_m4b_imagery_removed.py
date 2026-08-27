"""Guard tests: the imagery provider machinery is removed (M4-B, Q10-6).

Plan MARINE-AND-MAPS-PLAN-2026-08-27.md §M4 round B; operator ruling "if we
dont need it then get rid of it" (Q10-6, 2026-08-27, recorded as PA9
extended). Pins the five assertions named in the M4-B brief §4:

  1. `GET /admin/config/api/imagery` -> 404 (unknown section).
  2. The admin section list (`_CUSTOM_SECTIONS`) has no `imagery-provider`
     section_id.
  3. `POST /wizard/step/6` with a stray `provider_imagery` field is
     accepted (200) and NOT stored in `state.providers`.
  4. `_SECTION_ALLOWED_KEYS` has no `("api", "imagery")` key.
  5. No locale file (`translations/*.json`, all 13) contains a
     `help.admin.imagery-provider*` or `help.wizard.imagery*` key.

PRE-CHANGE EVIDENCE: stack-side m4b-stack landed all 5 commits (72a76e4,
0c9a30a, b1a216c, aaa6889, d3da6ba) before I finished the API-repo half of
this round, so — same timing situation as the API repo, coordinator ruling
2026-08-27 ("A. Proceed") — this file's assertions are not backed by a
live pytest run of THIS file against clean pre-change HEAD (065ac62).
Pre-change behaviour is established by direct citation of
`git show 065ac62:<path>` content (verified against the actual pre-change
source below, not fabricated), corroborated by the aggregate baseline run
`46 passed, 1 warning in 3.50s` I captured at HEAD 065ac62 BEFORE any M4-B
commit landed (that run included `tests/test_admin_imagery.py` and
`tests/test_wizard_imagery.py` passing in full, which independently proves
the admin section and wizard field existed and worked pre-change):

  (1)/(4) `git show 065ac62:weewx_clearskies_config/admin/routes.py` —
      `_SECTION_META` contains `("api", "imagery", "Imagery Provider", ())`
      and `_SECTION_ALLOWED_KEYS` contains `("api", "imagery"):
      frozenset({"provider", "api_key"})`, so `("api","imagery") in
      _VALID_SECTIONS` is True pre-change — `GET
      /admin/config/api/imagery` would render the section (200), not 404;
      `TestAdminImagerySectionGone.test_get_admin_config_api_imagery_404`
      below would FAIL pre-change (200, not 404). `_SECTION_ALLOWED_KEYS`
      assertion in `TestSectionAllowedKeysHasNoImagery` would likewise
      FAIL pre-change (key present).
  (2) Same file — `_CUSTOM_SECTIONS` contains a dict with
      `"section_id": "imagery-provider"` (providers group) — the
      `TestAdminSectionListHasNoImageryProvider` assertion below would
      FAIL pre-change (found, not absent).
  (3) `git show 065ac62:weewx_clearskies_config/wizard/routes.py` —
      `step6_post()`'s domain loop is `for domain in ("forecast",
      "alerts", "aqi", "earthquakes", "radar", "imagery")` pre-change (six
      domains, "imagery" included) — a `provider_imagery` field IS read
      and stored into `state.providers["imagery"]`. The pre-deletion test
      `tests/test_wizard_imagery.py::test_step6_post_saves_imagery_provider_naip`
      asserted exactly this and passed in the 46-passed pre-change
      baseline run. `TestWizardStep6DropsStrayImageryField` below would
      FAIL pre-change (state.providers.get("imagery") == "naip", not
      None).
  (5) `git show 065ac62:weewx_clearskies_config/translations/en.json` —
      contains `"help.admin.imagery-provider.title"`,
      `"help.admin.imagery-provider.body"`, `"help.wizard.imagery.title"`,
      `"help.wizard.imagery.body"` keys.
      `TestNoLocaleCarriesImageryHelpKeys.test_en_json_has_no_imagery_help_keys`
      below would FAIL pre-change (keys present in en.json). The other 12
      locale files never had these specific keys even pre-change
      (pre-existing translation gap, confirmed by m4b-stack and
      independently verified here by parsing each file's JSON and
      checking for the exact key prefixes, not a bare substring match —
      several locale files legitimately use the English word "imagery" in
      unrelated strings, e.g. "radar imagery", "satellite imagery"), so
      those 12 sub-tests are non-discriminating passes both pre- and
      post-change — only the en.json sub-test is a real guard.

POST-CHANGE RUN (this file, against HEAD after d3da6ba landed) — pasted
into the closeout report, not here (this docstring is authored once,
before the post-change run, per "guard must fail pre-change" discipline).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ===========================================================================
# 1. GET /admin/config/api/imagery -> 404.
# ===========================================================================


class TestAdminImagerySectionGone:
    def test_get_admin_config_api_imagery_404(self, authed_client) -> None:
        resp = authed_client.get(
            "/admin/config/api/imagery", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 404


# ===========================================================================
# 2. Admin section list has no "imagery-provider" entry.
# ===========================================================================


class TestAdminSectionListHasNoImageryProvider:
    def test_custom_sections_has_no_imagery_provider_id(self) -> None:
        from weewx_clearskies_config.admin.routes import _CUSTOM_SECTIONS

        section_ids = {cs["section_id"] for cs in _CUSTOM_SECTIONS}
        assert "imagery-provider" not in section_ids


# ===========================================================================
# 3. POST /wizard/step/6 with a stray provider_imagery field: accepted,
#    not stored.
# ===========================================================================


class TestWizardStep6DropsStrayImageryField:
    def test_stray_provider_imagery_field_accepted_and_not_stored(
        self, authed_client
    ) -> None:
        from weewx_clearskies_config.wizard.state import get_wizard_state

        resp = authed_client.post(
            "/wizard/step/6", data={"provider_imagery": "naip"}
        )
        assert resp.status_code == 200

        session_cookie = authed_client.cookies.get("clearskies_session")
        assert session_cookie is not None
        state = get_wizard_state(session_cookie)
        assert state is not None
        assert "imagery" not in state.providers


# ===========================================================================
# 4. _SECTION_ALLOWED_KEYS has no ("api", "imagery") entry.
# ===========================================================================


class TestSectionAllowedKeysHasNoImagery:
    def test_section_allowed_keys_has_no_api_imagery(self) -> None:
        from weewx_clearskies_config.admin.routes import _SECTION_ALLOWED_KEYS

        assert ("api", "imagery") not in _SECTION_ALLOWED_KEYS


# ===========================================================================
# 5. No locale file carries help.admin.imagery-provider* / help.wizard.imagery*.
# ===========================================================================


def _translations_dir() -> Path:
    import weewx_clearskies_config

    return Path(weewx_clearskies_config.__file__).parent / "translations"


def _locale_files() -> list[Path]:
    return sorted(_translations_dir().glob("*.json"))


class TestNoLocaleCarriesImageryHelpKeys:
    def test_thirteen_locale_files_present(self) -> None:
        # Sanity check on the fixture itself — brief says "ALL 13
        # translations/*.json".
        assert len(_locale_files()) == 13

    @pytest.mark.parametrize("locale_path", _locale_files(), ids=lambda p: p.name)
    def test_locale_has_no_imagery_help_keys(self, locale_path: Path) -> None:
        with locale_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        offending = [
            key
            for key in data
            if key.startswith("help.admin.imagery-provider")
            or key.startswith("help.wizard.imagery")
        ]
        assert offending == [], f"{locale_path.name} carries stale keys: {offending}"
