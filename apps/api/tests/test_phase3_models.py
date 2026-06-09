"""The three Phase-3 ORM models mirror the migration 0011 schema."""

from sqlalchemy.dialects.postgresql import JSONB, UUID

from usan_api.db.models import CallbackRequest, FollowUpFlag, SmsMessage


def test_follow_up_flag_columns_and_table():
    assert FollowUpFlag.__tablename__ == "follow_up_flags"
    cols = FollowUpFlag.__table__.columns
    assert {
        "id",
        "call_id",
        "elder_id",
        "severity",
        "category",
        "reason",
        "status",
        "created_at",
    } <= set(cols.keys())
    assert cols["call_id"].foreign_keys.pop().ondelete == "CASCADE"
    assert not cols["severity"].nullable
    assert not cols["status"].nullable


def test_callback_request_columns_and_table():
    assert CallbackRequest.__tablename__ == "callback_requests"
    cols = CallbackRequest.__table__.columns
    assert {
        "id",
        "call_id",
        "elder_id",
        "requested_time_text",
        "requested_at",
        "notes",
        "status",
        "created_at",
    } <= set(cols.keys())
    assert not cols["requested_time_text"].nullable
    assert cols["requested_at"].nullable


def test_sms_message_columns_uuid_pk_and_defaults():
    assert SmsMessage.__tablename__ == "sms_messages"
    cols = SmsMessage.__table__.columns
    assert {
        "id",
        "call_id",
        "elder_id",
        "to_number",
        "template_key",
        "body",
        "status",
        "telnyx_message_id",
        "error",
        "sent_at",
        "created_at",
        "updated_at",
    } <= set(cols.keys())
    assert isinstance(cols["id"].type, UUID)
    assert isinstance(cols["error"].type, JSONB)
    assert cols["telnyx_message_id"].unique is True
    # UUID server default + updated_at onupdate (mirrors Call/Elder style).
    assert "gen_random_uuid" in str(cols["id"].server_default.arg)
    assert cols["updated_at"].onupdate is not None
