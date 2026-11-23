"""Tests for the model interface, the fake model, and choosing a client.

The fake model is what every worker test runs against, so these tests are
really about whether those tests can be trusted: a scripted fake that returned
its replies out of order, or silently kept returning the last one, would make
the whole of Chunk 6 meaningless.
"""

import json
from pathlib import Path

import pytest

from app import classifier
from app.config import Settings
from app.models import FakeModelClient, ModelUnavailableError, get_model_client

SAMPLES = {
    ticket["id"]: ticket
    for ticket in json.loads(Path("data/sample_tickets.json").read_text())
}

# Section 15, with FAKE_FAILURE_RATE=0. t-1005 is absent because it never
# reaches the model: the suspicion score fails it first.
EXPECTED = {
    "t-1001": ("billing", "medium"),
    "t-1002": ("account", "medium"),
    "t-1003": ("technical", "high"),
    "t-1004": ("account", "low"),
    "t-1006": ("other", "low"),
    "t-1007": ("billing", "medium"),
    "t-1008": ("other", "low"),
    "t-1009": ("technical", "medium"),
    "t-1010": ("technical", "medium"),
}


def _settings(**overrides) -> Settings:
    """Build settings without touching the environment."""
    values = {
        "model_provider": "fake",
        "model_api_key": None,
        "fake_failure_rate": 0.0,
        "worker_count": 2,
        "poll_interval_seconds": 1.0,
        "model_timeout_seconds": 20.0,
        "retry_wait_seconds": 2.0,
        "database_path": "./data/tickets.db",
    }
    values.update(overrides)
    return Settings(**values)


def _ask(fake: FakeModelClient, ticket_id: str) -> str:
    """Put one sample ticket through the fake, as the worker will."""
    ticket = SAMPLES[ticket_id]
    return fake.classify(
        classifier.INSTRUCTIONS,
        classifier.build_ticket_text(ticket["subject"], ticket["body"]),
    )


# --- the scripted fake: what every worker test depends on ---------------


def test_scripted_replies_come_back_in_order():
    fake = FakeModelClient(scripted=["first", "second", "third"])

    assert fake.classify("i", "t") == "first"
    assert fake.classify("i", "t") == "second"
    assert fake.classify("i", "t") == "third"


def test_a_scripted_error_is_raised_in_its_turn():
    """"Times out, then succeeds" has to mean exactly that."""
    fake = FakeModelClient(
        scripted=[ModelUnavailableError("timed out"), '{"ok": true}']
    )

    with pytest.raises(ModelUnavailableError):
        fake.classify("i", "t")
    assert fake.classify("i", "t") == '{"ok": true}'


def test_running_out_of_scripted_replies_fails_loudly():
    """Silence here would be the worst outcome.

    If the fake repeated its last reply once exhausted, a worker with a retry
    bug that called the model five times would still pass a test written for
    two calls.
    """
    fake = FakeModelClient(scripted=["only one"])
    fake.classify("i", "t")

    with pytest.raises(AssertionError, match="ran out of replies"):
        fake.classify("i", "t")


def test_a_scripted_fake_ignores_the_failure_rate():
    """A test must never be at the mercy of the failure rate."""
    fake = FakeModelClient(failure_rate=1.0, scripted=["always this"])

    assert fake.classify("i", "t") == "always this"


def test_every_call_is_recorded():
    """How the worker tests prove the model was not called, or called once."""
    fake = FakeModelClient(scripted=["a", "b"])
    fake.classify("instructions-1", "ticket-1")
    fake.classify("instructions-2", "ticket-2")

    assert fake.calls == [
        ("instructions-1", "ticket-1"),
        ("instructions-2", "ticket-2"),
    ]


# --- the rules (section 15) ---------------------------------------------


@pytest.mark.parametrize("ticket_id", sorted(EXPECTED))
def test_each_sample_gets_its_expected_classification(ticket_id):
    """With the failure rate at 0, the nine classifiable samples match 15."""
    fake = FakeModelClient(failure_rate=0.0)

    result = classifier.validate_reply(_ask(fake, ticket_id))

    assert (result.category, result.priority) == EXPECTED[ticket_id]


@pytest.mark.parametrize("ticket_id", sorted(EXPECTED))
def test_every_reply_passes_the_real_validation(ticket_id):
    """The fake must produce replies the real validator accepts.

    A fake that emitted something `validate_reply` rejected would make every
    sample fail as `invalid_output` and the end-to-end run meaningless.
    """
    fake = FakeModelClient(failure_rate=0.0)

    classifier.validate_reply(_ask(fake, ticket_id))


def test_the_rules_are_deterministic():
    """Same ticket, same answer, every time."""
    fake = FakeModelClient(failure_rate=0.0)

    replies = {_ask(fake, "t-1003") for _ in range(20)}

    assert len(replies) == 1


