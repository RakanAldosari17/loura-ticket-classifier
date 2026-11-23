"""Tests for the real provider (10.4), against a mocked HTTP layer only.

**Nothing here touches the network.** `httpx.MockTransport` answers every
request from a function in the test, so the request shape, the success path and
every failure path are checked without an API key and without spending anything.

That is also the honest limit of this chunk: the provider has **never been run
against the live API**. These tests prove the code sends what the documented
request format says and handles what the documented errors say; they cannot
prove the documentation is right. The README says so plainly.
"""

import json

import httpx
import pytest

from app import classifier
from app.config import ANTHROPIC_API_URL, ANTHROPIC_VERSION, MODEL_MAX_TOKENS, Settings
from app.models import AnthropicModelClient, ModelUnavailableError, get_model_client

API_KEY = "sk-ant-test-secret-do-not-leak-123456"

VALID_BODY = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5",
    "stop_reason": "end_turn",
    "content": [
        {
            "type": "text",
            "text": '{"category": "billing", "priority": "medium", '
            '"summary": "Customer was charged twice."}',
        }
    ],
}


def _settings(**overrides) -> Settings:
    values = {
        "model_provider": "anthropic",
        "model_api_key": API_KEY,
        "model_name": "claude-haiku-4-5",
        "fake_failure_rate": 0.0,
        "worker_count": 2,
        "poll_interval_seconds": 1.0,
        "model_timeout_seconds": 20.0,
        "retry_wait_seconds": 2.0,
        "database_path": "./data/tickets.db",
    }
    values.update(overrides)
    return Settings(**values)


def _client_with(handler, monkeypatch, **setting_overrides) -> AnthropicModelClient:
    """Build the provider with every HTTP call answered by `handler`."""
    transport = httpx.MockTransport(handler)
    real_post = httpx.post

    def fake_post(url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as mock_client:
            return mock_client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert real_post is not httpx.post
    return AnthropicModelClient(_settings(**setting_overrides))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=VALID_BODY)


# --- the request we send -------------------------------------------------


def test_the_request_matches_the_documented_format(monkeypatch):
    """One POST, three headers, instructions as system, ticket as user."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=VALID_BODY)

    client = _client_with(handler, monkeypatch)
    client.classify("THE INSTRUCTIONS", "<ticket>THE TICKET</ticket>")

    assert captured["method"] == "POST"
    assert captured["url"] == ANTHROPIC_API_URL
    assert captured["headers"]["x-api-key"] == API_KEY
    assert captured["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert captured["headers"]["content-type"] == "application/json"

    body = captured["body"]
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == MODEL_MAX_TOKENS
    assert body["system"] == "THE INSTRUCTIONS"
    assert body["messages"] == [
        {"role": "user", "content": "<ticket>THE TICKET</ticket>"}
    ]


def test_the_instructions_and_the_ticket_are_never_joined(monkeypatch):
    """L2 in its strongest form: two fields, not one string.

    If these were concatenated, the model would have no structural signal for
    where the customer's text begins.
    """
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=VALID_BODY)

    client = _client_with(handler, monkeypatch)
    client.classify(classifier.INSTRUCTIONS, classifier.build_ticket_text("S", "B"))

    body = captured["body"]
    # The system message is exactly the instructions, and nothing else. It does
    # name the markers, because the model has to be told where customer text
    # will appear, but it carries no ticket content.
    assert body["system"] == classifier.INSTRUCTIONS
    assert "Subject: S" not in body["system"]

    # The user message is exactly the ticket, and nothing else.
    assert body["messages"][0]["content"] == classifier.build_ticket_text("S", "B")
    assert classifier.INSTRUCTIONS not in body["messages"][0]["content"]


def test_the_model_name_comes_from_the_setting(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=VALID_BODY)

    client = _client_with(handler, monkeypatch, model_name="claude-sonnet-5")
    client.classify("i", "t")

    assert captured["body"]["model"] == "claude-sonnet-5"


# --- the success path ----------------------------------------------------


def test_a_successful_reply_returns_the_text_unparsed(monkeypatch):
    """The client returns text. It does not parse, trust or check it."""
    client = _client_with(_ok, monkeypatch)

    reply = client.classify("i", "t")

    assert reply == VALID_BODY["content"][0]["text"]
    # And the real validator accepts it, so the two halves fit together.
    result = classifier.validate_reply(reply)
    assert result.category == "billing"


def test_several_text_blocks_are_joined(monkeypatch):
    """The API may split a reply across blocks; the JSON must survive that."""
    body = dict(VALID_BODY)
    body["content"] = [
        {"type": "text", "text": '{"category": "billing", "priority": "low",'},
        {"type": "text", "text": ' "summary": "Split across blocks."}'},
    ]

    client = _client_with(lambda r: httpx.Response(200, json=body), monkeypatch)

    assert classifier.validate_reply(client.classify("i", "t")).priority == "low"


def test_non_text_blocks_are_ignored(monkeypatch):
    """A thinking block alongside the answer must not corrupt the reply."""
    body = dict(VALID_BODY)
    body["content"] = [
        {"type": "thinking", "thinking": "Let me consider this ticket..."},
        VALID_BODY["content"][0],
    ]

    client = _client_with(lambda r: httpx.Response(200, json=body), monkeypatch)

    assert classifier.validate_reply(client.classify("i", "t")).category == "billing"


def test_rubbish_text_is_returned_rather_than_hidden(monkeypatch):
    """A reply that will fail validation is still the validator's business.

    The client must not decide that a reply is bad: that would make
    `invalid_output` unreachable and move the model boundary into the wrong
    module.
    """
    body = dict(VALID_BODY)
    body["content"] = [{"type": "text", "text": "Sure! Here you go: {oops"}]

    client = _client_with(lambda r: httpx.Response(200, json=body), monkeypatch)
    reply = client.classify("i", "t")

    assert reply == "Sure! Here you go: {oops"
    with pytest.raises(classifier.InvalidOutputError):
        classifier.validate_reply(reply)


# --- failures worth retrying -> ModelUnavailableError --------------------


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529])
def test_retryable_statuses_become_model_unavailable(monkeypatch, status):
    """429 and the 5xx family are the worker's one retry (7.4 step 3).

    529 is Anthropic's "overloaded", which is exactly the case retrying helps.
    """
    client = _client_with(lambda r: httpx.Response(status, json={}), monkeypatch)

    with pytest.raises(ModelUnavailableError, match=str(status)):
        client.classify("i", "t")


def test_a_timeout_becomes_model_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = _client_with(handler, monkeypatch)

    with pytest.raises(ModelUnavailableError, match="timed out"):
        client.classify("i", "t")


def test_a_connection_error_becomes_model_unavailable(monkeypatch):
    """DNS failure, refused connection, TLS problem: never reached the provider."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(handler, monkeypatch)

    with pytest.raises(ModelUnavailableError, match="could not reach"):
        client.classify("i", "t")


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("no content key", {"stop_reason": "end_turn"}),
        ("content is not a list", {"content": "text"}),
        ("empty content", {"content": [], "stop_reason": "end_turn"}),
        ("only a thinking block", {"content": [{"type": "thinking", "thinking": "..."}]}),
        ("refusal", {"content": [], "stop_reason": "refusal"}),
    ],
)
def test_an_unusable_response_shape_becomes_model_unavailable(monkeypatch, name, body):
    """A worker must not die because a payload changed shape."""
    client = _client_with(lambda r: httpx.Response(200, json=body), monkeypatch)

    with pytest.raises(ModelUnavailableError):
        client.classify("i", "t")


