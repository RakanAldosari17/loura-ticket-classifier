"""The suspicion score and its phrase lists (section 9 of DESIGN.md).

One scoring function, used twice: on the ticket text before the model is called,
and on the summary the model returns.

The score is not a verdict. It is a number the worker turns into a decision:
0 is clean, 1-2 means classify but flag for a human, 3 or more means reject. The
preference throughout is false positives over false negatives — a suspicious
ticket is never presented as a trusted classification, but it is never lost
either, because it is stored as `failed` with a reason and can be found with the
`status` filter.
"""

import re

from app.config import (
    STRONG_PHRASE_POINTS,
    SUSPICION_FLAG_THRESHOLD,
    SUSPICION_REJECT_THRESHOLD,
    WEAK_PHRASE_POINTS,
)

# Section 9.2. Strong phrases are worth 3 points: one is enough to reject a
# ticket on its own. Weak phrases are worth 1: they only matter in combination.
#
# `refund` and `urgent` are deliberately absent, because real tickets use them:
# t-1001 asks for a refund and t-1006 says "not urgent". The `you are now`
# family is absent too — matching is plain substring, so `you are now a` fires
# inside "you are now auto-renewing". Role-change wording is the detector's job.
STRONG_PHRASES = (
    "ignore all previous instructions",
    "ignore previous instructions",
    "ignore the above",
    "disregard previous instructions",
    "system prompt",
    "classify it as",
    "classify this as",
    "summarise it as",
    "summarize it as",
)

WEAK_PHRASES = (
    "approved",
    "has been granted",
    "refund issued",
    "from the ceo",
    "this ticket is from",
)


def normalise(text: str) -> str:
    """Prepare text for matching (9.3): lower case, whitespace collapsed.

    Collapsing runs of whitespace means a phrase split across a line break, or
    padded with tabs, still matches. Without it, "ignore all\\nprevious
    instructions" would slip past a list that contains the same words.
    """
    return re.sub(r"\s+", " ", text).lower()


def phrase_score(text: str) -> int:
    """Return the phrase points for one piece of text (9.2, 9.3).

    Each distinct phrase counts at most once, however often it appears: a
    phrase repeated twenty times is one signal, not twenty, and without this a
    single repeated word could outvote everything else.
    """
    normalised = normalise(text)
    strong_hits = sum(1 for phrase in STRONG_PHRASES if phrase in normalised)
    weak_hits = sum(1 for phrase in WEAK_PHRASES if phrase in normalised)
    return strong_hits * STRONG_PHRASE_POINTS + weak_hits * WEAK_PHRASE_POINTS


def is_flagged(score: int) -> bool:
    """True if the score means "classify this, but have a human look" (1-2)."""
    return SUSPICION_FLAG_THRESHOLD <= score < SUSPICION_REJECT_THRESHOLD


def is_rejected(score: int) -> bool:
    """True if the score means "do not send this to the model" (3 or more)."""
    return score >= SUSPICION_REJECT_THRESHOLD


def ticket_text(subject: str, body: str) -> str:
    """Join a ticket's subject and body into the text L1b scores (A19).

    Both are attacker-controlled, so both are scored, as one piece of text.

    Note that the newline does **not** isolate them: `normalise` collapses it to
    a space before matching, so a phrase split across the two — subject "ignore
    the", body "above…" — still matches. That is the behaviour we want, since
    splitting a phrase across the fields is an obvious way to try to dodge the
    list. The cost is a false positive if a real subject ends and a real body
    begins with the two halves of a phrase, which is far-fetched, and the
    preference is false positives over false negatives (9.1).
    """
    return f"{subject}\n{body}"
