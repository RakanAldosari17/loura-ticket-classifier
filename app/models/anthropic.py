"""A real model provider: Anthropic's Messages API, over plain HTTP (10.4).

Selected with `MODEL_PROVIDER=anthropic`. The fake model stays the default, and
nothing else in the system changes: this class satisfies the same `ModelClient`
interface, so the worker cannot tell the difference.

Why raw `httpx` rather than the `anthropic` SDK: section 12 fixes the dependency
list, `httpx` is already there for the test client, and the request this needs is
one POST with three headers. The SDK would be the better choice in a real
service — it brings retries, streaming and typed errors — and the README says so.

Every failure that is worth trying again becomes `ModelUnavailableError`, which
is the one error the worker retries (7.4 step 3). A failure that would fail again
identically — a bad key, a malformed request — is raised as itself, because
retrying it would waste a call and hide the real problem.
"""

import logging

import httpx

from app.config import (
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    MODEL_MAX_TOKENS,
    Settings,
)
from app.models.base import ModelUnavailableError

logger = logging.getLogger(__name__)

# Status codes worth a second attempt: too many requests, and anything the
# provider reports as its own fault. 529 is Anthropic's "overloaded".
_RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class AnthropicModelClient:
    """Calls Anthropic's Messages API and returns the reply text (10.1)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.model_api_key:
            # Should be unreachable: get_model_client checks first. Kept so the
            # class cannot be constructed into a broken state by other code.
            raise ValueError("MODEL_API_KEY is required for the anthropic provider")
        self._api_key = settings.model_api_key
        self._model_name = settings.model_name
        # The worker also enforces a timeout (10.1). This one stops the HTTP
        # request itself from outliving the worker's patience in a thread.
        self._timeout = settings.model_timeout_seconds

    def classify(self, instructions: str, ticket_text: str) -> str:
        """Send the prompt and return the model's raw reply text.

        The instructions go as the **system** message and the ticket as the
        **user** message. That is L2: prompt structure (instructions kept
        separate from the ticket data), expressed in the strongest form the API
        offers — the two never become one string.

        The reply is returned unparsed and untrusted; `classifier.validate_reply`
        decides whether any of it may be stored.
        """
        payload = {
            "model": self._model_name,
            "max_tokens": MODEL_MAX_TOKENS,
            "system": instructions,
            "messages": [{"role": "user", "content": ticket_text}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            response = httpx.post(
                ANTHROPIC_API_URL,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise ModelUnavailableError(f"the model timed out: {error}") from error
        except httpx.RequestError as error:
            # DNS failure, refused connection, TLS problem: the request never
            # reached the provider, so it is worth trying again.
            raise ModelUnavailableError(
                f"could not reach the model: {type(error).__name__}"
            ) from error

        if response.status_code in _RETRYABLE_STATUSES:
            raise ModelUnavailableError(
                f"the model returned {response.status_code}; worth retrying"
            )

        if response.status_code != 200:
            # A bad key or a malformed request fails identically next time, so
            # it is not a ModelUnavailableError. The provider's message is not
            # included: it can echo request content back.
            raise RuntimeError(
                f"the model rejected the request with {response.status_code}"
            )

        return _reply_text(response.json())


def _reply_text(body: dict) -> str:
    """Pull the text out of a Messages API response.

    Anything unexpected in the shape becomes `ModelUnavailableError` rather than
    a crash: the provider's response is as untrusted as the text inside it, and
    a worker must not die because a payload changed shape.
    """
    try:
        blocks = body["content"]
    except (KeyError, TypeError) as error:
        raise ModelUnavailableError("the model's response had no content") from error

    texts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if not any(texts):
        # A refusal or a thinking-only reply lands here. Not retried as invalid
        # output, because nothing was returned to validate.
        raise ModelUnavailableError(
            f"the model returned no text (stop_reason={body.get('stop_reason')!r})"
        )
    return "".join(texts)
