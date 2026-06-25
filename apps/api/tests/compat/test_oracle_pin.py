from __future__ import annotations

import hashlib

from tests.compat.oracle_loader import ORACLE_DIR, ORACLE_PATH, load_oracle, oracle_operations


def test_oracle_checksum_matches_pin():
    recorded = (ORACLE_DIR / "SHA256SUMS").read_text().split()[0]
    actual = hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest()
    assert actual == recorded, "vendored oracle changed without a reviewed re-pin"


def test_oracle_version_and_shape():
    spec = load_oracle()
    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["version"] == "3.0.0"
    assert (ORACLE_DIR / "VERSION").read_text().strip() == "3.0.0"


def test_oracle_has_84_operations():
    assert len(oracle_operations()) == 84
