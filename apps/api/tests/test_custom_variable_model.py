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
    }
    assert not cols["name"].nullable
    assert cols["name"].unique is True
    assert "false" in str(cols["phi"].server_default.arg)
    assert cols["description"].server_default is not None
    assert cols["example"].server_default is not None
    assert cols["updated_at"].onupdate is not None
