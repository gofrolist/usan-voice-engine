"""Keyless V4 signed GET URLs for GCS recordings.

Signs via IAM signBlob using the runtime's attached service account (ADC) — the SA
self-impersonates, so no private-key file is needed. On a GCE VM the SA must hold
roles/iam.serviceAccountTokenCreator on itself and read access to the object.
Blocking (a network call to IAM signBlob); call via asyncio.to_thread.
"""

import datetime
import threading

import google.auth
import google.auth.credentials
import google.auth.transport.requests
from google.cloud import storage


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    """Split gs://bucket/object/key into (bucket, key). Raises ValueError if malformed.

    Rejects keys that begin with '/' or contain a '..' path segment, so a crafted
    recording_uri cannot escape its prefix or be coerced into an unexpected object.
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    bucket, _, key = uri[len("gs://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"gs:// URI missing bucket or object key: {uri!r}")
    segments = key.split("/")
    if key.startswith("/") or "%" in key or any(seg in ("..", ".") for seg in segments):
        raise ValueError(f"gs:// object key has an unsafe path: {uri!r}")
    return bucket, key


# Cached ADC credentials for signing. Refreshing hits the metadata server, so reuse
# the credentials object across requests and refresh only when the token is missing or
# expired (tokens last ~1h). Guarded by a lock because generate_signed_url runs inside
# asyncio.to_thread worker threads.
_signing_credentials: google.auth.credentials.Credentials | None = None
_signing_lock = threading.Lock()


def _signing_creds() -> google.auth.credentials.Credentials:
    """Return refreshed ADC suitable for IAM signBlob, cached across calls."""
    global _signing_credentials
    with _signing_lock:
        if _signing_credentials is None:
            _signing_credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        # MUST refresh before first use: pre-refresh, service_account_email is the
        # literal "default" and token is None. .valid stays True until the token
        # expires, so warm calls skip the metadata round-trip.
        if not _signing_credentials.valid:
            _signing_credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
        return _signing_credentials


def generate_signed_url(
    gs_uri: str, ttl_seconds: int, *, expected_bucket: str | None = None
) -> str:
    """Return a V4 signed GET URL for a gs:// object, signed keylessly via IAM signBlob.

    The signBlob call is unavoidably per-URL (keyless V4 signing); the ADC refresh it
    needs is cached across requests (see _signing_creds). When ``expected_bucket`` is
    given, the parsed bucket must match it — fail closed rather than sign a URL for an
    object in some other (attacker-influenced) bucket.
    """
    bucket_name, blob_name = _parse_gs_uri(gs_uri)
    if expected_bucket is not None and bucket_name != expected_bucket:
        raise ValueError(
            f"gs:// bucket {bucket_name!r} does not match expected {expected_bucket!r}"
        )
    credentials = _signing_creds()
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
