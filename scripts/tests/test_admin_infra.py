"""Structural contract for the admin-UI infra/deploy wiring (Plan admin-5).

Runs in the `pytest (scripts)` CI job (Python 3.12 + pytest + pyyaml). Pins the
P5 invariants so a regression in the compose overlay, Caddyfile, CI workflow,
Terraform DNS, or env docs fails CI before it can reach the VM.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def _load_yaml(rel: str):
    return yaml.safe_load((ROOT / rel).read_text())


def _env_keys(env) -> set[str]:
    """Normalize a compose `environment:` (map or list form) to a set of keys."""
    if isinstance(env, dict):
        return set(env.keys())
    return {str(e).split("=", 1)[0] for e in (env or [])}


def test_admin_overlay_service_shape():
    doc = _load_yaml("infra/docker-compose.admin.yml")
    svc = doc["services"]["admin-ui"]
    assert "usan-admin-ui" in svc["image"]
    assert "${IMAGE_TAG" in svc["image"]  # explicit-tag required
    assert svc["pull_policy"] == "always"
    assert svc["logging"]["driver"] == "journald"
    assert svc["restart"] == "unless-stopped"
    # No published port: the edge Caddy reaches it on the bridge.
    assert "ports" not in svc


def test_admin_overlay_caddy_env():
    doc = _load_yaml("infra/docker-compose.admin.yml")
    keys = _env_keys(doc["services"]["caddy"]["environment"])
    assert "ADMIN_DOMAIN" in keys
    # Tenancy P5 dropped the operator-CIDR gate (admin is proxied through
    # Cloudflare; access is enforced by Google SSO + RLS), so the caddy env
    # must NOT carry ADMIN_ALLOWED_CIDR — its `:?required` guard would otherwise
    # fail the boot.
    assert "ADMIN_ALLOWED_CIDR" not in keys


def test_api_env_passes_sso_settings():
    doc = _load_yaml("infra/docker-compose.yml")
    keys = _env_keys(doc["services"]["api"]["environment"])
    for k in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
        "GOOGLE_OAUTH_HD",
        "ADMIN_BOOTSTRAP_EMAILS",
        "ADMIN_SESSION_TTL_S",
        "SESSION_COOKIE_SECURE",
        "ADMIN_POST_LOGIN_REDIRECT",
    ):
        assert k in keys, f"api service must pass {k} to the container"


def test_caddyfile_admin_block_is_sso_gated():
    # Tenancy P5 removed the operator-CIDR gate: admin is proxied through
    # Cloudflare and access is enforced at the app layer (Google SSO + RLS),
    # with the origin locked down by Authenticated Origin Pulls (mTLS). The
    # admin block must therefore have NO remote_ip/respond-403 CIDR gate, and
    # MUST require Cloudflare's AOP client cert.
    text = (INFRA / "Caddyfile").read_text()
    assert "{$ADMIN_DOMAIN}" in text
    admin_block = text.split("{$ADMIN_DOMAIN}", 1)[1]
    # The old CIDR gate is gone.
    assert "ADMIN_ALLOWED_CIDR" not in admin_block
    assert "remote_ip" not in admin_block
    assert "respond 403" not in admin_block
    # Same-origin: /v1 to the API, everything else to the SPA container.
    assert "admin-ui:8080" in admin_block
    # Origin lockdown: only Cloudflare (presenting the AOP client cert) gets in.
    assert "require_and_verify" in admin_block
    assert "cloudflare-origin-pull-ca.pem" in admin_block


def test_api_origin_does_not_expose_admin_plane():
    # Defense in depth: the admin/auth plane must be reachable ONLY via the
    # CIDR-gated admin.<domain> origin, so the ungated api.<domain> block must 403
    # the /v1/admin/* and /v1/auth/* prefixes.
    text = (INFRA / "Caddyfile").read_text()
    # Anchor on the site headers ("<domain> {"), not the leading comment that also
    # mentions both placeholders.
    api_block = text.split("{$API_DOMAIN} {", 1)[1].split("{$LIVEKIT_DOMAIN} {", 1)[0]
    assert "/v1/admin/*" in api_block and "/v1/auth/*" in api_block
    assert "403" in api_block


def test_inner_caddyfile_has_spa_fallback():
    text = (ROOT / "apps/admin-ui/Caddyfile").read_text()
    assert "try_files" in text and "/index.html" in text
    assert ":8080" in text


def test_dockerfile_runs_nonroot_static_server():
    text = (ROOT / "apps/admin-ui/Dockerfile").read_text()
    assert "vite build" in text or "npm run build" in text
    assert "caddy:2-alpine" in text
    assert re.search(r"USER\s+1001", text)


def test_build_workflow_builds_and_ships_admin_ui():
    text = (ROOT / ".github/workflows/build.yml").read_text()
    assert "usan-admin-ui" in text
    assert "scope=admin-ui" in text
    # Deploy job ships the overlay and includes it in the compose chain.
    assert "docker-compose.admin.yml" in text
    assert text.count("docker-compose.admin.yml") >= 2  # SCP source + -f chain


def _job_runs(doc: dict, job: str) -> str:
    """All `run:` script text for a workflow job, joined — for asserting commands."""
    steps = doc.get("jobs", {}).get(job, {}).get("steps", []) or []
    return "\n".join(s.get("run", "") for s in steps if isinstance(s, dict))


def _job_workdirs(doc: dict, job: str) -> set[str]:
    steps = doc.get("jobs", {}).get(job, {}).get("steps", []) or []
    return {s.get("working-directory") for s in steps if isinstance(s, dict)}


def test_frontend_ci_jobs_exist():
    # Assert the actual job STRUCTURE (working-directory + invoked npm scripts), not
    # just substring presence — a misconfigured job (wrong dir / wrong script) must fail.
    test_yml = _load_yaml(".github/workflows/test.yml")
    lint_yml = _load_yaml(".github/workflows/lint.yml")

    vitest = _job_runs(test_yml, "vitest-admin-ui")
    assert "npm ci" in vitest
    assert "npm test" in vitest
    assert "apps/admin-ui" in _job_workdirs(test_yml, "vitest-admin-ui")

    lint = _job_runs(lint_yml, "lint-admin-ui")
    assert "npm run lint" in lint
    assert "npm run typecheck" in lint
    assert "apps/admin-ui" in _job_workdirs(lint_yml, "lint-admin-ui")

    # The scripts job installs pyyaml (this very test module imports yaml).
    assert "pyyaml" in _job_runs(test_yml, "pytest-scripts")


def test_terraform_has_admin_dns_record():
    text = (INFRA / "terraform/dns.tf").read_text()
    assert 'cloudflare_dns_record" "admin"' in text
    block = text.split('"admin"', 1)[1]
    assert 'name    = "admin"' in block
    # Tenancy P5 proxies admin through Cloudflare (orange-cloud) for WAF/DDoS/Access.
    assert "proxied = true" in block


def test_env_examples_document_admin_keys():
    prod = (INFRA / ".env.prod.example").read_text()
    # ADMIN_ALLOWED_CIDR is intentionally NOT required: Tenancy P5 removed the
    # operator-CIDR gate (admin is proxied through Cloudflare; access is enforced
    # by Google SSO + RLS), so the env example no longer advertises that key.
    for k in (
        "ADMIN_DOMAIN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
        "ADMIN_BOOTSTRAP_EMAILS",
    ):
        assert k in prod, f".env.prod.example must document {k}"
    assert "/v1/auth/callback" in prod  # the exact redirect URI shape
