"""Loads the trained injection detector and scores text with it (9.6).

This module owns the model file: where it is, loading it once, and turning a
probability into suspicion points. It knows nothing about tickets or workers,
and `scoring.py` adds its points to the same total as the phrase points.

The model itself is TF-IDF features feeding a logistic regression, trained
offline by `scripts/train_injection_detector.py`. It is not an LLM: it never
makes a network call, needs no API key, and costs nothing to run.
"""

import logging
from pathlib import Path
from typing import Any

import joblib

from app.config import (
    DETECTOR_STRONG_PROBABILITY,
    DETECTOR_WEAK_PROBABILITY,
    STRONG_PHRASE_POINTS,
    WEAK_PHRASE_POINTS,
)

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "injection"
TRAIN_CSV = _DATA_DIR / "train.csv"
TEST_CSV = _DATA_DIR / "test.csv"
DETECTOR_PATH = _DATA_DIR / "detector.joblib"

# The loaded model, or None before startup has loaded it. Module-level, because
# loading takes long enough that doing it per ticket would be wasteful, and the
# model never changes while the service runs.
_model: Any | None = None


class DetectorUnavailableError(RuntimeError):
    """Raised when the model file is missing or cannot be read."""


def load_detector() -> None:
    """Load the model file once, at startup (7.2, step 2).

    Called before the service accepts requests. If the file is missing the
    service stops here rather than starting without the detector: a defence
    that quietly disappears is worse than one that is obviously absent, because
    nothing downstream would report that tickets stopped being screened.
    """
    global _model
    if not DETECTOR_PATH.exists():
        raise DetectorUnavailableError(
            f"The injection detector model is missing: {DETECTOR_PATH}. "
            "Build it with: python scripts/train_injection_detector.py"
        )
    _model = joblib.load(DETECTOR_PATH)
    logger.info("injection detector loaded from %s", DETECTOR_PATH)


def injection_probability(text: str) -> float:
    """Return how injection-like this text is, from 0.0 to 1.0.

    Raises `DetectorUnavailableError` if the model was never loaded, rather
    than silently returning 0.0, which would look exactly like "this text is
    fine" and disable the defence without a trace.
    """
    if _model is None:
        raise DetectorUnavailableError(
            "The injection detector was not loaded. Call load_detector() first."
        )
    return float(_model.predict_proba([text])[0][1])


def detector_points(probability: float) -> int:
    """Turn a probability into suspicion points (9.6).

    The cut-offs reuse the phrase weights deliberately: a confident detection is
    worth the same as a strong phrase (3, enough to reject on its own), and a
    middling one the same as a weak phrase (1, enough to flag). That keeps one
    scale for the whole L1b score rather than two that have to be reconciled.
    """
    if probability >= DETECTOR_STRONG_PROBABILITY:
        return STRONG_PHRASE_POINTS
    if probability >= DETECTOR_WEAK_PROBABILITY:
        return WEAK_PHRASE_POINTS
    return 0
