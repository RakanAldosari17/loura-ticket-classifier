"""Model clients, and the function that chooses which one to use.

`get_model_client(settings)` returns the fake model by default, or a real
provider if one is configured (section 10.4).
"""

from app.config import Settings
from app.models.base import ModelClient, ModelUnavailableError
from app.models.fake import FakeModelClient

__all__ = [
    "FakeModelClient",
    "ModelClient",
    "ModelUnavailableError",
    "get_model_client",
]


def get_model_client(settings: Settings) -> ModelClient:
    """Return the model client the settings ask for (10.4).

    Called once at startup. There is **no fallback to the fake**: if a real
    provider is configured and cannot be used, the service refuses to start.
    Falling back would be worse than failing, because the service would come up
    looking healthy while quietly classifying every ticket with a keyword
    matcher instead of the model somebody chose and is paying for.
    """
    if settings.model_provider == "fake":
        return FakeModelClient(failure_rate=settings.fake_failure_rate)

    if not settings.model_api_key:
        raise ValueError(
            f"MODEL_PROVIDER is {settings.model_provider!r} but MODEL_API_KEY is "
            "not set. Set MODEL_API_KEY, or set MODEL_PROVIDER=fake to run "
            "without a real provider."
        )

    raise ValueError(
        f"MODEL_PROVIDER is {settings.model_provider!r}, which is not supported. "
        "Only 'fake' is built. See DESIGN.md section 10.4."
    )
