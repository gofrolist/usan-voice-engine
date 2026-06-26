from __future__ import annotations

import uuid

import pytest

from usan_api.compat import ids
from usan_api.compat.errors import CompatError


def test_chat_id_round_trips():
    cid = uuid.uuid4()
    token = ids.encode_chat_id(cid)
    assert token.startswith("chat_")
    assert ids.decode_chat_id(token) == cid


def test_message_id_encodes_with_prefix():
    mid = uuid.uuid4()
    assert ids.encode_message_id(mid) == "message_" + mid.hex


@pytest.mark.parametrize("bad", ["nope", "chat_xyz", "agent_" + "0" * 32, ""])
def test_decode_chat_id_rejects_malformed(bad):
    with pytest.raises(CompatError) as exc:
        ids.decode_chat_id(bad)
    assert exc.value.status_code == 422
