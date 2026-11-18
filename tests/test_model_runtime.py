import httpx
import pytest

from sealwharf.adapters.model_runtime import LocalModelSession, model_status


def test_model_status_checks_local_model_name():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"models": [{"name": "qwen3-vl:4b"}]}))
    assert model_status(transport=transport) is True
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"models": [{"name": "other"}]}))
    assert model_status(transport=transport) is False


def test_invalid_model_service_response_is_rejected():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"models": "invalid"}))
    with pytest.raises(ValueError):
        model_status(transport=transport)


def test_connection_timeout_is_treated_as_unavailable():
    def timeout(request):
        raise httpx.ConnectTimeout("Local service unavailable", request=request)
    assert model_status(transport=httpx.MockTransport(timeout)) is None


def test_slow_local_model_service_is_treated_as_unavailable():
    def timeout(request):
        raise httpx.ReadTimeout("Local service is starting", request=request)
    assert model_status(transport=httpx.MockTransport(timeout)) is None


def test_local_model_is_checked_once_and_must_be_installed(monkeypatch):
    checks = []
    monkeypatch.setattr("sealwharf.adapters.model_runtime.model_status", lambda: checks.append(True) or True)
    session = LocalModelSession()
    session.ensure()
    session.ensure()
    assert len(checks) == 1
    for status in (None, False):
        monkeypatch.setattr("sealwharf.adapters.model_runtime.model_status", lambda value=status: value)
        with pytest.raises(RuntimeError):
            LocalModelSession().ensure()


def test_local_model_failure_does_not_confirm_candidate(monkeypatch):
    def fail():
        raise RuntimeError("model unavailable")
    monkeypatch.setattr("sealwharf.adapters.model_runtime.ensure_local_model", fail)
    session = LocalModelSession()
    assert session.review_contextual(b"synthetic", "Jane Example", object()) is None
