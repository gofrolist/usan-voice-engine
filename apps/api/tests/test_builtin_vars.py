from datetime import UTC
from types import SimpleNamespace

from usan_api.builtin_vars import DATA_BUILTIN_NAMES, resolve_builtin_vars
from usan_api.schemas.variable_catalog import (
    BUILTIN_DEFAULTS,
    BUILTIN_NAMES,
    PHI_BUILTIN_NAMES,
)


def _elder(name="Margaret Doe", tz="US/Eastern", meds=None):
    meta = {}
    if meds is not None:
        meta["medication_schedule"] = meds
    return SimpleNamespace(name=name, timezone=tz, meta=meta)


def _log(mood=4, pain=2, notes=None, date_iso="2026-06-05"):
    from datetime import datetime

    return SimpleNamespace(
        mood=mood,
        pain_level=pain,
        notes=notes,
        logged_at=datetime.fromisoformat(f"{date_iso}T12:00:00+00:00").astimezone(UTC),
    )


def test_resolves_data_builtins_only_no_clock():
    resolved, tz = resolve_builtin_vars(_elder(), None, direction="outbound")
    assert set(resolved.keys()) == {
        "first_name",
        "elder_name",
        "contact_name",  # US4 alias of elder_name (FR-024)
        "call_direction",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
    }
    # current_time/current_date are agent-side only — never in resolved_vars.
    assert "current_time" not in resolved
    assert "current_date" not in resolved
    # Every resolved key is a catalog built-in.
    assert set(resolved.keys()) <= BUILTIN_NAMES


def test_first_name_is_first_token_and_elder_name_is_full():
    resolved, _ = resolve_builtin_vars(_elder(name="Margaret Anne Doe"), None, direction="inbound")
    assert resolved["first_name"] == "Margaret"
    assert resolved["elder_name"] == "Margaret Anne Doe"
    assert resolved["call_direction"] == "inbound"


def test_timezone_is_passed_through_from_elder():
    _, tz = resolve_builtin_vars(_elder(tz="US/Pacific"), None, direction="outbound")
    assert tz == "US/Pacific"


def test_wellness_fields_resolve_mood_pain_and_summary():
    resolved, _ = resolve_builtin_vars(_elder(), _log(mood=4, pain=2), direction="outbound")
    assert resolved["last_mood"] == "4"
    assert resolved["last_pain"] == "2"
    assert "mood 4/5" in resolved["last_check_in"]
    assert resolved["last_check_in_line"].startswith("For context, their last check-in was")
    assert "2026-06-05" in resolved["last_check_in_line"]


def test_no_wellness_log_leaves_wellness_fields_empty():
    resolved, _ = resolve_builtin_vars(_elder(), None, direction="outbound")
    assert resolved["last_mood"] == ""
    assert resolved["last_pain"] == ""
    assert resolved["last_check_in"] == ""
    assert resolved["last_check_in_line"] == ""


def test_today_meds_joins_schedule_names():
    meds = [{"name": "Lisinopril"}, {"name": "Metformin"}, {"dosage": "no-name"}]
    resolved, _ = resolve_builtin_vars(_elder(meds=meds), None, direction="outbound")
    assert resolved["today_meds"] == "Lisinopril, Metformin"


def test_today_meds_empty_when_no_schedule():
    resolved, _ = resolve_builtin_vars(_elder(meds=None), None, direction="outbound")
    assert resolved["today_meds"] == ""


def test_unknown_elder_inbound_resolves_to_call_direction_only():
    resolved, tz = resolve_builtin_vars(None, None, direction="inbound")
    assert resolved["call_direction"] == "inbound"
    assert resolved["first_name"] == ""
    assert resolved["elder_name"] == ""
    assert tz == ""


# ---------------------------------------------------------------------------
# US4 — contact_name builtin alias of elder_name (FR-024, T040–T042)
# ---------------------------------------------------------------------------


def test_contact_name_is_a_catalog_builtin_aliasing_elder_name():
    # contact_name is a permanent builtin alias: same tier, same default "there",
    # and (like elder_name) PHI-free so it can be spoken before identity confirm.
    assert "contact_name" in BUILTIN_NAMES
    assert BUILTIN_DEFAULTS["contact_name"] == "there"
    assert BUILTIN_DEFAULTS["contact_name"] == BUILTIN_DEFAULTS["elder_name"]
    assert "contact_name" not in PHI_BUILTIN_NAMES


def test_contact_name_resolves_identically_to_elder_name():
    # Same elder.name source → contact_name and elder_name carry the same value.
    resolved, _ = resolve_builtin_vars(_elder(name="Margaret Anne Doe"), None, direction="outbound")
    assert resolved["contact_name"] == "Margaret Anne Doe"
    assert resolved["contact_name"] == resolved["elder_name"]
    # contact_name is one of the data builtins the resolver emits.
    assert "contact_name" in DATA_BUILTIN_NAMES
    assert set(resolved.keys()) <= BUILTIN_NAMES


def test_contact_name_empty_for_unknown_caller_like_elder_name():
    resolved, _ = resolve_builtin_vars(None, None, direction="inbound")
    assert resolved["contact_name"] == ""
    assert resolved["contact_name"] == resolved["elder_name"]
