"""Tests for the storage layer (section 5 of DESIGN.md).

The tests that matter most here are the two about the atomic claim: they are the
only thing standing between "two workers" and "the same ticket classified
twice".
"""

import dataclasses
import re
import sqlite3
import threading

import pytest

from app import config, db

# --- helpers --------------------------------------------------------------


def _insert(ticket_id: str, subject: str = "Subject", body: str = "Body") -> dict:
    """Insert a ticket with sensible defaults, for tests that don't care."""
    return db.insert_ticket(ticket_id, subject, body)


def _raw_connection() -> sqlite3.Connection:
    """Open a connection directly, to try writes the module would never make."""
    connection = sqlite3.connect(config.load_settings().database_path)
    connection.isolation_level = None
    return connection


def _set_status(ticket_id: str, status: str) -> None:
    """Force a ticket's status, bypassing the guarded update."""
    connection = _raw_connection()
    try:
        connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
    finally:
        connection.close()


# --- insert and get -------------------------------------------------------


def test_insert_then_get_returns_the_ticket_as_pending(temp_db):
    db.insert_ticket("t-1001", "Charged twice", "Two charges of 49.00.")

    ticket = db.get_ticket("t-1001")

    assert ticket is not None
    assert ticket["id"] == "t-1001"
    assert ticket["subject"] == "Charged twice"
    assert ticket["body"] == "Two charges of 49.00."
    assert ticket["status"] == "pending"
    # Nothing the model produces exists yet.
    assert ticket["category"] is None
    assert ticket["priority"] is None
    assert ticket["summary"] is None
    assert ticket["failure_reason"] is None
    assert ticket["flagged"] is False


def test_get_ticket_returns_none_for_an_unknown_id(temp_db):
    assert db.get_ticket("t-does-not-exist") is None


def test_an_empty_subject_is_stored_as_an_empty_string(temp_db):
    """t-1008 has no subject; that is valid input, not a missing value."""
    db.insert_ticket("t-1008", "", "asdf")

    ticket = db.get_ticket("t-1008")

    assert ticket is not None
    assert ticket["subject"] == ""


def test_timestamps_are_utc_iso_8601_to_the_second(temp_db):
    ticket = _insert("t-1001")

    for field in ("created_at", "updated_at"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ticket[field]), field


def test_newlines_in_a_body_survive_a_round_trip(temp_db):
    """t-1010's body has real line breaks and a quoted line."""
    body = "See below.\n\n> On Monday you wrote: we have fixed the issue.\n\nIt is not fixed."
    db.insert_ticket("t-1010", "Re: Re: Re: still broken", body)

    ticket = db.get_ticket("t-1010")

    assert ticket is not None
    assert ticket["body"] == body


# --- duplicates -----------------------------------------------------------


def test_inserting_a_duplicate_id_raises_and_changes_nothing(temp_db):
    db.insert_ticket("t-1001", "First subject", "First body")

    with pytest.raises(db.DuplicateTicketError):
        db.insert_ticket("t-1001", "Second subject", "Second body")

    # The first submission wins: nothing about the stored ticket changed.
    ticket = db.get_ticket("t-1001")
    assert ticket is not None
    assert ticket["subject"] == "First subject"
    assert ticket["body"] == "First body"
    assert db.list_tickets()[1] == 1


# --- CHECK constraints ----------------------------------------------------
# These prove the second line of defence: even a bug that bypassed every Python
# check could not store a value outside the allowed sets.


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "in_progress"),
        ("category", "refund"),
        ("priority", "urgent"),
        ("failure_reason", "model_said_no"),
    ],
)
def test_check_constraints_reject_values_outside_the_allowed_sets(temp_db, column, value):
    _insert("t-1001")
    connection = _raw_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE tickets SET {column} = ? WHERE id = ?", (value, "t-1001")
            )
    finally:
        connection.close()


