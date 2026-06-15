from datetime import UTC
from types import SimpleNamespace

from usan_api.builtin_vars import DATA_BUILTIN_NAMES, resolve_builtin_vars
from usan_api.schemas.variable_catalog import (
    BUILTIN_DEFAULTS,
    BUILTIN_NAMES,
    PHI_BUILTIN_NAMES,
)


def _contact(name="Margaret Doe", tz="US/Eastern", meds=None):
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
    resolved, tz = resolve_builtin_vars(_contact(), None, direction="outbound")
    assert set(resolved.keys()) == {
        "first_name",
        "contact_name",
        "call_direction",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
        "open_family_tasks",  # US2 / FR-009
        "pending_med_reasks",  # US3 / FR-005
        "personal_facts",  # US4 / FR-024
        "last_call_summary",
        "open_plans",
        "important_dates",
        "survey_due",  # US6 / FR-032
    }
    # current_time/current_date are agent-side only — never in resolved_vars.
    assert "current_time" not in resolved
    assert "current_date" not in resolved
    # Every resolved key is a catalog built-in.
    assert set(resolved.keys()) <= BUILTIN_NAMES


def test_first_name_is_first_token_and_contact_name_is_full():
    resolved, _ = resolve_builtin_vars(
        _contact(name="Margaret Anne Doe"), None, direction="inbound"
    )
    assert resolved["first_name"] == "Margaret"
    assert resolved["contact_name"] == "Margaret Anne Doe"
    assert resolved["call_direction"] == "inbound"


def test_timezone_is_passed_through_from_contact():
    _, tz = resolve_builtin_vars(_contact(tz="US/Pacific"), None, direction="outbound")
    assert tz == "US/Pacific"


def test_wellness_fields_resolve_mood_pain_and_summary():
    resolved, _ = resolve_builtin_vars(_contact(), _log(mood=4, pain=2), direction="outbound")
    assert resolved["last_mood"] == "4"
    assert resolved["last_pain"] == "2"
    assert "mood 4/5" in resolved["last_check_in"]
    assert resolved["last_check_in_line"].startswith("For context, their last check-in was")
    assert "2026-06-05" in resolved["last_check_in_line"]


def test_no_wellness_log_leaves_wellness_fields_empty():
    resolved, _ = resolve_builtin_vars(_contact(), None, direction="outbound")
    assert resolved["last_mood"] == ""
    assert resolved["last_pain"] == ""
    assert resolved["last_check_in"] == ""
    assert resolved["last_check_in_line"] == ""


def test_today_meds_joins_schedule_names():
    meds = [{"name": "Lisinopril"}, {"name": "Metformin"}, {"dosage": "no-name"}]
    resolved, _ = resolve_builtin_vars(_contact(meds=meds), None, direction="outbound")
    assert resolved["today_meds"] == "Lisinopril, Metformin"


def test_today_meds_empty_when_no_schedule():
    resolved, _ = resolve_builtin_vars(_contact(meds=None), None, direction="outbound")
    assert resolved["today_meds"] == ""


def test_open_family_tasks_renders_passed_messages():
    # The caller queries open family tasks and passes the messages; the resolver joins
    # them into one prompt-ready string (US2 / FR-009, T030).
    resolved, _ = resolve_builtin_vars(
        _contact(),
        None,
        direction="outbound",
        open_family_tasks=["remind mom to drink water", "ask about her doctor visit"],
    )
    assert resolved["open_family_tasks"] == "remind mom to drink water; ask about her doctor visit"


def test_open_family_tasks_empty_when_none():
    resolved, _ = resolve_builtin_vars(_contact(), None, direction="outbound")
    assert resolved["open_family_tasks"] == ""


def test_pending_med_reasks_renders_passed_medication_names():
    # The caller queries pending medication reminders and passes the names; the resolver
    # comma-joins them (single tokens like today_meds) into the builtin (US3 / FR-005).
    resolved, _ = resolve_builtin_vars(
        _contact(),
        None,
        direction="outbound",
        pending_med_reasks=["Lisinopril", "Metformin"],
    )
    assert resolved["pending_med_reasks"] == "Lisinopril, Metformin"
    assert "pending_med_reasks" in DATA_BUILTIN_NAMES
    assert set(resolved.keys()) <= BUILTIN_NAMES


def test_pending_med_reasks_empty_when_none():
    resolved, _ = resolve_builtin_vars(_contact(), None, direction="outbound")
    assert resolved["pending_med_reasks"] == ""


def test_memory_builtins_render_passed_values():
    # The caller resolves memory from personal_facts/conversation_summaries and passes
    # ready strings; the resolver joins facts/plans with "; " (phrases) and dates with
    # ", " (short labels), like open_family_tasks vs today_meds (US4 / FR-024).
    resolved, _ = resolve_builtin_vars(
        _contact(),
        None,
        direction="outbound",
        personal_facts=["son Tom lives nearby", "walks every morning"],
        last_call_summary="Chatted about the garden.",
        open_plans=["water the roses", "call the pharmacy"],
        important_dates=["her birthday"],
    )
    assert resolved["personal_facts"] == "son Tom lives nearby; walks every morning"
    assert resolved["last_call_summary"] == "Chatted about the garden."
    assert resolved["open_plans"] == "water the roses; call the pharmacy"
    assert resolved["important_dates"] == "her birthday"
    for name in ("personal_facts", "last_call_summary", "open_plans", "important_dates"):
        assert name in DATA_BUILTIN_NAMES
    assert set(resolved.keys()) <= BUILTIN_NAMES