# --- failures not worth retrying ----------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_a_permanent_failure_is_not_a_model_unavailable_error(monkeypatch, status):
    """A bad key or a malformed request fails identically next time.

    Making these retryable would double every wasted call and bury the real
    problem behind a `model_unavailable` ticket.
    """
    client = _client_with(lambda r: httpx.Response(status, json={}), monkeypatch)

    with pytest.raises(RuntimeError) as caught:
        client.classify("i", "t")

    assert not isinstance(caught.value, ModelUnavailableError)


# --- the key must not leak ----------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 429, 500, 529])
def test_the_api_key_never_appears_in_an_error(monkeypatch, status):
    """Errors reach logs, terminals and bug reports."""
    client = _client_with(lambda r: httpx.Response(status, json={}), monkeypatch)

    with pytest.raises(Exception) as caught:
        client.classify("i", "t")

    assert API_KEY not in str(caught.value)
    assert API_KEY not in repr(caught.value)


def test_the_api_key_never_appears_in_a_log_line(monkeypatch, caplog):
    """Including when the provider echoes the request back in its error body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": f"bad key {API_KEY}"}}
        )

    client = _client_with(handler, monkeypatch)

    with caplog.at_level("DEBUG"):
        with pytest.raises(RuntimeError):
            client.classify("i", "t")

    assert API_KEY not in caplog.text


def test_the_providers_error_text_is_not_repeated_back(monkeypatch):
    """The provider can echo request content, which includes ticket text."""
    secret_ticket = "my card number is 4111-1111-1111-1111"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": secret_ticket}})

    client = _client_with(handler, monkeypatch)

    with pytest.raises(RuntimeError) as caught:
        client.classify("i", secret_ticket)

    assert secret_ticket not in str(caught.value)


# --- choosing the provider ----------------------------------------------


def test_the_provider_is_selected_by_the_setting():
    client = get_model_client(_settings())

    assert isinstance(client, AnthropicModelClient)


def test_the_fake_is_still_the_default():
    from app.models import FakeModelClient

    client = get_model_client(_settings(model_provider="fake", model_api_key=None))

    assert isinstance(client, FakeModelClient)


def test_the_provider_with_no_key_says_what_to_do():
    """The message has to be actionable: it is the first thing a reviewer sees."""
    with pytest.raises(ValueError) as caught:
        get_model_client(_settings(model_api_key=None))

    message = str(caught.value)
    assert "MODEL_API_KEY" in message
    assert ".env" in message
    assert "start the service again" in message


def test_an_unknown_provider_still_names_the_supported_ones():
    with pytest.raises(ValueError, match="anthropic"):
        get_model_client(_settings(model_provider="openai"))


def test_the_default_model_name_is_a_small_cheap_one():
    """Classification is a small task; the default should not cost Opus money."""
    import os

    from app.config import load_settings

    saved = os.environ.pop("MODEL_NAME", None)
    try:
        assert load_settings().model_name == "claude-haiku-4-5"
    finally:
        if saved is not None:
            os.environ["MODEL_NAME"] = saved
