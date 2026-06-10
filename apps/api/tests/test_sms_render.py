from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from usan_api.sms_render import render_sms_body

_NOW = datetime(2026, 6, 9, 9, 15, 0, tzinfo=ZoneInfo("UTC"))


def _elder(name="Margaret Doe", tz="UTC", meds=None):
    return SimpleNamespace(name=name, timezone=tz, meta={"medication_schedule": meds or []})


def _call(direction="outbound"):
    return SimpleNamespace(direction=SimpleNamespace(value=direction))


def test_renders_non_phi_token():
    out = render_sms_body(
        "Hello {{first_name}}, from USAN.", call=_call(), elder=_elder(), now=_NOW
    )
    assert out == "Hello Margaret, from USAN."


def test_phi_token_renders_empty_defense_in_depth():
    # A PHI token would be hard-blocked at save; if one ever reaches render it
    # resolves to empty (the non-PHI subset drops PHI names).
    out = render_sms_body("Mood: {{last_mood}}.", call=_call(), elder=_elder(), now=_NOW)
    assert out == "Mood: ."


def test_today_meds_phi_token_renders_empty_defense_in_depth():
    # today_meds is PHI (variable_catalog) and is dropped by the non-PHI subset,
    # so {{today_meds}} resolves to empty even when meds are configured. The
    # save-time hard-block in _reject_phi_in_templates already prevents this body
    # from being stored; this guards the render path as defense-in-depth.
    # Dict-shaped entries so today_meds would resolve to a non-empty value if the
    # PHI filter were absent — proving the filter (not an empty source) blanks it.
    out = render_sms_body(
        "Meds: {{today_meds}}.",
        call=_call(),
        elder=_elder(meds=[{"name": "Lisinopril"}, {"name": "Metformin"}]),
        now=_NOW,
    )
    assert out == "Meds: ."


def test_unknown_token_renders_empty():
    out = render_sms_body("X {{not_a_var}} Y", call=_call(), elder=_elder(), now=_NOW)
    assert out == "X  Y"


def test_value_is_sanitized_before_insertion():
    # A name carrying braces / control chars / a brace-injection is neutralized.
    elder = _elder(name="Ann\n{{evil}}")
    out = render_sms_body("Hi {{first_name}}.", call=_call(), elder=elder, now=_NOW)
    assert "{" not in out
    assert "}" not in out
    assert "\n" not in out


def test_clock_tokens_resolve():
    # _NOW is 2026-06-09 09:15 UTC; %A, %B %-d in UTC -> "Tuesday, June 9".
    out = render_sms_body("Today is {{current_date}}.", call=_call(), elder=_elder(), now=_NOW)
    assert out == "Today is Tuesday, June 9."
