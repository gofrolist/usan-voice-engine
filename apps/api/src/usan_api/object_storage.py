"""Keyless V4 signed GET URLs for GCS recordings.

Signs via IAM signBlob using the runtime's attached service account (ADC) — the SA
self-impersonates, so no private-key file is needed. On a GCE VM the SA must hold
roles/iam.serviceAccountTokenCreator on itself and read access to the object.
Blocking (a network call to IAM signBlob); call via asyncio.to_thread.
"""

import datetime

import google.auth
import google.auth.transport.requests
from google.cloud import storage


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    """Split gs://bucket/object/key into (bucket, key). Raises ValueError if malformed."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    bucket, _, key = uri[len("gs://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"gs:// URI missing bucket or object key: {uri!r}")
    return bucket, key


def generate_signed_url(gs_uri: str, ttl_seconds: int) -> str:
    """Return a V4 signed GET URL for a gs:// object, signed keylessly via IAM signBlob."""
    bucket_name, blob_name = _parse_gs_uri(gs_uri)

    # ADC. On a GCE VM this is the attached service account via the metadata server.
    credentials, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    # MUST refresh: pre-refresh, service_account_email is the literal "default" and
    # token is None — signing would fail or sign as the wrong principal.
    credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
    sa_email = credentials.service_account_email  # type: ignore[attr-defined]

    client = storage.Client(credentials=credentials)
    blob = client.bucket(bucket_name).blob(blob_name)
    return str(
        blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(seconds=ttl_seconds),
            method="GET",
            service_account_email=sa_email,
            access_token=credentials.token,
        )
    )
