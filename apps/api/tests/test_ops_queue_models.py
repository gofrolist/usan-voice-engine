"""The ops-queue workflow ORM columns mirror the migration 0013 schema."""

import pytest
from sqlalchemy import Text

from usan_api.db.models import CallbackRequest, FollowUpFlag


@pytest.mark.parametrize("model", [FollowUpFlag, CallbackRequest])
def test_workflow_columns_on_both_models(model):
    cols = model.__table__.columns
    # NULL = never transitioned past 'open' (no backfill in 0013).
    assert "status_updated_at" in cols
    assert cols["status_updated_at"].nullable is True
    assert cols["status_updated_at"].type.timezone is True  # house DateTime(timezone=True)
    assert cols["status_updated_by"].nullable is True
    assert isinstance(cols["status_updated_by"].type, Text)  # admin actor email