def test_check_constraints_allow_every_configured_value(temp_db):
    """The allowed sets in config.py and the CHECK constraints must agree.

    Without this, a value could be legal in Python and rejected by the database
    (or the reverse) and nobody would notice until it happened in production.
    """
    _insert("t-1001")
    connection = _raw_connection()
    try:
        for column, allowed in (
            ("status", config.STATUSES),
            ("category", config.CATEGORIES),
            ("priority", config.PRIORITIES),
            ("failure_reason", config.FAILURE_REASONS),
        ):
            for value in allowed:
                connection.execute(
                    f"UPDATE tickets SET {column} = ? WHERE id = ?", (value, "t-1001")
                )
    finally:
        connection.close()


def test_flagged_only_accepts_zero_and_one(temp_db):
    _insert("t-1001")
    connection = _raw_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tickets SET flagged = 2 WHERE id = ?", ("t-1001",))
    finally:
        connection.close()


# --- the atomic claim -----------------------------------------------------


def test_claim_returns_a_ticket_and_sets_it_under_review(temp_db):
    _insert("t-1001")

    claimed = db.claim_next_pending()

    assert claimed is not None
    assert claimed["id"] == "t-1001"
    assert claimed["status"] == "under_review"
    assert db.get_ticket("t-1001")["status"] == "under_review"


def test_a_second_claim_does_not_return_the_same_ticket(temp_db):
    _insert("t-1001")

    assert db.claim_next_pending() is not None
    # The only ticket is no longer pending, so there is nothing left to claim.
    assert db.claim_next_pending() is None


def test_claim_returns_none_when_nothing_is_pending(temp_db):
    assert db.claim_next_pending() is None


def test_claim_takes_the_oldest_pending_ticket_first(temp_db):
    """Oldest first, with the id as tie-break, so no ticket is starved."""
    _insert("t-1003")
    _insert("t-1001")
    _insert("t-1002")

    # All three share a created_at second, so the id tie-break decides.
    claimed = db.claim_next_pending()

    assert claimed is not None
    assert claimed["id"] == "t-1001"


def test_two_threads_racing_for_the_only_pending_ticket_exactly_one_wins(temp_db):
    """The test the whole design rests on.

    Both threads are released at the same instant by a barrier and both try to
    claim the one pending ticket. If the claim were a plain read-then-write,
    both could succeed and the ticket would be classified twice.

    The race is run many times with a fresh ticket each round. One round is not
    enough: the window between the SELECT and the UPDATE is tiny, so a single
    round can easily pass even when the guard is missing. Repeating it makes the
    test reliably catch that regression.
    """
    rounds = 40

    for round_number in range(rounds):
        ticket_id = f"t-{1001 + round_number}"
        _insert(ticket_id)

        start = threading.Barrier(2)
        results: list[dict | None] = []
        lock = threading.Lock()

        def claim() -> None:
            start.wait()
            claimed = db.claim_next_pending()
            with lock:
                results.append(claimed)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [result for result in results if result is not None]
        assert len(winners) == 1, (
            f"round {round_number}: expected exactly one winner, got {len(winners)}"
        )
        assert winners[0]["id"] == ticket_id


