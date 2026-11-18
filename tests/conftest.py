import pytest

from sealwharf.application.worker import Runtime


@pytest.fixture(autouse=True)
def quiet_courtesy_messages(request, monkeypatch):
    """Keep "received" and "signed by" notes out of tests that count failure notices.

    The end-to-end battery in test_flow checks those notes explicitly.
    """
    if request.module.__name__ != "test_flow":
        monkeypatch.setattr(Runtime, "mention", lambda *args, **kwargs: None)
