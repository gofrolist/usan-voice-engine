"""The CustomVariable ORM model mirrors the migration 0015 schema."""

from usan_api.db.models import CustomVariable


def test_custom_variable_columns_and_defaults():
    assert CustomVariable.__tablename__ == "custom_variables"
    cols = CustomVariable.__table__.columns
    assert set(cols.keys()) == {
        "id",
        "name",
        "description",
        "example",
        "phi",
        "created_at",
        "updated_at",
        # Added by the TenantScoped mixin (migration 0032): every tenant-owned table
        # carries organization_id for RLS isolation.
        "organization_id",
    }
    assert not cols["name"].nullable
    # Per-org uniqueness (migration 0034): uniqueness moved from a single-column
    # unique to a composite UNIQUE(name, organization_id) in __table_args__.
    uniques = {
        tuple(c.name for c in con.columns)
        for con in CustomVariable.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("name", "organization_id") in uniques
    assert "false" in str(cols["phi"].server_default.arg)
    assert cols["description"].server_default is not None
    assert cols["example"].server_default is not None
    assert cols["updated_at"].onupdate is not None
    # The tenant column is NOT NULL and defaults from the request context (see
    # TenantScoped / migration 0032's COALESCE(... , default_org_id()) DEFAULT).
    assert not cols["organization_id"].nullable
    assert cols["organization_id"].server_default is not None
