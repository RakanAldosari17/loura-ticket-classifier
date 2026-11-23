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
