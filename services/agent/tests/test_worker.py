from usan_agent.worker import CallMetadata, parse_metadata


def test_parse_metadata_outbound():
    raw = '{"call_id": "abc", "direction": "outbound", "dynamic_vars": {"name": "Ada"}}'
    md = parse_metadata(raw)
    assert md == CallMetadata(call_id="abc", direction="outbound", dynamic_vars={"name": "Ada"})


def test_parse_metadata_none_is_inbound():
    md = parse_metadata(None)
    assert md.call_id is None
    assert md.direction == "inbound"
    assert md.dynamic_vars == {}


def test_parse_metadata_empty_string_is_inbound():
    md = parse_metadata("")
    assert md.direction == "inbound"
    assert md.call_id is None


def test_parse_metadata_invalid_json_is_inbound():
    md = parse_metadata("not json")
    assert md.direction == "inbound"
    assert md.call_id is None
    assert md.dynamic_vars == {}
