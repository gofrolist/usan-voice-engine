from usan_api.invites import build_accept_error_url, build_accept_url


class _S:
    """Minimal Settings stand-in for the URL builders."""

    def __init__(self, admin_base_url=None, google_oauth_redirect_uri=None):
        self.admin_base_url = admin_base_url
        self.google_oauth_redirect_uri = google_oauth_redirect_uri


def test_accept_url_prefers_admin_base_url():
    s = _S(admin_base_url="https://admin.example.com")
    assert build_accept_url(s, "tok123") == (
        "https://admin.example.com/v1/auth/accept-invite?token=tok123"
    )


def test_accept_url_falls_back_to_oauth_redirect_origin():
    s = _S(google_oauth_redirect_uri="https://admin.example.com/v1/auth/callback")
    assert build_accept_url(s, "tok123") == (
        "https://admin.example.com/v1/auth/accept-invite?token=tok123"
    )


def test_accept_error_url():
    s = _S(admin_base_url="https://admin.example.com")
    assert build_accept_error_url(s, "mismatch") == (
        "https://admin.example.com/accept-invite?status=error&reason=mismatch"
    )
