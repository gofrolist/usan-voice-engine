from usan_api.schemas.agent_config import TOOL_NAMES
from usan_api.schemas.tool_catalog import TOOL_CATALOG
from usan_api.schemas.tool_catalog import TOOL_NAMES as CATALOG_TOOL_NAMES


def test_tool_names_equals_catalog_names():
    catalog_names = {t.name for t in TOOL_CATALOG}
    assert catalog_names == TOOL_NAMES
    assert catalog_names == CATALOG_TOOL_NAMES


def test_catalog_has_exactly_seven_in_order():
    assert [t.name for t in TOOL_CATALOG] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]
