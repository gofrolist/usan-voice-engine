from usan_api.schemas.agent_config import TOOL_NAMES
from usan_api.schemas.tool_catalog import TOOL_CATALOG
from usan_api.schemas.tool_catalog import TOOL_NAMES as CATALOG_TOOL_NAMES


def test_tool_names_equals_catalog_names():
    catalog_names = {t.name for t in TOOL_CATALOG}
    assert catalog_names == TOOL_NAMES
    assert catalog_names == CATALOG_TOOL_NAMES


def test_catalog_has_exactly_fifteen_in_order():
    assert [t.name for t in TOOL_CATALOG] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "raise_crisis",
        "schedule_callback",
        "close_family_task",
        "record_personal_fact",
        "record_survey",
        "get_activity",
        "send_sms",
        "send_info_sms",
        "register_opt_out",
        "set_spanish_callback",
        "end_call",
    ]
