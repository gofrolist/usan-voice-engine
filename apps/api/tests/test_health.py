def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Build provenance is surfaced for ops/monitoring; defaults to "dev" when the
    # image build-args are absent (local/uncontainerized test run).
    assert body["version"] == "dev"
    assert body["git_sha"] == "dev"
