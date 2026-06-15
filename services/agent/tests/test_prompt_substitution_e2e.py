# services/agent/tests/test_prompt_substitution_e2e.py
from datetime import datetime
from zoneinfo import ZoneInfo

from usan_agent.prompt_vars import build_vars, substitute


def test_greeting_and_system_prompt_render_end_to_end():
    resolved = {
        "first_name": "Margaret",
        "contact_name": "Margaret Doe",
        "call_direction": "outbound",
        "last_check_in": "on 2026-06-05, mood 4/5",
        "last_check_in_line": "For context, their last check-in was on 2026-06-05, mood 4/5.",
        "last_mood": "4",
        "last_pain": "2",
        "today_meds": "Lisinopril, Metformin",
    }
    now = datetime(2026, 6, 8, 13, 15, tzinfo=ZoneInfo("UTC"))
    values = build_vars(resolved, {}, timezone="US/Eastern", now=now)

    greeting = substitute("Good morning {{first_name}}! It is {{current_time}}.", values)
    assert greeting.startswith("Good morning Margaret!")
    assert "{{" not in greeting  # current_time resolved; no literal token remains

    system = substitute("Their meds today: {{today_meds}}. Unknown: {{not_a_var}}.", values)
    assert "Lisinopril, Metformin" in system
    assert "Unknown: ." in system  # unknown var -> empty string, never literal braces


def test_missing_first_name_falls_back_to_default():
    values = build_vars({}, {}, timezone="", now=datetime(2026, 6, 8, 9, 15))
    assert substitute("Hello {{first_name}}!", values) == "Hello there!"
