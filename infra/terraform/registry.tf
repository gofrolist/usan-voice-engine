# Plan 4e Workstream E — Artifact Registry + keyless supply chain.
#
# Images move from GHCR to an in-region Artifact Registry repo. Two keyless
# auth paths replace the long-lived GHCR_PAT:
#   - the VM PULLS keyless via its attached service account (artifactregistry.reader);
#   - GitHub Actions PUSHES keyless via Workload Identity Federation impersonating a
#     deploy SA (artifactregistry.writer) — no SA JSON key stored in GitHub secrets.
#
# This file is additive: applying it does NOT change how images are currently built
# or pulled (that flips in the CI/compose cutover). Reader-grant-first ordering.

resource "google_project_service" "artifactregistry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

# Docker repo: <region>-docker.pkg.dev/<project>/usan/usan-{api,agent,agent-base}.
resource "google_artifact_registry_repository" "usan" {
  project       = var.project_id
  location      = var.region
  repository_id = "usan"
  description   = "USAN container images (api, agent, agent-base). Plan 4e E."
  format        = "DOCKER"
  depends_on    = [google_project_service.artifactregistry]
}

# The VM pulls keyless via its attached SA (same ADC pattern as Vertex/secrets).
resource "google_artifact_registry_repository_iam_member" "vm_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.usan.location
  repository = google_artifact_registry_repository.usan.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.vm.email}"
}

# --- Workload Identity Federation: GitHub Actions -> GCP, keyless ---

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions"
  description               = "WIF pool for GitHub Actions OIDC (Plan 4e E)."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Only mint tokens for our repo AND only from the refs that legitimately push:
  # `main` (builds the `latest` tag) and `v*` release tags. build.yml triggers on
  # exactly these (push to main + tags). This blocks a malicious pull_request (whose
  # ref is `refs/pull/N/merge`) from adding a workflow that assumes the deploy SA and
  # poisons the registry — the principalSet alone (repository-scoped) would allow any
  # ref, so the ref gate lives here on the provider.
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && (assertion.ref == \"refs/heads/main\" || assertion.ref.startsWith(\"refs/tags/v\"))"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# The SA that CI impersonates. It can ONLY push to the usan GAR repo — nothing else.
resource "google_service_account" "github_deployer" {
  account_id   = "github-deployer"
  display_name = "GitHub Actions image pusher (Plan 4e E)"
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.usan.location
  repository = google_artifact_registry_repository.usan.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deployer.email}"
}

# Let workflows from our repo (via the WIF pool) impersonate the deploy SA. Scoped
# to attribute.repository so no other repo using this pool could assume it.
resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