def test_memory_builtins_empty_when_none():
    resolved, _ = resolve_builtin_vars(_contact(), None, direction="outbound")
    assert resolved["personal_facts"] == ""
    assert resolved["last_call_summary"] == ""
    assert resolved["open_plans"] == ""
    assert resolved["important_dates"] == ""


def test_build_memory_params_windows_dates_and_excludes_them_from_facts():
    # build_memory_params projects important_date facts into important_dates only when
    # within ±1 day of today (month/day match), and never duplicates them into
    # personal_facts; latest summary feeds last_call_summary/open_plans.
    from datetime import UTC, datetime

    from usan_api.builtin_vars import build_memory_params

    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    facts = [
        SimpleNamespace(category="person", content="son Tom lives nearby", structured={}),
        SimpleNamespace(
            category="important_date",
            content="birthday",
            structured={"date": "1950-07-04", "label": "her birthday"},
        ),
        SimpleNamespace(
            category="important_date",
            content="anniversary",
            structured={"date": "2020-12-25", "label": "anniversary"},
        ),
    ]
    summary = SimpleNamespace(summary="Lovely chat.", open_plans=["call the pharmacy", 5, ""])
    params = build_memory_params(facts, summary, timezone="UTC", now=now)
    assert params["personal_facts"] == ["son Tom lives nearby"]
    assert params["important_dates"] == ["her birthday"]  # 07-04 in window; 12-25 not
    assert params["last_call_summary"] == "Lovely chat."
    assert params["open_plans"] == ["call the pharmacy"]  # non-strings dropped


def test_build_memory_params_empty_inputs():
    from datetime import UTC, datetime

    from usan_api.builtin_vars import build_memory_params

    params = build_memory_params([], None, timezone="", now=datetime(2026, 7, 4, tzinfo=UTC))
    assert params == {
        "personal_facts": [],
        "last_call_summary": None,
        "open_plans": [],
        "important_dates": [],
    }


def test_build_memory_params_year_boundary_wrap():
    # The ±1 window is built from real date arithmetic, so Dec 31 must include Jan 1
    # (next year) and the match is anniversary-style (year ignored).
    from datetime import UTC, datetime

    from usan_api.builtin_vars import build_memory_params

    fact = SimpleNamespace(
        category="important_date",
        content="nye",
        structured={"date": "1980-01-01", "label": "New Year"},
    )
    on_nye = build_memory_params(
        [fact], None, timezone="UTC", now=datetime(2026, 12, 31, 12, tzinfo=UTC)
    )
    assert on_nye["important_dates"] == ["New Year"]  # Jan 1 is within ±1 of Dec 31
    two_days_out = build_memory_params(
        [fact], None, timezone="UTC", now=datetime(2026, 12, 29, 12, tzinfo=UTC)
    )
    assert two_days_out["important_dates"] == []


def test_build_memory_params_leap_day_observed_in_non_leap_year():
    # A Feb-29 anniversary is observed on Feb-28 in non-leap years (else it would surface
    # only ~1 year in 4); in a leap year it matches the actual day.
    from datetime import UTC, datetime

    from usan_api.builtin_vars import build_memory_params

    fact = SimpleNamespace(
        category="important_date",
        content="leap birthday",
        structured={"date": "1952-02-29", "label": "leap bday"},
    )
    non_leap = build_memory_params(
        [fact], None, timezone="UTC", now=datetime(2027, 2, 28, 12, tzinfo=UTC)
    )
    assert non_leap["important_dates"] == ["leap bday"]
    leap = build_memory_params(
        [fact], None, timezone="UTC", now=datetime(2028, 2, 29, 12, tzinfo=UTC)
    )
    assert leap["important_dates"] == ["leap bday"]


def test_unknown_contact_inbound_resolves_to_call_direction_only():
    resolved, tz = resolve_builtin_vars(None, None, direction="inbound")
    assert resolved["call_direction"] == "inbound"
    assert resolved["first_name"] == ""
    assert resolved["contact_name"] == ""
    assert tz == ""


# ---------------------------------------------------------------------------
# US4 — contact_name builtin alias of contact_name (FR-024, T040–T042)
# ---------------------------------------------------------------------------


def test_contact_name_is_a_catalog_builtin_aliasing_contact_name():
    # contact_name is a permanent builtin alias: same tier, same default "there",
    # and (like contact_name) PHI-free so it can be spoken before identity confirm.
    assert "contact_name" in BUILTIN_NAMES
    assert BUILTIN_DEFAULTS["contact_name"] == "there"
    assert "contact_name" not in PHI_BUILTIN_NAMES


def test_contact_name_resolves_to_full_name():
    resolved, _ = resolve_builtin_vars(
        _contact(name="Margaret Anne Doe"), None, direction="outbound"
    )
    assert resolved["contact_name"] == "Margaret Anne Doe"
    # contact_name is one of the data builtins the resolver emits.
    assert "contact_name" in DATA_BUILTIN_NAMES
    assert set(resolved.keys()) <= BUILTIN_NAMES


def test_contact_name_empty_for_unknown_caller():
    resolved, _ = resolve_builtin_vars(None, None, direction="inbound")
    assert resolved["contact_name"] == ""
