from usan_api.db.models import AdminUser, Membership


def test_admin_user_has_identity_fields_and_no_role():
    cols = AdminUser.__table__.c
    assert "is_super_admin" in cols
    assert "status" in cols
    assert "last_active_org_id" in cols
    assert "role" not in cols  # moved to Membership


def test_membership_composite_pk_and_role():
    pk = {c.name for c in Membership.__table__.primary_key.columns}
    assert pk == {"email", "organization_id"}
    assert "role" in Membership.__table__.c
