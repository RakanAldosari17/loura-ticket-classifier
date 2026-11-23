"""Request and response shapes, and the input rules for a submitted ticket.

This module is L1a: front door input rules (shape and size checks).

The input rules (section 4.2) live here: id pattern and length, subject and body
limits, and the rejection of unknown extra fields.

These classes are Pydantic models, which is how FastAPI validates input: the
rules are declared as types and constraints, and FastAPI rejects anything that
does not match before the endpoint function ever runs. That is what makes the
front door's promise — "nothing invalid is ever stored" — structural rather than
something each endpoint has to remember to check.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import (
    BODY_MAX_LENGTH,
    ID_PATTERN,
    SUBJECT_MAX_LENGTH,
)


class TicketSubmission(BaseModel):
    """The body of `POST /tickets` (section 4.2).

    `extra="forbid"` rejects unknown fields (A12). A caller sending `prioriti`
    instead of `priority` should be told, not silently ignored — and a ticket
    intake that quietly drops fields is a bad place to discover a typo.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    subject: str = Field(max_length=SUBJECT_MAX_LENGTH)
    body: str = Field(max_length=BODY_MAX_LENGTH)

    @field_validator("body")
    @classmethod
    def body_must_not_be_only_whitespace(cls, value: str) -> str:
        """A body of spaces or newlines is not a ticket (A11).

        The stored body keeps its original spacing; this only rejects a body
        that has nothing in it at all.
        """
        if not value.strip():
            raise ValueError("must contain at least one non-whitespace character")
        return value


class SubmissionAccepted(BaseModel):
    """The reply to an accepted submission (6.3).

    Deliberately the same for every accepted ticket: it says the ticket was
    stored and is waiting, and nothing else. Later checks happen in a worker,
    and revealing anything about them here would tell someone probing the
    system which of their tickets tripped a defence.
    """

    id: str
    status: str


class Ticket(BaseModel):
    """One ticket as the API returns it (6.4).

    Used by both "get one" and "list", so the two can never drift apart.
    """

    id: str
    subject: str
    body: str
    status: str
    category: str | None
    priority: str | None
    summary: str | None
    flagged: bool
    failure_reason: str | None
    created_at: str
    updated_at: str


class TicketPage(BaseModel):
    """One page of tickets (6.5)."""

    items: list[Ticket]
    page: int
    page_size: int
    total: int
    next_page: int | None


class ErrorDetail(BaseModel):
    """One thing that was wrong with the input (6.2)."""

    field: str
    problem: str


class ErrorBody(BaseModel):
    """The contents of an error reply (6.2)."""

    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    """Every error the API returns, in one shape (6.2)."""

    error: ErrorBody


def ticket_from_row(row: dict[str, Any]) -> Ticket:
    """Turn a database row into the API's ticket object.

    Written once and used by both read endpoints, so "get one" and "list" can
    never disagree about what a ticket looks like.
    """
    return Ticket(**row)


# The filter values the list endpoint accepts. `Literal` means FastAPI itself
# rejects anything outside these sets, using the same values as config.py.
CategoryFilter = Literal["billing", "technical", "account", "other"]
PriorityFilter = Literal["low", "medium", "high"]
StatusFilter = Literal["pending", "under_review", "classified", "failed"]
