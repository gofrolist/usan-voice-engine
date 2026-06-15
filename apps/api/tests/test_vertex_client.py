"""Review H4: the api-side Vertex client is built with ``vertexai=True`` (ADC) — NEVER the
Gemini Developer API.

Post-call summarization (US4), the monthly family-report narrative (US8), and the Admin
Studio text-test all route through ``vertex_test._generate_sync``. A regression that built
the client as the Gemini Developer API (``vertexai=False`` / ``api_key=...``) would silently
egress transcript/elder PHI to a non-BAA-covered service — a Constitution II violation. The
existing summarization tests mock ``run_vertex_turn`` and so could not catch that; this pins
the client construction itself.
"""

from types import SimpleNamespace

from usan_api import vertex_test


class _FakeModels:
    def generate_content(self, **kwargs: object) -> object:
        return SimpleNamespace(candidates=[])


class _FakeClient:
    instances: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        _FakeClient.instances.append(kwargs)
        self.models = _FakeModels()

    def close(self) -> None:
        return None


def test_generate_sync_uses_vertex_ai_not_gemini_dev_api(monkeypatch):
    import google.genai as genai

    _FakeClient.instances.clear()
    monkeypatch.setattr(genai, "Client", _FakeClient)

    settings = SimpleNamespace(gcp_project="proj-x", vertex_location="us-central1")
    vertex_test._generate_sync(
        model="gemini-2.5-flash", config=None, contents=[], settings=settings
    )

    assert len(_FakeClient.instances) == 1
    kwargs = _FakeClient.instances[0]
    assert kwargs.get("vertexai") is True  # Vertex AI (BAA), not the Gemini Developer API
    assert kwargs.get("project") == "proj-x"
    assert kwargs.get("location") == "us-central1"
    assert "api_key" not in kwargs  # the Developer-API key path is never taken
