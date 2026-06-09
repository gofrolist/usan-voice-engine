def test_variable_catalog_requires_admin_session(client):
    # Mirrors the admin-profiles plane: no session cookie -> 401.
    r = client.get("/v1/admin/variable-catalog")
    assert r.status_code == 401


def test_variable_catalog_returns_ten_builtins_in_order(client, admin_session):
    r = client.get("/v1/admin/variable-catalog")
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["variables"]
    variables = body["variables"]
    assert [v["name"] for v in variables] == [
        "first_name",
        "elder_name",
        "call_direction",
        "current_time",
        "current_date",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
    ]


def test_variable_catalog_each_entry_has_contract_shape(client, admin_session):
    variables = client.get("/v1/admin/variable-catalog").json()["variables"]
    for v in variables:
        assert set(v.keys()) == {"name", "tier", "description", "default", "example", "phi"}
        assert v["tier"] == "builtin"
    by_name = {v["name"]: v for v in variables}
    assert by_name["first_name"]["default"] == "there"
    assert by_name["first_name"]["example"] == "Margaret"
    assert by_name["today_meds"]["default"] == ""


def test_variable_catalog_phi_field_values(client, admin_session):
    variables = client.get("/v1/admin/variable-catalog").json()["variables"]
    by_name = {v["name"]: v for v in variables}
    phi_names = {"last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"}
    for name, v in by_name.items():
        if name in phi_names:
            assert v["phi"] is True, f"{name} should have phi=True"
        else:
            assert v["phi"] is False, f"{name} should have phi=False"
