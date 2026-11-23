"""Tests for the catch-all error handler (section 6.2).

Checked on a throwaway app with a route that raises, rather than by breaking a
real endpoint: the real service has no route that fails on purpose, and section
6.6 says no endpoints may be added to it.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import register_error_handlers

SECRET = "connection to /Users/someone/secret/path failed: password=hunter2"


@pytest.fixture
def failing_client() -> TestClient:
    """A client for an app whose only route raises an unexpected error."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(SECRET)

    # Without this the test client re-raises the error instead of letting the
    # handler turn it into a response, which is the thing being tested.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_an_unexpected_error_becomes_a_500_in_the_standard_shape(failing_client):
    response = failing_client.get("/boom")

    assert response.status_code == 500
    payload = response.json()
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "internal_error"
    assert isinstance(payload["error"]["message"], str)
    assert "details" not in payload["error"]


def test_the_reply_never_contains_the_exception_text_or_a_stack_trace(failing_client):
    """The caller learns that it failed, and nothing else.

    An exception's text can carry a file path, a database column, a stack trace
    or a piece of someone's ticket. None of that belongs in an HTTP reply, so
    this test pins the one thing that must never regress.
    """
    body = failing_client.get("/boom").text

    assert SECRET not in body
    assert "password" not in body
    assert "hunter2" not in body
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    assert "/Users/" not in body


def test_the_full_error_is_logged_for_the_people_running_the_service(
    failing_client, caplog
):
    """What the caller does not get, the log must have."""
    with caplog.at_level("ERROR"):
        failing_client.get("/boom")

    logged = caplog.text
    assert "unhandled error" in logged
    assert "/boom" in logged
    # The log keeps what the response drops: the real error and its traceback.
    assert SECRET in logged
    assert "RuntimeError" in logged
    assert "Traceback" in logged
