from usan_api.db.models import Call


def test_call_model_has_egress_id_column():
    assert "egress_id" in Call.__table__.columns
    assert Call.__table__.columns["egress_id"].nullable is True