def test_t1009_is_classified_by_its_subject_not_its_second_issue():
    """Its body mentions being overcharged, but says that is a separate ticket.

    This is the rule that makes the fake mirror the real prompt's "classify it
    by its main request" instruction, rather than matching whichever keyword
    appears first.
    """
    fake = FakeModelClient(failure_rate=0.0)

    result = classifier.validate_reply(_ask(fake, "t-1009"))

    assert result.category == "technical"


def test_t1007_is_billing_even_though_it_mentions_an_accountant():
    """"accountant" contains "account", so the order of the rules matters."""
    fake = FakeModelClient(failure_rate=0.0)

    assert classifier.validate_reply(_ask(fake, "t-1007")).category == "billing"


def test_an_unrecognisable_ticket_is_other_and_low():
    fake = FakeModelClient(failure_rate=0.0)

    result = classifier.validate_reply(
        fake.classify(classifier.INSTRUCTIONS, classifier.build_ticket_text("", "asdf"))
    )

    assert (result.category, result.priority) == ("other", "low")


# --- the broken replies (A24) -------------------------------------------


def test_failure_rate_zero_never_breaks():
    fake = FakeModelClient(failure_rate=0.0, seed=1)

    for _ in range(200):
        classifier.validate_reply(_ask(fake, "t-1001"))


def test_failure_rate_one_always_breaks():
    """Either an invalid reply or a timeout, never a valid classification."""
    fake = FakeModelClient(failure_rate=1.0, seed=1)

    for _ in range(100):
        try:
            reply = _ask(fake, "t-1001")
        except ModelUnavailableError:
            continue
        with pytest.raises(classifier.InvalidOutputError):
            classifier.validate_reply(reply)


def test_every_broken_reply_fails_the_real_validation():
    """Each broken reply must break a *different* rule in 8.3.

    If one of them were accidentally valid, the service would look like it was
    exercising the failure paths while quietly storing a result.
    """
    from app.models.fake import _BROKEN_REPLIES

    reasons = set()
    for reply in _BROKEN_REPLIES:
        if reply is None:
            continue  # the timeout case, covered by the test below
        with pytest.raises(classifier.InvalidOutputError) as caught:
            classifier.validate_reply(reply)
        reasons.add(str(caught.value).split(":")[0])

    # Several distinct reasons, not the same failure seven times over.
    assert len(reasons) >= 4


def test_the_timeout_is_one_of_the_broken_outcomes():
    """The retry path has to be reachable when the service is just running."""
    fake = FakeModelClient(failure_rate=1.0, seed=0)

    for _ in range(200):
        try:
            _ask(fake, "t-1001")
        except ModelUnavailableError:
            return
    pytest.fail("the fake never timed out in 200 broken replies")


def test_the_failure_rate_is_roughly_honoured():
    """A loose check: 10% should not behave like 0% or 100%."""
    fake = FakeModelClient(failure_rate=0.1, seed=7)

    broken = 0
    for _ in range(1000):
        try:
            classifier.validate_reply(_ask(fake, "t-1001"))
        except (classifier.InvalidOutputError, ModelUnavailableError):
            broken += 1

    assert 50 <= broken <= 150, broken


# --- choosing a client (10.4) -------------------------------------------


def test_the_default_provider_is_the_fake():
    client = get_model_client(_settings())

    assert isinstance(client, FakeModelClient)


def test_the_fake_is_given_the_configured_failure_rate():
    client = get_model_client(_settings(fake_failure_rate=0.25))

    assert client.failure_rate == 0.25


def test_a_real_provider_without_a_key_refuses_to_start():
    """7.2 step 1: stop immediately, and say what to set."""
    with pytest.raises(ValueError) as caught:
        get_model_client(_settings(model_provider="anthropic", model_api_key=None))

    message = str(caught.value)
    assert "MODEL_API_KEY" in message
    assert "anthropic" in message


def test_a_real_provider_with_a_key_is_still_not_supported_yet():
    """No real provider is built, so it fails rather than pretending."""
    with pytest.raises(ValueError, match="not supported"):
        get_model_client(_settings(model_provider="anthropic", model_api_key="k"))


def test_an_unknown_provider_never_falls_back_to_the_fake():
    """The failure mode this prevents: a service that looks healthy while
    classifying everything with a keyword matcher."""
    for provider in ("anthropic", "openai", "ollama", "FAKE", "Fake", ""):
        with pytest.raises(ValueError):
            client = get_model_client(
                _settings(model_provider=provider, model_api_key="k")
            )
            assert not isinstance(client, FakeModelClient)


def test_the_api_key_is_never_in_an_error_message():
    """An error can end up in a log, a terminal or a bug report."""
    with pytest.raises(ValueError) as caught:
        get_model_client(
            _settings(model_provider="anthropic", model_api_key="sk-secret-123456")
        )

    assert "sk-secret-123456" not in str(caught.value)
