import json
import uuid


def _create(compat_client, headers, **over):
    data = {
        "knowledge_base_name": over.get("name", "Support"),
        "knowledge_base_texts": json.dumps(
            over.get("texts", [{"title": "FAQ", "text": "hello world"}])
        ),
    }
    data.update(over.get("extra", {}))
    return compat_client.post("/create-knowledge-base", data=data, headers=headers)


def test_create_in_progress_and_omits_sources(compat_client, compat_headers) -> None:
    r = _create(compat_client, compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["knowledge_base_id"].startswith("knowledge_base_")
    assert body["status"] == "in_progress"
    assert "knowledge_base_sources" not in body


def test_files_rejected_422(compat_client, compat_headers) -> None:
    files = {"knowledge_base_files[]": ("a.txt", b"data", "text/plain")}
    r = compat_client.post(
        "/create-knowledge-base",
        data={"knowledge_base_name": "K"},
        files=files,
        headers=compat_headers,
    )
    assert r.status_code == 422


def test_get_list_delete_lifecycle(compat_client, compat_headers) -> None:
    kid = _create(compat_client, compat_headers).json()["knowledge_base_id"]
    assert (
        compat_client.get(f"/get-knowledge-base/{kid}", headers=compat_headers).status_code == 200
    )
    assert compat_client.get("/list-knowledge-bases", headers=compat_headers).status_code == 200
    assert (
        compat_client.delete(f"/delete-knowledge-base/{kid}", headers=compat_headers).status_code
        == 204
    )
    assert (
        compat_client.get(f"/get-knowledge-base/{kid}", headers=compat_headers).status_code == 404
    )


def test_bad_id_422(compat_client, compat_headers) -> None:
    r = compat_client.get(f"/get-knowledge-base/agent_{uuid.uuid4().hex}", headers=compat_headers)
    assert r.status_code == 422
