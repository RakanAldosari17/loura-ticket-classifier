"""The fake model: deterministic rules, plus deliberately broken replies (10.3).

The default model client. It needs no network, no API key and no money, and it
can be scripted in tests so that no test depends on randomness.

Two modes, and they never mix:

- **scripted**: replies and errors given in advance, returned in order. Every
  worker test uses this, so a test that says "the model times out, then
  succeeds" means exactly that and cannot do anything else.
- **rules**: keyword rules producing the classifications in section 15, with a
  share of deliberately broken replies (`FAKE_FAILURE_RATE`). This is the mode a
  reviewer sees when they start the service, and the broken replies are the
  point: they make the retry path and the validation path happen for real.

The rules are keyword matching, not language understanding. That is a stated
weakness, not a pretence: what this project is really demonstrating is how the
code treats a model's output, and for that the output only has to be plausible.
"""

import random

from app.models.base import ModelUnavailableError

# --- the rules (section 15) ----------------------------------------------
#
# Matched against the subject first, then the body. The subject is the ticket's
# main request, which is also what the real prompt asks a model to classify by
# (10.2), so this mirrors the instruction rather than inventing a second rule.
#
# Billing is checked before account on purpose: "accountant" contains "account",
# and t-1007 ("Our accountant needs the corrected invoice") is a billing ticket.

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "billing",
        (
            "invoice",
            "charge",
            "charged",
            "refund",
            "subscription",
            "card statement",
            "payment",
            "overcharg",
            "billing",
        ),
    ),
    (
        "account",
        (
            "log in",
            "login",
            "password",
            "email address",
            "change email",
            "sign in",
            "credentials",
            "my account",
        ),
    ),
    (
        "technical",
        (
            "api",
            "500",
            "error",
            "export",
            "timeout",
            "upload",
            "broken",
            "crash",
            "not working",
            "integration",
        ),
    ),
)

# "blocking our nightly job", "production" — something is already failing for a
# business, which is what high priority is for.
_HIGH_PRIORITY_KEYWORDS = (
    "blocking",
    "production",
    "outage",
    "cannot access",
    "down",
    "data loss",
)

# A request rather than a fault, or something the customer says is not pressing.
# "not urgent" is listed and bare "urgent" is not: t-1006 says "Not urgent" and
# t-1005 shouts "URGENT" in its subject, so the bare word means nothing.
_LOW_PRIORITY_KEYWORDS = (
    "not urgent",
    "nice to have",
    "feature request",
    "would love",
    "what do i need to do",
    "i'd like to",
    "how do i",
)

# Below this, with no keyword match at all, a body is junk rather than a report.
_JUNK_BODY_LENGTH = 20

_SUMMARY_BY_CATEGORY = {
    "billing": "Customer reports a billing problem and asks for it to be corrected.",
    "account": "Customer needs help with access to or details on their account.",
    "technical": "Customer reports the product failing and needs it investigated.",
    "other": "Customer has a request that does not fit the other categories.",
}

# --- the broken replies (A24) --------------------------------------------
#
# One per way the validation in 8.3 can fail, so running the service exercises
# every branch of it. `None` means "raise ModelUnavailableError" instead of
# returning text, which is the timeout case.
_BROKEN_REPLIES: tuple[str | None, ...] = (
    # malformed JSON: a cut-off object
    '{"category": "billing", "priority": "medi',
    # valid JSON wrapped in prose
    'Sure! Here is the result: {"category": "billing", "priority": "low", '
    '"summary": "Customer has a billing question."}',
    # a category outside the allowed set
    '{"category": "refund", "priority": "low", "summary": "A refund question."}',
    # a priority outside the allowed set
    '{"category": "billing", "priority": "urgent", "summary": "A billing question."}',
    # a missing summary
    '{"category": "billing", "priority": "low"}',
    # a summary over 200 characters
    '{"category": "billing", "priority": "low", "summary": "'
    + "The customer has a very long-winded billing problem. " * 5
    + '"}',
    # a simulated timeout
    None,
)


