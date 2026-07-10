"""Tool catalog schema tests (Admin-UI Phase 3 design §4.1).

Locks the closed tool inventory: catalog order, per-tool category/gating flags,
and ``TOOL_NAMES`` as a frozenset of the catalog names.
"""

from usan_api.schemas.tool_catalog import TOOL_CATALOG, TOOL_NAMES


def test_catalog_has_exactly_fifteen_tools_in_order():
    names = [t.name for t in TOOL_CATALOG]
    assert names == [
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


def test_catalog_categories_and_flags():
    by_name = {t.name: t for t in TOOL_CATALOG}
    assert by_name["log_wellness"].category == "logging"
    assert by_name["log_medication"].category == "logging"
    assert by_name["get_today_meds"].category == "logging"
    assert by_name["flag_for_followup"].category == "safety"
    assert by_name["raise_crisis"].category == "safety"
    assert by_name["schedule_callback"].category == "safety"
    assert by_name["close_family_task"].category == "logging"
    assert by_name["record_survey"].category == "logging"
    assert by_name["get_activity"].category == "logging"
    assert by_name["send_sms"].category == "messaging"
    assert by_name["send_info_sms"].category == "messaging"
    assert by_name["register_opt_out"].category == "safety"
    assert by_name["set_spanish_callback"].category == "safety"
    assert by_name["end_call"].category == "lifecycle"
    # end_call is locked-on; send_sms needs >=1 template before it is offered.
    assert by_name["end_call"].always_on is True
    assert by_name["send_sms"].requires_config is True
    # Every other tool keeps the conservative defaults.
    for name, spec in by_name.items():
        if name != "end_call":
            assert spec.always_on is False
        if name != "send_sms":
            assert spec.requires_config is False


def test_tool_names_is_frozenset_of_catalog_names():
    assert isinstance(TOOL_NAMES, frozenset)
    assert {t.name for t in TOOL_CATALOG} == TOOL_NAMES