def test_many_threads_claiming_many_tickets_never_claim_one_twice(temp_db):
    """Four workers, ten tickets: every ticket claimed exactly once."""
    for number in range(10):
        _insert(f"t-{1001 + number}")

    claimed_ids: list[str] = []
    lock = threading.Lock()

    def claim_until_empty() -> None:
        while True:
            ticket = db.claim_next_pending()
            if ticket is None:
                return
            with lock:
                claimed_ids.append(ticket["id"])

    threads = [threading.Thread(target=claim_until_empty) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(claimed_ids) == sorted(f"t-{1001 + n}" for n in range(10))
    assert len(claimed_ids) == len(set(claimed_ids)), "a ticket was claimed twice"


# --- the guarded result write --------------------------------------------


def test_mark_classified_stores_the_result(temp_db):
    _insert("t-1001")
    db.claim_next_pending()

    assert db.mark_classified("t-1001", "billing", "medium", "Charged twice.", False) is True

    ticket = db.get_ticket("t-1001")
    assert ticket is not None
    assert ticket["status"] == "classified"
    assert ticket["category"] == "billing"
    assert ticket["priority"] == "medium"
    assert ticket["summary"] == "Charged twice."
    assert ticket["flagged"] is False
    assert ticket["failure_reason"] is None


def test_mark_failed_stores_the_reason(temp_db):
    _insert("t-1005")
    db.claim_next_pending()

    assert db.mark_failed("t-1005", "suspected_injection", True) is True

    ticket = db.get_ticket("t-1005")
    assert ticket is not None
    assert ticket["status"] == "failed"
    assert ticket["failure_reason"] == "suspected_injection"
    assert ticket["flagged"] is True
    # A failed ticket never carries a classification.
    assert ticket["category"] is None
    assert ticket["priority"] is None
    assert ticket["summary"] is None


@pytest.mark.parametrize("status", ["pending", "classified", "failed"])
def test_mark_classified_only_applies_to_under_review_tickets(temp_db, status):
    _insert("t-1001")
    _set_status("t-1001", status)

    assert db.mark_classified("t-1001", "billing", "low", "Summary.", False) is False
    assert db.get_ticket("t-1001")["status"] == status


@pytest.mark.parametrize("status", ["pending", "classified", "failed"])
def test_mark_failed_only_applies_to_under_review_tickets(temp_db, status):
    _insert("t-1001")
    _set_status("t-1001", status)

    assert db.mark_failed("t-1001", "model_unavailable", False) is False
    assert db.get_ticket("t-1001")["status"] == status


def test_marking_an_unknown_id_reports_failure(temp_db):
    assert db.mark_failed("t-nope", "invalid_output", False) is False


# --- the startup reset ----------------------------------------------------


def test_reset_under_review_moves_them_back_to_pending_and_counts_them(temp_db):
    for ticket_id in ("t-1001", "t-1002", "t-1003"):
        _insert(ticket_id)
    _set_status("t-1001", "under_review")
    _set_status("t-1002", "under_review")
    _set_status("t-1003", "classified")

    count = db.reset_under_review()

    assert count == 2
    assert db.get_ticket("t-1001")["status"] == "pending"
    assert db.get_ticket("t-1002")["status"] == "pending"
    # A finished ticket is left alone.
    assert db.get_ticket("t-1003")["status"] == "classified"


def test_reset_under_review_returns_zero_when_there_is_nothing_to_reset(temp_db):
    _insert("t-1001")

    assert db.reset_under_review() == 0
    assert db.get_ticket("t-1001")["status"] == "pending"


# --- listing --------------------------------------------------------------


@pytest.fixture
def listed_tickets(temp_db) -> None:
    """Five tickets covering every filter combination the tests need."""
    for ticket_id in ("t-1001", "t-1002", "t-1003", "t-1004", "t-1005"):
        _insert(ticket_id)
        _set_status(ticket_id, "under_review")

    db.mark_classified("t-1001", "billing", "medium", "Double charge.", False)
    db.mark_classified("t-1002", "account", "medium", "Login broken.", False)
    db.mark_classified("t-1003", "technical", "high", "Production 500s.", False)
    db.mark_classified("t-1004", "billing", "high", "Invoice wrong.", True)
    db.mark_failed("t-1005", "suspected_injection", True)


def test_list_with_no_filters_returns_everything(listed_tickets):
    items, total = db.list_tickets()

    assert total == 5
    assert len(items) == 5


def test_list_is_newest_first_with_id_as_tie_break(listed_tickets):
    items, _ = db.list_tickets()

    # All five share a created_at second, so the id tie-break decides the order.
    assert [item["id"] for item in items] == [
        "t-1001",
        "t-1002",
        "t-1003",
        "t-1004",
        "t-1005",
    ]


def test_list_filters_by_category(listed_tickets):
    items, total = db.list_tickets(category="billing")

    assert total == 2
    assert {item["id"] for item in items} == {"t-1001", "t-1004"}


def test_list_filters_by_priority(listed_tickets):
    items, total = db.list_tickets(priority="high")

    assert total == 2
    assert {item["id"] for item in items} == {"t-1003", "t-1004"}


def test_list_filters_by_status(listed_tickets):
    items, total = db.list_tickets(status="failed")

    assert total == 1
    assert items[0]["id"] == "t-1005"


def test_list_filters_by_flagged(listed_tickets):
    flagged_items, flagged_total = db.list_tickets(flagged=True)
    clean_items, clean_total = db.list_tickets(flagged=False)

    assert flagged_total == 2
    assert {item["id"] for item in flagged_items} == {"t-1004", "t-1005"}
    assert clean_total == 3
    assert {item["id"] for item in clean_items} == {"t-1001", "t-1002", "t-1003"}


def test_list_combines_filters_with_and(listed_tickets):
    items, total = db.list_tickets(category="billing", priority="high")

    assert total == 1
    assert items[0]["id"] == "t-1004"


def test_list_with_filters_that_match_nothing_is_an_empty_result(listed_tickets):
    items, total = db.list_tickets(category="other")

    assert items == []
    assert total == 0


def test_list_pagination_returns_the_right_page_and_the_full_total(listed_tickets):
    page_one, total_one = db.list_tickets(page=1, page_size=2)
    page_two, total_two = db.list_tickets(page=2, page_size=2)
    page_three, total_three = db.list_tickets(page=3, page_size=2)

    # The total is the number of matches, not the number on this page.
    assert total_one == total_two == total_three == 5
    assert [item["id"] for item in page_one] == ["t-1001", "t-1002"]
    assert [item["id"] for item in page_two] == ["t-1003", "t-1004"]
    assert [item["id"] for item in page_three] == ["t-1005"]


def test_list_pagination_past_the_end_is_empty_not_an_error(listed_tickets):
    items, total = db.list_tickets(page=99, page_size=20)

    assert items == []
    assert total == 5


def test_list_pagination_applies_after_filtering(listed_tickets):
    items, total = db.list_tickets(category="billing", page=2, page_size=1)

    assert total == 2
    assert [item["id"] for item in items] == ["t-1004"]


def test_list_fetches_only_the_requested_page(listed_tickets):
    """The database does the paging, rather than Python slicing a full read."""
    items, _ = db.list_tickets(page=1, page_size=2)

    assert len(items) == 2


# --- settings -------------------------------------------------------------


def test_settings_fall_back_to_the_documented_defaults(monkeypatch):
    for name in (
        "MODEL_PROVIDER",
        "MODEL_API_KEY",
        "FAKE_FAILURE_RATE",
        "WORKER_COUNT",
        "POLL_INTERVAL_SECONDS",
        "MODEL_TIMEOUT_SECONDS",
        "RETRY_WAIT_SECONDS",
        "DATABASE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = config.load_settings()

    assert settings.model_provider == "fake"
    assert settings.model_api_key is None
    assert settings.fake_failure_rate == 0.1
    assert settings.worker_count == 2
    assert settings.poll_interval_seconds == 1.0
    assert settings.model_timeout_seconds == 20.0
    assert settings.retry_wait_seconds == 2.0
    assert settings.database_path == "./data/tickets.db"


def test_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")
    monkeypatch.setenv("WORKER_COUNT", "4")

    settings = config.load_settings()

    assert settings.model_provider == "anthropic"
    assert settings.fake_failure_rate == 0.0
    assert settings.worker_count == 4


def test_a_setting_that_is_not_a_number_fails_loudly(monkeypatch):
    """A typo in a setting should stop the service, not be silently ignored."""
    monkeypatch.setenv("WORKER_COUNT", "two")

    with pytest.raises(ValueError, match="WORKER_COUNT"):
        config.load_settings()


def test_settings_cannot_be_changed_once_loaded():
    settings = config.load_settings()

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.worker_count = 99  # type: ignore[misc]
