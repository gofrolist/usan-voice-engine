from usan_api.repositories.metrics import response_latency_ms


def test_response_latency_sums_present_components():
    assert response_latency_ms(120, 210, 80) == 410


def test_response_latency_ignores_none():
    assert response_latency_ms(None, 210, 80) == 290


def test_response_latency_all_none_is_none():
    assert response_latency_ms(None, None, None) is None
