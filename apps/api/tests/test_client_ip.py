from starlette.requests import Request

from usan_api.client_ip import client_ip


def _req(*, xff: str | None = None, client: tuple[str, int] | None = ("10.0.0.1", 1234)) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope: dict = {"type": "http", "headers": headers}
    if client is not None:
        scope["client"] = client
    return Request(scope)


def test_client_ip_uses_xff_first_hop():
    # Caddy sets X-Forwarded-For to the real client; honor its first hop over the peer.
    assert client_ip(_req(xff="203.0.113.7, 70.41.3.18")) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_without_xff():
    assert client_ip(_req(xff=None, client=("10.0.0.5", 5))) == "10.0.0.5"


def test_client_ip_falls_back_to_peer_when_first_hop_blank():
    # A leading-comma header (", 1.2.3.4") must not collapse into one shared "" bucket.
    assert client_ip(_req(xff=", 1.2.3.4", client=("10.0.0.9", 9))) == "10.0.0.9"


def test_client_ip_unknown_without_client_or_xff():
    assert client_ip(_req(xff=None, client=None)) == "unknown"
