import pytest

from sealwharf.application.worker import Runtime


@pytest.fixture(autouse=True)
def quiet_courtesy_messages(request, monkeypatch):
    """Keep "received" and "signed by" notes out of tests that count failure notices.

    The end-to-end battery in test_flow checks those notes explicitly.
    """
    if request.module.__name__ != "test_flow":
        monkeypatch.setattr(Runtime, "mention", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def questions_are_asked_at_once(monkeypatch):
    """Do not wait for more documents before asking; the tests that cover the wait set it themselves."""
    monkeypatch.setattr("sealwharf.application.worker.SETTLE_SECONDS", 0)
