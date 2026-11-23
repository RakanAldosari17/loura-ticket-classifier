"""All settings and fixed limits in one place (section 11 of DESIGN.md).

Settings come from environment variables with defaults; fixed limits are
constants. Nothing else in the codebase hard-codes a limit or reads the
environment directly.
"""

import os
from dataclasses import dataclass

# --- Allowed values -------------------------------------------------------
# Defined once and used everywhere, so a status or category can never be
# spelled two different ways in two different modules. The database repeats
# these sets in its CHECK constraints (db.py); a test asserts the two agree.

STATUSES = ("pending", "under_review", "classified", "failed")
CATEGORIES = ("billing", "technical", "account", "other")
PRIORITIES = ("low", "medium", "high")
FAILURE_REASONS = ("suspected_injection", "model_unavailable", "invalid_output")

# --- Fixed limits (section 11) --------------------------------------------
# Deliberately not configurable: they are part of the design, not deployment
# knobs. Lengths are counted in characters, because word counts can be gamed
# with spacing.

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
SUBJECT_MAX_LENGTH = 200
BODY_MAX_LENGTH = 10_000
SUMMARY_MAX_LENGTH = 200

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100

# Suspicion scoring (section 9.2): 0 is clean, 1-2 is flagged, 3+ is rejected.
STRONG_PHRASE_POINTS = 3
WEAK_PHRASE_POINTS = 1
SUSPICION_FLAG_THRESHOLD = 1
SUSPICION_REJECT_THRESHOLD = 3

# Where the trained detector's probability turns into those same points (9.6).
# At or above the strong cut-off it is worth a strong signal, so it can reject a
# ticket on its own; at or above the weak one, a weak signal; below, nothing.
# Measured on the ten samples, not guessed: legitimate tickets topped out at
# 0.634 and the injection scored 0.799, so both cut-offs sit in that gap.
DETECTOR_STRONG_PROBABILITY = 0.85
DETECTOR_WEAK_PROBABILITY = 0.65


@dataclass(frozen=True)
class Settings:
    """The environment-driven settings, read once at startup.

    Frozen, so that nothing can quietly change a setting while the service is
    running: every part of the system sees the same values for its whole life.
    """

    model_provider: str
    model_api_key: str | None
    fake_failure_rate: float
    worker_count: int
    poll_interval_seconds: float
    model_timeout_seconds: float
    retry_wait_seconds: float
    database_path: str


def _env_str(name: str, default: str) -> str:
    """Read a string setting, treating an empty value as unset."""
    value = os.environ.get(name, "").strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    """Read an integer setting, failing loudly if the value is not a number."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None


def _env_float(name: str, default: float) -> float:
    """Read a decimal setting, failing loudly if the value is not a number."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _validate(settings: Settings) -> None:
    """Reject settings that parse as numbers but make no sense.

    Being the right type is not the same as being usable: `WORKER_COUNT=0`
    starts a service that never classifies anything, and a negative timeout
    fails every model call. Each of these would look like a mysterious bug at
    runtime, so they are caught here, at startup, with the name of the setting
    that is wrong.
    """
    if settings.worker_count < 1:
        raise ValueError(
            f"WORKER_COUNT must be at least 1, got {settings.worker_count}; "
            "with no workers, no ticket is ever classified"
        )
    if not 0.0 <= settings.fake_failure_rate <= 1.0:
        raise ValueError(
            f"FAKE_FAILURE_RATE must be between 0 and 1, got {settings.fake_failure_rate}"
        )
    for name, value in (
        ("POLL_INTERVAL_SECONDS", settings.poll_interval_seconds),
        ("MODEL_TIMEOUT_SECONDS", settings.model_timeout_seconds),
        ("RETRY_WAIT_SECONDS", settings.retry_wait_seconds),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be greater than 0, got {value}")


def load_settings() -> Settings:
    """Build the settings from the environment, falling back to the defaults.

    A bad value stops the service with a clear message rather than being
    silently replaced by the default: a typo in a deployment setting should be
    obvious, not invisible.
    """
    settings = Settings(
        model_provider=_env_str("MODEL_PROVIDER", "fake"),
        model_api_key=os.environ.get("MODEL_API_KEY") or None,
        fake_failure_rate=_env_float("FAKE_FAILURE_RATE", 0.1),
        worker_count=_env_int("WORKER_COUNT", 2),
        poll_interval_seconds=_env_float("POLL_INTERVAL_SECONDS", 1.0),
        model_timeout_seconds=_env_float("MODEL_TIMEOUT_SECONDS", 20.0),
        retry_wait_seconds=_env_float("RETRY_WAIT_SECONDS", 2.0),
        database_path=_env_str("DATABASE_PATH", "./data/tickets.db"),
    )
    _validate(settings)
    return settings
