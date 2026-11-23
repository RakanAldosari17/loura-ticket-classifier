"""Every SQL statement in the system (section 5 of DESIGN.md).

No ORM and no query builder: plain `sqlite3` with `?` placeholders. Keeping all
SQL in one module means a reviewer can read the whole storage layer at once, and
the atomic claim that stops two workers taking the same ticket lives here.

Concurrency approach: every function opens its own short-lived connection and
closes it again. A `sqlite3.Connection` must not be shared between threads, and
this service runs several workers in threads alongside the API, so a connection
per operation removes the question entirely. Connections are opened in
autocommit mode, so each statement commits on its own as soon as it succeeds.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import load_settings

# How long a connection waits for another writer to finish before giving up.
# SQLite allows only one writer at a time; without this, a worker writing at the
# same moment as the API would fail instantly instead of waiting its turn.
_BUSY_TIMEOUT_MS = 5000

# The CHECK constraints repeat the allowed values from config.py on purpose: even
# a bug elsewhere in the code cannot store a status, category, priority or reason
# outside these sets. The SQL is written out in full rather than generated, so it
# reads as plain SQL; `test_db.py` asserts it still matches config.py.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id             TEXT    PRIMARY KEY,
    subject        TEXT    NOT NULL,
    body           TEXT    NOT NULL,
    status         TEXT    NOT NULL
                   CHECK (status IN ('pending', 'under_review', 'classified', 'failed')),
    category       TEXT
                   CHECK (category IS NULL OR
                          category IN ('billing', 'technical', 'account', 'other')),
    priority       TEXT
                   CHECK (priority IS NULL OR priority IN ('low', 'medium', 'high')),
    summary        TEXT,
    flagged        INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    failure_reason TEXT
                   CHECK (failure_reason IS NULL OR
                          failure_reason IN ('suspected_injection',
                                             'model_unavailable',
                                             'invalid_output')),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status     ON tickets (status);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets (created_at);
"""


class DuplicateTicketError(Exception):
    """Raised when a ticket id is submitted that is already stored."""


def _connect() -> sqlite3.Connection:
    """Open a new connection to the configured database file.

    `isolation_level=None` means autocommit: every statement commits as soon as
    it runs, so a crash can never leave a half-finished write behind. The path
    is read from the settings on each call, so tests can point the whole storage
    layer at a temporary file.
    """
    connection = sqlite3.connect(load_settings().database_path)
    connection.isolation_level = None
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return connection


def _utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string, to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_ticket(row: sqlite3.Row) -> dict[str, Any]:
    """Turn a database row into a plain dictionary.

    SQLite has no boolean type, so `flagged` is stored as 0 or 1; it is turned
    back into a real boolean here, so nothing outside this module has to know.
    """
    ticket = dict(row)
    ticket["flagged"] = bool(ticket["flagged"])
    return ticket


def init_db() -> None:
    """Create the table and its indexes if they do not exist yet."""
    # The database file's folder may not exist on a clean clone.
    Path(load_settings().database_path).parent.mkdir(parents=True, exist_ok=True)

    connection = _connect()
    try:
        # Write-ahead logging lets readers carry on while one writer works, so
        # the API can read tickets while a worker is writing a result.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(_SCHEMA)
    finally:
        connection.close()


