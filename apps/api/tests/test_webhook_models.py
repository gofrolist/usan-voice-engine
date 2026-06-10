"""The two outbound-webhook ORM models mirror the migration 0014 schema."""

from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from usan_api.db.models import WebhookDelivery, WebhookEndpoint


def test_webhook_endpoint_columns_and_defaults():
    assert WebhookEndpoint.__tablename__ == "webhook_endpoints"
    cols = WebhookEndpoint.__table__.columns
    assert {
        "id",
        "url",
        "description",
        "enabled",
        "secret",
        "events",
        "consecutive_failures",
        "disabled_reason",
        "created_at",
        "updated_at",
    } <= set(cols.keys())
    assert not cols["secret"].nullable
    assert not cols["events"].nullable
    assert isinstance(cols["events"].type, ARRAY)
    assert "true" in str(cols["enabled"].server_default.arg)
    assert "0" in str(cols["consecutive_failures"].server_default.arg)
    assert cols["description"].nullable
    assert cols["disabled_reason"].nullable
    assert cols["updated_at"].onupdate is not None


def test_webhook_delivery_columns_and_fk():
    assert WebhookDelivery.__tablename__ == "webhook_deliveries"
    cols = WebhookDelivery.__table__.columns
    # Read FK rules without mutating the shared Table metadata (no .pop()).
    assert next(iter(cols["endpoint_id"].foreign_keys)).ondelete == "CASCADE"
    assert not cols["endpoint_id"].nullable
    assert "'pending'" in str(cols["status"].server_default.arg)
    assert "0" in str(cols["attempts"].server_default.arg)
    assert isinstance(cols["payload"].type, JSONB)
    assert not cols["payload"].nullable
    assert not cols["next_attempt_at"].nullable
    assert cols["next_attempt_at"].server_default is not None
    assert cols["response_code"].nullable
    assert cols["last_error"].nullable
    assert cols["delivered_at"].nullable
    assert cols["updated_at"].onupdate is not None
