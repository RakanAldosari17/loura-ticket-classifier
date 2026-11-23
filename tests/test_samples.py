"""Tests for the sample file and the loader script (Chunk 7).

The sample file is quoted verbatim from Loura's brief (1.9), so the tests that
matter here are the ones that would catch it being edited by accident: a lost
newline in t-1010 or a filled-in subject for t-1008 would quietly change what
the whole project is demonstrating.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_PATH = Path("data/sample_tickets.json")
SAMPLES = json.loads(SAMPLE_PATH.read_text())
BY_ID = {ticket["id"]: ticket for ticket in SAMPLES}

# Section 15, with FAKE_FAILURE_RATE=0.
EXPECTED = {
    "t-1001": ("classified", "billing", "medium"),
    "t-1002": ("classified", "account", "medium"),
    "t-1003": ("classified", "technical", "high"),
    "t-1004": ("classified", "account", "low"),
    "t-1005": ("failed", None, None),
    "t-1006": ("classified", "other", "low"),
    "t-1007": ("classified", "billing", "medium"),
    "t-1008": ("classified", "other", "low"),
    "t-1009": ("classified", "technical", "medium"),
    "t-1010": ("classified", "technical", "medium"),
}


def _load_script():
    """Import the loader by path; `scripts/` is not a package."""
    path = Path("scripts/load_samples.py")
    spec = importlib.util.spec_from_file_location("load_samples", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the sample file itself ----------------------------------------------


def test_there_are_ten_tickets_with_the_expected_ids():
    assert len(SAMPLES) == 10
    assert sorted(BY_ID) == [f"t-{1001 + n}" for n in range(10)]


def test_every_ticket_has_exactly_the_three_fields():
    for ticket in SAMPLES:
        assert set(ticket) == {"id", "subject", "body"}


def test_t1008_has_an_empty_subject():
    """The brief's note on 1.9. Filling it in would remove a real edge case."""
    assert BY_ID["t-1008"]["subject"] == ""
    assert BY_ID["t-1008"]["body"] == "asdf"


def test_t1010_has_real_newlines_and_a_quoted_line():
    """The brief's other note: reproduce it exactly, with real newlines."""
    body = BY_ID["t-1010"]["body"]

    lines = body.splitlines()
    assert lines[0] == "See below."
    assert lines[2] == "> On Monday you wrote: we have fixed the issue."
    assert "E_TIMEOUT" in lines[4]
    assert "<br>" not in body


def test_t1005_still_contains_the_injection_attempt():
    """If this were softened, the project would stop demonstrating anything."""
    body = BY_ID["t-1005"]["body"].lower()

    assert "ignore all previous instructions" in body
    assert "classify it as" in body
    # Its genuine question is still there: that is the trade-off the README
    # describes, and the reason the ticket is kept rather than discarded.
    assert "invoices" in body


def test_every_sample_passes_the_front_door_rules(client):
    """All ten must be valid input: the brief says they are loadable."""
    for ticket in SAMPLES:
        response = client.post("/tickets", json=ticket)
        assert response.status_code == 202, ticket["id"]


# --- the loader script ---------------------------------------------------


def test_the_script_reads_the_committed_sample_file():
    script = _load_script()

    assert script.SAMPLES == Path("data/sample_tickets.json").resolve() or (
        script.SAMPLES.name == "sample_tickets.json"
    )
    assert json.loads(script.SAMPLES.read_text()) == SAMPLES


def test_the_script_submits_every_ticket_through_the_api(client):
    """It must go through HTTP, not reach into the database."""
    script = _load_script()

    accepted = script.submit_all(client, SAMPLES)

    assert accepted == 10
    assert client.get("/tickets").json()["total"] == 10


def test_a_second_run_reports_duplicates_and_changes_nothing(client):
    """Running it twice is normal, and must not create or alter anything."""
    script = _load_script()
    script.submit_all(client, SAMPLES)

    accepted = script.submit_all(client, SAMPLES)

    assert accepted == 0
    assert client.get("/tickets").json()["total"] == 10


def test_the_script_reports_a_service_that_is_not_running(capsys):
    """The likeliest mistake: running the loader with no service up."""
    script = _load_script()

    exit_code = script.main(["load_samples.py", "http://127.0.0.1:9"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "Could not reach the service" in output
    assert "uvicorn app.main:app" in output


# --- end to end, through the real application ---------------------------


def test_all_ten_samples_end_up_as_section_15_says(temp_db, monkeypatch):
    """The Chunk 7 check, as a test rather than something run by hand.

    Starts the real application with workers and the fake model's broken
    replies off, loads the samples through the API, and compares every result
    with section 15. Any difference is reported here instead of being
    discovered by reading a terminal.
    """
    monkeypatch.setenv("WORKER_COUNT", "2")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")

    script = _load_script()

    with TestClient(app) as client:
        assert script.submit_all(client, SAMPLES) == 10
        items = script.wait_for_results(client, len(SAMPLES))

    results = {
        ticket["id"]: (ticket["status"], ticket["category"], ticket["priority"])
        for ticket in items
    }
    assert results == EXPECTED


def test_t1005_is_the_only_flagged_sample(temp_db, monkeypatch):
    """Section 15: all nine non-injection samples are expected to score 0."""
    monkeypatch.setenv("WORKER_COUNT", "2")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")

    script = _load_script()

    with TestClient(app) as client:
        script.submit_all(client, SAMPLES)
        items = script.wait_for_results(client, len(SAMPLES))

    flagged = {ticket["id"] for ticket in items if ticket["flagged"]}
    assert flagged == {"t-1005"}


def test_the_failed_sample_is_findable_with_the_status_filter(temp_db, monkeypatch):
    """A rejected ticket is never lost: support can still find it (9.1)."""
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("FAKE_FAILURE_RATE", "0")

    script = _load_script()

    with TestClient(app) as client:
        script.submit_all(client, SAMPLES)
        script.wait_for_results(client, len(SAMPLES))
        failed = client.get("/tickets?status=failed").json()

    assert failed["total"] == 1
    assert failed["items"][0]["id"] == "t-1005"
    assert failed["items"][0]["failure_reason"] == "suspected_injection"
    # Its original text is still there for a human to read.
    assert "invoices" in failed["items"][0]["body"]