def insert_ticket(ticket_id: str, subject: str, body: str) -> None:
    """Store a new ticket as `pending`.

    Raises `DuplicateTicketError` if the id is already taken. Duplicates are
    caught by the PRIMARY KEY, not by looking first and then inserting: between
    a check and an insert, another request could store the same id.

    Returns nothing: a caller that wants the stored ticket asks for it with
    `get_ticket`, so this function has exactly one job.
    """
    now = _utc_now()
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO tickets (id, subject, body, status, flagged, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', 0, ?, ?)
            """,
            (ticket_id, subject, body, now, now),
        )
    except sqlite3.IntegrityError as error:
        # Only the primary key can cause this on an insert; anything else is a
        # real bug and should not be hidden behind a duplicate-id message.
        if "tickets.id" not in str(error):
            raise
        raise DuplicateTicketError(ticket_id) from error
    finally:
        connection.close()


def get_ticket(ticket_id: str) -> dict[str, Any] | None:
    """Return one ticket, or None if no ticket has that id."""
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    finally:
        connection.close()
    return _row_to_ticket(row) if row is not None else None


def list_tickets(
    *,
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    flagged: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """Return one page of tickets, newest first, and the total number of matches.

    Filters are optional and combined with AND. Only the requested page is
    fetched from the database, so a large table does not have to be read into
    memory to answer a request for twenty rows.

    The WHERE clause is assembled from fixed strings; every value the caller
    supplies travels as a `?` parameter, never as SQL text.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if category is not None:
        conditions.append("category = ?")
        params.append(category)
    if priority is not None:
        conditions.append("priority = ?")
        params.append(priority)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if flagged is not None:
        conditions.append("flagged = ?")
        params.append(1 if flagged else 0)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    connection = _connect()
    try:
        total = connection.execute(
            f"SELECT COUNT(*) FROM tickets{where}", params
        ).fetchone()[0]

        # Newest first, with the id as a tie-break: timestamps are only accurate
        # to the second, so without it the order of tickets saved in the same
        # second would be arbitrary and pagination could repeat or skip a row.
        rows = connection.execute(
            f"""
            SELECT * FROM tickets{where}
            ORDER BY created_at DESC, id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        connection.close()

    return [_row_to_ticket(row) for row in rows], total


def claim_next_pending() -> dict[str, Any] | None:
    """Take the oldest pending ticket and mark it `under_review`, or return None.

    This is the **atomic claim**, the one piece of SQL that makes several workers
    safe. The ticket is chosen by a SELECT, but the UPDATE repeats the condition
    `status = 'pending'`, and a single UPDATE statement cannot be interrupted
    half-way. So if two workers pick the same ticket, the first UPDATE changes
    one row and the second changes none: the loser sees `rowcount == 0` and goes
    back to waiting, instead of classifying a ticket that is already taken.
    """
    connection = _connect()
    try:
        # Oldest first, id as tie-break, so a ticket cannot be starved.
        row = connection.execute(
            """
            SELECT id FROM tickets
            WHERE status = 'pending'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None

        cursor = connection.execute(
            """
            UPDATE tickets
            SET status = 'under_review', updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (_utc_now(), row["id"]),
        )
        if cursor.rowcount != 1:
            return None  # another worker claimed it first
        claimed_id = row["id"]
    finally:
        connection.close()

    return get_ticket(claimed_id)


def _finish_under_review(
    ticket_id: str,
    *,
    status: str,
    category: str | None,
    priority: str | None,
    summary: str | None,
    failure_reason: str | None,
    flagged: bool,
) -> bool:
    """Write a final result, but only if the ticket is still `under_review`.

    One guarded UPDATE behind both `mark_classified` and `mark_failed`. The
    guard matters after a restart: the startup reset puts `under_review` tickets
    back to `pending`, and a worker from before the restart must not be able to
    overwrite the result of whoever picked the ticket up afterwards.

    Returns True if the ticket was updated, False if it was no longer
    `under_review`.
    """
    connection = _connect()
    try:
        cursor = connection.execute(
            """
            UPDATE tickets
            SET status = ?, category = ?, priority = ?, summary = ?,
                failure_reason = ?, flagged = ?, updated_at = ?
            WHERE id = ? AND status = 'under_review'
            """,
            (
                status,
                category,
                priority,
                summary,
                failure_reason,
                1 if flagged else 0,
                _utc_now(),
                ticket_id,
            ),
        )
    finally:
        connection.close()
    return cursor.rowcount == 1


def mark_classified(
    ticket_id: str,
    category: str,
    priority: str,
    summary: str,
    flagged: bool,
) -> bool:
    """Record a validated classification. Only applies to `under_review` tickets."""
    return _finish_under_review(
        ticket_id,
        status="classified",
        category=category,
        priority=priority,
        summary=summary,
        failure_reason=None,
        flagged=flagged,
    )


def mark_failed(ticket_id: str, reason: str, flagged: bool) -> bool:
    """Record a failure and its reason. Only applies to `under_review` tickets."""
    return _finish_under_review(
        ticket_id,
        status="failed",
        category=None,
        priority=None,
        summary=None,
        failure_reason=reason,
        flagged=flagged,
    )


def reset_under_review() -> int:
    """Put every `under_review` ticket back to `pending` and return the count.

    Run once at startup. A ticket is only `under_review` because some worker
    claimed it, and no worker can have survived the restart, so any ticket left
    in that state is abandoned and has to be picked up again.
    """
    connection = _connect()
    try:
        cursor = connection.execute(
            """
            UPDATE tickets
            SET status = 'pending', updated_at = ?
            WHERE status = 'under_review'
            """,
            (_utc_now(),),
        )
    finally:
        connection.close()
    return cursor.rowcount
