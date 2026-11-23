"""The `ModelClient` interface and the model-unavailable error (section 10.1).

This is the only interface in the project. Everything that classifies a ticket
depends on this and nothing else, so the model can be swapped without touching
the workers.

It is deliberately tiny: two strings in, one string out, one error. Anything
larger would start describing a particular provider, and then swapping the model
would mean changing the worker too.
"""

from typing import Protocol


class ModelUnavailableError(Exception):
    """Raised when a model call times out or fails.

    The one failure the system retries (7.4, step 3). It means "ask again and it
    might work", which is true of a timeout or a 503 and not true of a reply
    that came back malformed — that is `InvalidOutputError` in `classifier.py`,
    and it is never retried.
    """


class ModelRejectedRequestError(Exception):
    """Raised when the provider refuses the request itself.

    A bad API key, a malformed request, a model name that does not exist. The
    model never looked at the ticket, and the same call would be refused the
    same way next time, so this is **never retried** — which is why it is a
    separate type from `ModelUnavailableError` rather than a subclass of it.

    The ticket still ends `model_unavailable`: from the ticket's point of view
    the model was not available to classify it. What is different is the fix,
    and that goes in the log, where an operator can act on it.

    Carries the status code only. The provider's own message is deliberately
    not kept: it can echo the request back, which contains the ticket, and the
    API key must never reach a log or an exception.
    """

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"the provider rejected the request with {status_code}")


class ModelClient(Protocol):
    """Anything that can turn a prompt into reply text.

    A `Protocol` describes a shape rather than a base class to inherit from: any
    object with a matching `classify` method satisfies it, so the fake model and
    a real provider need nothing in common but this method.
    """

    def classify(self, instructions: str, ticket_text: str) -> str:
        """Return the model's raw reply text.

        The instructions and the ticket are separate arguments, never joined
        here, so a real provider can send them as a system message and a user
        message. That separation is L2: prompt structure (instructions kept
        separate from the ticket data).

        The reply is **raw text**. It is not parsed, trusted or checked by the
        client; `classifier.validate_reply` decides whether it may be stored.

        Raises `ModelUnavailableError` on a timeout or any provider failure.
        """
        ...
