"""Tool catalog API tests (Admin-UI Phase 3 design §4.1).

Covers GET /v1/admin/tool-catalog: admin-session gating, the closed 7-tool inventory
in catalog order, the per-entry contract shape (name/label/description/category plus
the always_on/requires_config gating flags), and that the response set matches
``TOOL_NAMES``. Mirrors test_variable_catalog_api.py.
"""

from usan_api.schemas.tool_catalog import TOOL_NAMES


def test_tool_catalog_returns_fifteen_tools_in_order(client, super_admin_acting_session):
    r = client.get("/v1/admin/tool-catalog")
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["tools"]
    assert [t["name"] for t in body["tools"]] == [
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


def test_tool_catalog_each_entry_has_contract_shape(client, super_admin_acting_session):
    tools = client.get("/v1/admin/tool-catalog").json()["tools"]
    for t in tools:
        assert set(t.keys()) == {
            "name",
            "label",
            "description",
            "category",
            "always_on",
            "requires_config",
        }
    by_name = {t["name"]: t for t in tools}
    assert by_name["end_call"]["always_on"] is True
    assert by_name["send_sms"]["requires_config"] is True
    assert by_name["send_sms"]["category"] == "messaging"
    assert by_name["flag_for_followup"]["category"] == "safety"


def test_tool_catalog_response_matches_tool_names(client, super_admin_acting_session):
    tools = client.get("/v1/admin/tool-catalog").json()["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES
