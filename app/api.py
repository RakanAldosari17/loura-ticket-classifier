"""The front door: the HTTP routes and the single error format (section 6).

Submit a ticket, get one ticket, list tickets. This module never calls the
model: it only validates input, reads and writes the database, and replies.

Every error leaving the service — including the ones FastAPI raises before an
endpoint runs — is built by `error_response`, so a caller only ever has to
understand one error shape (6.2).
"""

import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.config import ID_PATTERN, PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from app.schemas import (
    CategoryFilter,
    PriorityFilter,
    StatusFilter,
    SubmissionAccepted,
    Ticket,
    TicketPage,
    TicketSubmission,
    ticket_from_row,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Which error code a failed validation becomes, by where the bad value was.
# FastAPI reports the location as a tuple whose first item is the part of the
# request at fault, so one handler can serve all three codes in 6.2.
_LOCATION_TO_CODE = {
    "body": "invalid_input",
    "query": "invalid_query",
    "path": "invalid_id",
}

_CODE_TO_MESSAGE = {
    "invalid_input": "The submitted ticket is not valid.",
    "invalid_query": "One or more query parameters are not valid.",
    "invalid_id": "The ticket id in the path is not valid.",
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, str]] | None = None,
) -> JSONResponse:
    """Build every error reply the service sends (6.2).

    One function, used by the endpoints and by the exception handlers, so no
    error can accidentally come back in a different shape. `details` is included
    only when there is something field-specific to say.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


def _problem(raw_message: str) -> str:
    """Tidy one of Pydantic's messages for a caller to read.

    Pydantic prefixes messages from a custom validator with "Value error, ",
    which is noise to whoever is reading the reply.
    """
    return raw_message.removeprefix("Value error, ")


def _details_from(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Turn FastAPI's validation errors into the `details` list from 6.2."""
    details = []
    for error in errors:
        location = [str(part) for part in error["loc"]]
        # Drop the leading "body"/"query"/"path": the caller knows where they
        # sent it, and what they need is the field name.
        field = ".".join(location[1:]) if len(location) > 1 else location[0]
        details.append({"field": field, "problem": _problem(error["msg"])})
    return details


async def _handle_validation_error(
    request: Request, exception: RequestValidationError
) -> JSONResponse:
    """Replace FastAPI's default validation format with the one in 6.2.

    Without this, a bad request would come back in FastAPI's own shape and the
    API would have two different error formats depending on where the error
    came from.
    """
    errors = exception.errors()
    first_location = str(errors[0]["loc"][0]) if errors and errors[0]["loc"] else "body"
    code = _LOCATION_TO_CODE.get(first_location, "invalid_input")
    return error_response(
        status_code=422,
        code=code,
        message=_CODE_TO_MESSAGE[code],
        details=_details_from(errors),
    )


async def _handle_http_exception(
    request: Request, exception: StarletteHTTPException
) -> JSONResponse:
    """Put the errors raised by endpoints into the 6.2 shape.

    An endpoint raises these with the code and message already decided, so this
    only has to re-wrap them.
    """
    detail = exception.detail
    if isinstance(detail, dict) and "code" in detail:
        return error_response(exception.status_code, detail["code"], detail["message"])
    # Anything else is a framework-generated error, such as a wrong method.
    return error_response(exception.status_code, "error", str(detail))


def register_error_handlers(app: FastAPI) -> None:
    """Point FastAPI's error handling at the format in 6.2."""
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)


class _ApiError(StarletteHTTPException):
    """An error an endpoint raises deliberately, carrying its 6.2 code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(
            status_code=status_code, detail={"code": code, "message": message}
        )


@router.post(
    "/tickets",
    status_code=202,
    response_model=SubmissionAccepted,
    summary="Submit a ticket for classification",
)
def submit_ticket(submission: TicketSubmission) -> SubmissionAccepted:
    """Store a ticket and accept it for later classification (6.3).

    202 Accepted, not 201 Created: the work the caller actually asked for has
    not happened yet. The ticket is stored and a worker will classify it.
    """
    try:
        db.insert_ticket(submission.id, submission.subject, submission.body)
    except db.DuplicateTicketError:
        # The first submission wins (4.4): nothing is updated or re-classified.
        raise _ApiError(
            status_code=409,
            code="duplicate_id",
            message=f"A ticket with id '{submission.id}' already exists.",
        ) from None

    logger.info("ticket saved id=%s status=pending", submission.id)
    return SubmissionAccepted(id=submission.id, status="pending")


@router.get(
    "/tickets",
    response_model=TicketPage,
    summary="List tickets, filtered and paginated",
)
def list_tickets(
    category: CategoryFilter | None = Query(default=None),
    priority: PriorityFilter | None = Query(default=None),
    status: StatusFilter | None = Query(default=None),
    flagged: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
) -> TicketPage:
    """Return one page of tickets, newest first (6.5).

    Every ticket is listed whatever its status; filters only narrow the result.
    An empty page is a normal reply, not an error.
    """
    rows, total = db.list_tickets(
        category=category,
        priority=priority,
        status=status,
        flagged=flagged,
        page=page,
        page_size=page_size,
    )
    # There is a next page only if this one did not reach the end.
    next_page = page + 1 if page * page_size < total else None

    return TicketPage(
        items=[ticket_from_row(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
        next_page=next_page,
    )


@router.get(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    summary="Get one ticket by id",
)
def get_ticket(ticket_id: str = Path(pattern=ID_PATTERN)) -> Ticket:
    """Return one ticket, whatever state it is in (6.4).

    Every status returns 200: a caller polling for a result needs to see
    `pending` and `under_review` too, not an error.
    """
    row = db.get_ticket(ticket_id)
    if row is None:
        raise _ApiError(
            status_code=404,
            code="not_found",
            message=f"No ticket with id '{ticket_id}'.",
        )
    return ticket_from_row(row)
