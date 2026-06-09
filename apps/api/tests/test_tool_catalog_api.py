def test_tool_catalog_requires_admin_session(client):
    # Mirrors the admin plane: no session cookie -> 401.
    r = client.get("/v1/admin/tool-catalog")
    assert r.status_code == 401


def test_tool_catalog_returns_seven_tools_in_order(client, admin_session):
    r = client.get("/v1/admin/tool-catalog")
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["tools"]
    assert [t["name"] for t in body["tools"]] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]


def test_tool_catalog_each_entry_has_contract_shape(client, admin_session):
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


def test_tool_catalog_response_matches_tool_names(client, admin_session):
    from usan_api.schemas.tool_catalog import TOOL_NAMES

    tools = client.get("/v1/admin/tool-catalog").json()["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES
