"""WS-B: translate_general_tools — classify RetellAI general_tools into native tool config."""

from usan_api.compat.tool_translate import translate_general_tools

_PARAMS = {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}


def _custom(**over):
    base = {
        "type": "custom",
        "name": "schedule_callback",
        "description": "Schedule a callback.",
        "url": "https://client.example.com/functions/v1/schedule-callback",
        "method": "POST",
        "parameters": _PARAMS,
    }
    base.update(over)
    return base


def test_custom_tool_becomes_external_spec():
    out = translate_general_tools([_custom()])
    assert len(out.external_tools) == 1
    spec = out.external_tools[0]
    assert spec["name"] == "schedule_callback"
    assert spec["url"].startswith("https://")
    assert spec["method"] == "POST"
    assert spec["parameters"] == _PARAMS
    assert spec["timeout_s"] == 10.0


def test_flat_dashboard_shape_without_type_is_custom():
    entry = _custom()
    del entry["type"]
    out = translate_general_tools([entry])
    assert [s["name"] for s in out.external_tools] == ["schedule_callback"]


def test_end_call_builtin_maps_to_enable():
    out = translate_general_tools([{"type": "end_call", "name": "end_call"}])
    assert out.enable == ["end_call"]
    assert out.external_tools == []


def test_kb_lookup_placeholder_is_not_an_http_tool():
    kb = {
        "name": "kb_lookup",
        "url": "RETELL_BUILT_IN — if your KB is uploaded to Retell this is handled natively",
        "parameters": {"type": "object", "properties": {}},
    }
    out = translate_general_tools([kb])
    assert out.kb_lookup_present is True
    assert out.external_tools == []


def test_unknown_builtin_type_is_skipped():
    out = translate_general_tools([{"type": "transfer_call", "name": "xfer"}])
    assert out.external_tools == []
    assert out.enable == []
    assert "transfer_call" in out.skipped


def test_non_dict_and_missing_url_are_skipped():
    out = translate_general_tools(["oops", {"type": "custom", "name": "no_url"}])
    assert out.external_tools == []
    assert len(out.skipped) == 2


def test_timeout_ms_converted_and_clamped():
    assert translate_general_tools([_custom(timeout_ms=2000)]).external_tools[0]["timeout_s"] == 2.0
    # Above the 30s cap clamps down; below the 1s floor clamps up.
    assert (
        translate_general_tools([_custom(timeout_ms=99000)]).external_tools[0]["timeout_s"] == 30.0
    )
    assert translate_general_tools([_custom(timeout_ms=100)]).external_tools[0]["timeout_s"] == 1.0


def test_method_normalized_and_defaulted():
    assert translate_general_tools([_custom(method="get")]).external_tools[0]["method"] == "GET"
    assert translate_general_tools([_custom(method="PATCH")]).external_tools[0]["method"] == "POST"
    no_method = _custom()
    del no_method["method"]
    assert translate_general_tools([no_method]).external_tools[0]["method"] == "POST"


def test_missing_description_falls_back_to_name():
    entry = _custom()
    del entry["description"]
    assert translate_general_tools([entry]).external_tools[0]["description"] == "schedule_callback"


def test_missing_parameters_defaults_to_empty_object_schema():
    entry = _custom()
    del entry["parameters"]
    params = translate_general_tools([entry]).external_tools[0]["parameters"]
    assert params == {"type": "object", "properties": {}}


def test_none_and_empty_are_noops():
    assert translate_general_tools(None).external_tools == []
    assert translate_general_tools([]).external_tools == []
