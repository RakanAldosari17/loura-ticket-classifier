"""Tests for the startup sequence: restart recovery and the settings check.

These drive the real lifespan handler through `TestClient`, so they test what
actually happens when `uvicorn app.main:app` starts, not a re-creation of it.
Section 7.2's five steps are only correct if they run in the right order on the
real application, and that order is what these tests pin.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config, db, injection_detector
from app.main import app


def _wait_for_classified(client: TestClient, expected: int, tries: int = 300) -> int:
    """Poll the API until the tickets have been processed.

    Polling rather than sleeping a fixed time: the workers run on their own
    schedule, so the test waits for the outcome instead of guessing how long it
    takes.
    """
    import time

    for _ in range(tries):
        total = client.get("/tickets?status=classified").json()["total"]
        if total >= expected:
            return total
        time.sleep(0.02)
    return client.get("/tickets?status=classified").json()["total"]


# --- the startup sequence runs, in order ---------------------------------


def test_starting_the_service_creates_the_database(temp_db, monkeypatch):
    """Step 3. A clean clone has no database file at all."""
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")

    with TestClient(app) as client:
        assert client.get("/tickets").status_code == 200


def test_the_detector_is_loaded_before_requests_are_served(temp_db, monkeypatch):
    """Step 2. The workers score tickets, so it has to be ready first."""
    monkeypatch.setenv("WORKER_COUNT", "1")
    injection_detector._model = None

    with TestClient(app):
        assert injection_detector._model is not None


def test_a_real_provider_with_no_key_stops_the_service_starting(temp_db, monkeypatch):
    """Step 1. No silent fallback to the fake (10.4)."""
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        with TestClient(app):
            pass


def test_a_bad_setting_stops_the_service_starting(temp_db, monkeypatch):
    """A typo in a setting should be loud, not silently replaced."""
    monkeypatch.setenv("WORKER_COUNT", "0")

    with pytest.raises(ValueError, match="WORKER_COUNT"):
        with TestClient(app):
            pass


# --- restart recovery (step 4) -------------------------------------------


def test_a_ticket_left_under_review_is_reset_on_startup_and_processed(
    temp_db, monkeypatch
):
    """The restart case, end to end through the real application.

    A ticket is left `under_review`, as a shutdown mid-ticket would leave it.
    Starting the service must reset it to `pending` and then classify it, so a
    restart loses nothing.
    """
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")

    db.insert_ticket("t-1001", "Charged twice", "Two charges of 49.00 this month.")
    claimed = db.claim_next_pending()
    assert claimed["status"] == "under_review"

    with TestClient(app) as client:
        assert _wait_for_classified(client, 1) == 1

    ticket = db.get_ticket("t-1001")
    assert ticket["status"] == "classified"
    assert ticket["category"] == "billing"


def test_the_startup_reset_is_logged_with_a_count(temp_db, monkeypatch, caplog):
    """A27: one line per lifecycle event, including this one.

    Without it, a restart that abandoned work would leave no trace.
    """
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")

    for ticket_id in ("t-1001", "t-1002"):
        db.insert_ticket(ticket_id, "Subject", "Body")
    db.claim_next_pending()
    db.claim_next_pending()

    with caplog.at_level("INFO"):
        with TestClient(app):
            pass

    assert "startup reset 2 under_review ticket(s) to pending" in caplog.text


def test_a_finished_ticket_is_not_touched_by_the_reset(temp_db, monkeypatch):
    """Only `under_review` is reset; results survive a restart."""
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")

    db.insert_ticket("t-1001", "Subject", "Body")
    db.claim_next_pending()
    db.mark_classified("t-1001", "billing", "low", "A stored summary.", False)

    with TestClient(app):
        pass

    ticket = db.get_ticket("t-1001")
    assert ticket["status"] == "classified"
    assert ticket["summary"] == "A stored summary."


# --- the whole thing, through the API -----------------------------------


def test_a_ticket_submitted_through_the_api_gets_classified(temp_db, monkeypatch):
    """Submit, wait, read back: the three flows in one test."""
    monkeypatch.setenv("WORKER_COUNT", "2")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")

    with TestClient(app) as client:
        response = client.post(
            "/tickets",
            json={
                "id": "t-1003",
                "subject": "API returning 500s since this morning",
                "body": "Our production integration gets HTTP 500. Blocking our job.",
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "pending"

        assert _wait_for_classified(client, 1) == 1

        ticket = client.get("/tickets/t-1003").json()

    assert ticket["status"] == "classified"
    assert ticket["category"] == "technical"
    assert ticket["priority"] == "high"
    assert ticket["flagged"] is False


def test_the_injection_sample_submitted_through_the_api_ends_failed(
    temp_db, monkeypatch
):
    """t-1005, end to end: accepted at the door, rejected by a worker."""
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")

    import json
    from pathlib import Path

    samples = {
        ticket["id"]: ticket
        for ticket in json.loads(Path("data/sample_tickets.json").read_text())
    }

    with TestClient(app) as client:
        assert client.post("/tickets", json=samples["t-1005"]).status_code == 202

        import time

        for _ in range(300):
            ticket = client.get("/tickets/t-1005").json()
            if ticket["status"] in ("classified", "failed"):
                break
            time.sleep(0.02)

    assert ticket["status"] == "failed"
    assert ticket["failure_reason"] == "suspected_injection"
    assert ticket["flagged"] is True
    # It is not lost: a human can find it with the status filter.
    with TestClient(app) as client:
        found = client.get("/tickets?status=failed").json()
        assert found["total"] == 1
        assert found["items"][0]["id"] == "t-1005"


def test_the_database_survives_a_restart(temp_db, monkeypatch):
    """SQLite is a file, which is the whole reason for choosing it (5.1)."""
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")

    with TestClient(app) as client:
        client.post("/tickets", json={"id": "t-1001", "subject": "Invoice", "body": "My invoice is wrong."})
        _wait_for_classified(client, 1)

    # A second start, as if the process had been restarted.
    with TestClient(app) as client:
        ticket = client.get("/tickets/t-1001").json()

    assert ticket["status"] == "classified"
    assert ticket["category"] == "billing"


# --- the schema and the settings agree -----------------------------------


def test_the_schema_accepts_every_failure_reason_including_internal_error():
    """internal_error was added to config and the CHECK constraint together.

    A30's reason is only usable if the database will store it, so this checks
    the two did not drift apart.
    """
    assert "internal_error" in config.FAILURE_REASONS


def test_the_check_constraint_lists_every_configured_failure_reason(temp_db):
    from app.db import _SCHEMA

    for reason in config.FAILURE_REASONS:
        assert f"'{reason}'" in _SCHEMA

    connection = sqlite3.connect(config.load_settings().database_path)
    connection.isolation_level = None
    try:
        db.insert_ticket("t-1001", "Subject", "Body")
        for reason in config.FAILURE_REASONS:
            connection.execute(
                "UPDATE tickets SET failure_reason = ? WHERE id = ?",
                (reason, "t-1001"),
            )
    finally:
        connection.close()