class FakeModelClient:
    """A `ModelClient` that needs no provider (10.3).

    `failure_rate` is the share of calls that return one of the broken replies,
    or time out. `scripted` overrides it entirely: when given, the client
    returns those items in order and ignores both the rules and the failure
    rate.
    """

    def __init__(
        self,
        failure_rate: float = 0.0,
        scripted: list[str | Exception] | None = None,
        seed: int | None = None,
    ) -> None:
        self.failure_rate = failure_rate
        self._scripted = list(scripted) if scripted is not None else None
        # Its own generator, so the service's broken replies never depend on,
        # or disturb, anything else using `random`.
        self._random = random.Random(seed)
        self.calls: list[tuple[str, str]] = []

    def classify(self, instructions: str, ticket_text: str) -> str:
        """Return reply text, or raise `ModelUnavailableError`.

        Every call is recorded in `self.calls`, which is how the worker tests
        assert that the model was never called for an injection, or called
        exactly once for an invalid reply.
        """
        self.calls.append((instructions, ticket_text))

        if self._scripted is not None:
            return self._next_scripted()

        if self.failure_rate > 0 and self._random.random() < self.failure_rate:
            return self._broken_reply()

        return self._reply_from_rules(ticket_text)

    def _next_scripted(self) -> str:
        """Return the next scripted item, raising it if it is an error."""
        assert self._scripted is not None
        if not self._scripted:
            raise AssertionError(
                "the scripted fake model ran out of replies: the code under test "
                "called the model more times than the test expected"
            )
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _broken_reply(self) -> str:
        """Return one of the broken replies, or raise the timeout."""
        choice = self._random.choice(_BROKEN_REPLIES)
        if choice is None:
            raise ModelUnavailableError("the fake model timed out")
        return choice

    def _reply_from_rules(self, ticket_text: str) -> str:
        """Build a plausible reply for this ticket (section 15)."""
        subject, body = _split_prompt(ticket_text)
        category = _category_for(subject, body)
        priority = _priority_for(subject, body, category)
        summary = _SUMMARY_BY_CATEGORY[category]
        return (
            '{"category": "%s", "priority": "%s", "summary": "%s"}'
            % (category, priority, summary)
        )


def _split_prompt(ticket_text: str) -> tuple[str, str]:
    """Pull the subject and body back out of the wrapped prompt (10.2).

    The fake sees only what a real model sees: the prompt. If the markers are
    not there, the whole text is treated as the body, so this never raises on
    input it did not expect.
    """
    subject = ""
    body = ticket_text
    if "Subject: " in ticket_text and "\nBody:\n" in ticket_text:
        after_subject = ticket_text.split("Subject: ", 1)[1]
        subject, body = after_subject.split("\nBody:\n", 1)
        body = body.removesuffix("\n</ticket>")
    return subject, body


def _category_for(subject: str, body: str) -> str:
    """Pick a category, reading the subject first (section 15).

    The subject is the main request. t-1009 is why: its body mentions being
    overcharged, but the customer says that is "a separate ticket", and its
    subject — "Data export empty" — is what they actually want fixed.
    """
    for text in (subject.lower(), body.lower()):
        for category, keywords in _CATEGORY_KEYWORDS:
            if any(keyword in text for keyword in keywords):
                return category
    return "other"


def _priority_for(subject: str, body: str, category: str) -> str:
    """Pick a priority from what the ticket says about urgency."""
    text = f"{subject}\n{body}".lower()

    if any(keyword in text for keyword in _HIGH_PRIORITY_KEYWORDS):
        return "high"
    if any(keyword in text for keyword in _LOW_PRIORITY_KEYWORDS):
        return "low"
    # Junk with no keywords at all is nobody's emergency (t-1008, "asdf").
    if category == "other" and len(body.strip()) < _JUNK_BODY_LENGTH:
        return "low"
    if category == "other":
        return "low"
    return "medium"
