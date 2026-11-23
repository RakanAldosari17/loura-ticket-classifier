"""Tests for reading tickets back: get one, list with filters and pagination."""

import sqlite3

import pytest

from app import config, db
from app.schemas import CategoryFilter, PriorityFilter, StatusFilter

VALID = {"id": "t-1001", "subject": "Charged twice", "body": "Two charges of 49.00."}


def _assert_error_shape(payload: dict, code: str, with_details: bool) -> None:
    """Every error must match section 6.2, whatever produced it."""
    assert set(payload) == {"error"}
    error = payload["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    if with_details:
        assert isinstance(error["details"], list) and error["details"]
    else:
        assert "details" not in error


def _set_created_at(ticket_id: str, created_at: str) -> None:
    """Force a ticket's created_at, so ordering tests do not depend on the clock.

    Timestamps are only accurate to the second, so a loop that straddles a second
    boundary would change the order and make these tests fail at random.
    """
    connection = sqlite3.connect(config.load_settings().database_path)
    connection.isolation_level = None
    try:
        connection.execute(
            "UPDATE tickets SET created_at = ? WHERE id = ?", (created_at, ticket_id)
        )
    finally:
        connection.close()


@pytest.fixture
def five_tickets(client):
    """Five tickets covering every filter the tests need.

    Timestamps descend from t-1001 to t-1005, so "newest first" is exactly that
    order however long the fixture takes to run.
    """
    for number, ticket_id in enumerate(
        ("t-1001", "t-1002", "t-1003", "t-1004", "t-1005")
    ):
        client.post("/tickets", json={"id": ticket_id, "subject": "s", "body": "b"})
        _set_created_at(ticket_id, f"2026-11-23T10:00:{5 - number:02d}Z")
        db.claim_next_pending()

    db.mark_classified("t-1001", "billing", "medium", "Double charge.", False)
    db.mark_classified("t-1002", "account", "medium", "Login broken.", False)
    db.mark_classified("t-1003", "technical", "high", "Production 500s.", False)
    db.mark_classified("t-1004", "billing", "high", "Invoice wrong.", True)
    db.mark_failed("t-1005", "suspected_injection", True)
    return client


# --- get one ticket (6.4) -------------------------------------------------


def test_get_returns_the_whole_ticket(client):
    client.post("/tickets", json=VALID)

    response = client.get("/tickets/t-1001")

    assert response.status_code == 200
    assert response.json() == {
        "id": "t-1001",
        "subject": "Charged twice",
        "body": "Two charges of 49.00.",
        "status": "pending",
        "category": None,
        "priority": None,
        "summary": None,
        "flagged": False,
        "failure_reason": None,
        "created_at": response.json()["created_at"],
        "updated_at": response.json()["updated_at"],
    }


def test_get_returns_200_for_every_status(client):
    """A caller polling for a result must see the in-between states too."""
    for ticket_id in ("t-1001", "t-1002", "t-1003"):
        client.post("/tickets", json={"id": ticket_id, "subject": "s", "body": "b"})

    db.claim_next_pending()  # t-1001 -> under_review
    db.mark_classified("t-1001", "billing", "low", "Summary.", False)
    db.claim_next_pending()  # t-1002 -> under_review
    db.mark_failed("t-1002", "model_unavailable", False)
    db.claim_next_pending()  # t-1003 -> under_review, left there

    for ticket_id, expected in (
        ("t-1001", "classified"),
        ("t-1002", "failed"),
        ("t-1003", "under_review"),
    ):
        response = client.get(f"/tickets/{ticket_id}")
        assert response.status_code == 200
        assert response.json()["status"] == expected


def test_a_classified_ticket_shows_its_result(client):
    client.post("/tickets", json=VALID)
    db.claim_next_pending()
    db.mark_classified("t-1001", "billing", "medium", "Charged twice.", True)

    body = client.get("/tickets/t-1001").json()

    assert body["category"] == "billing"
    assert body["priority"] == "medium"
    assert body["summary"] == "Charged twice."
    assert body["flagged"] is True
    assert body["failure_reason"] is None


def test_a_failed_ticket_shows_its_reason_and_no_classification(client):
    client.post("/tickets", json=VALID)
    db.claim_next_pending()
    db.mark_failed("t-1001", "suspected_injection", True)

    body = client.get("/tickets/t-1001").json()

    assert body["status"] == "failed"
    assert body["failure_reason"] == "suspected_injection"
    assert body["category"] is None
    assert body["priority"] is None
    assert body["summary"] is None


def test_an_unknown_id_returns_404(client):
    response = client.get("/tickets/t-9999")

    assert response.status_code == 404
    _assert_error_shape(response.json(), "not_found", with_details=False)


@pytest.mark.parametrize("bad_id", ["t 1001", "t@1001", "a" * 65, "t.1001", "t+1001"])
def test_a_malformed_id_returns_422_invalid_id(client, bad_id):
    response = client.get(f"/tickets/{bad_id}")

    assert response.status_code == 422, bad_id
    _assert_error_shape(response.json(), "invalid_id", with_details=True)


def test_a_malformed_id_is_told_apart_from_an_unknown_one(client):
    """Different problems get different codes, so a caller can tell them apart."""
    assert client.get("/tickets/t-9999").json()["error"]["code"] == "not_found"
    assert client.get("/tickets/t 9999").json()["error"]["code"] == "invalid_id"


def test_an_id_containing_a_slash_is_a_different_path_not_a_bad_id(client):
    """An encoded slash never reaches the endpoint.

    `/tickets/t%2F1001` decodes to `/tickets/t/1001`, which matches no route, so
    it is a 404 from routing rather than an invalid id. Worth pinning: it looks
    like it should be `invalid_id` until you see why it cannot be.
    """
    response = client.get("/tickets/t%2F1001")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/nope", 404, "not_found"),
        ("GET", "/tickets/t/1001", 404, "not_found"),
        ("DELETE", "/tickets/t-1001", 405, "method_not_allowed"),
        ("PUT", "/tickets", 405, "method_not_allowed"),
    ],
)
def test_errors_from_routing_use_the_route_level_codes(client, method, path, status, code):
    """Errors raised before any endpoint runs still match 6.2.

    An unknown path and an unsupported method get their own codes, so a caller
    can tell "no such ticket" from "no such endpoint" without reading the
    message. FastAPI's own error format never leaks out.
    """
    response = client.request(method, path)

    assert response.status_code == status
    _assert_error_shape(response.json(), code, with_details=False)


def test_an_unknown_ticket_and_an_unknown_route_both_say_not_found(client):
    """Both are genuinely 'there is nothing here', so both use not_found."""
    assert client.get("/tickets/t-9999").json()["error"]["code"] == "not_found"
    assert client.get("/nope").json()["error"]["code"] == "not_found"


# --- list tickets (6.5) ---------------------------------------------------


def test_list_with_no_filters_returns_everything(five_tickets):
    body = five_tickets.get("/tickets").json()

    assert body["total"] == 5
    assert len(body["items"]) == 5
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["next_page"] is None


def test_list_returns_an_empty_page_not_an_error(client):
    response = client.get("/tickets")

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "page": 1,
        "page_size": 20,
        "total": 0,
        "next_page": None,
    }


def test_list_items_have_the_same_shape_as_get_one(five_tickets):
    """Both read paths build the ticket object with the same function."""
    listed = five_tickets.get("/tickets").json()["items"][0]
    fetched = five_tickets.get(f"/tickets/{listed['id']}").json()

    assert listed == fetched


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        ("category=billing", {"t-1001", "t-1004"}),
        ("category=technical", {"t-1003"}),
        ("priority=high", {"t-1003", "t-1004"}),
        ("priority=medium", {"t-1001", "t-1002"}),
        ("status=failed", {"t-1005"}),
        ("status=classified", {"t-1001", "t-1002", "t-1003", "t-1004"}),
        ("flagged=true", {"t-1004", "t-1005"}),
        ("flagged=false", {"t-1001", "t-1002", "t-1003"}),
        ("category=billing&priority=high", {"t-1004"}),
        ("category=billing&flagged=false", {"t-1001"}),
        ("status=classified&flagged=true", {"t-1004"}),
        ("category=other", set()),
    ],
)
def test_list_filters(five_tickets, query, expected_ids):
    body = five_tickets.get(f"/tickets?{query}").json()

    assert {item["id"] for item in body["items"]} == expected_ids
    assert body["total"] == len(expected_ids)


def test_pagination_reports_the_next_page_until_the_last(five_tickets):
    page_one = five_tickets.get("/tickets?page=1&page_size=2").json()
    page_two = five_tickets.get("/tickets?page=2&page_size=2").json()
    page_three = five_tickets.get("/tickets?page=3&page_size=2").json()

    assert [item["id"] for item in page_one["items"]] == ["t-1001", "t-1002"]
    assert page_one["next_page"] == 2
    assert [item["id"] for item in page_two["items"]] == ["t-1003", "t-1004"]
    assert page_two["next_page"] == 3
    assert [item["id"] for item in page_three["items"]] == ["t-1005"]
    # The last page says there is nothing after it.
    assert page_three["next_page"] is None
    # The total is the number of matches, not the number on this page.
    assert page_one["total"] == page_two["total"] == page_three["total"] == 5


def test_next_page_is_null_when_the_page_exactly_fills_the_result(five_tickets):
    """A page that ends exactly on the last row has no next page."""
    body = five_tickets.get("/tickets?page=1&page_size=5").json()

    assert len(body["items"]) == 5
    assert body["next_page"] is None


def test_pagination_applies_after_filtering(five_tickets):
    body = five_tickets.get("/tickets?category=billing&page=2&page_size=1").json()

    assert [item["id"] for item in body["items"]] == ["t-1004"]
    assert body["total"] == 2
    assert body["next_page"] is None


def test_a_page_past_the_end_is_empty_not_an_error(five_tickets):
    response = five_tickets.get("/tickets?page=99")

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 5


# --- bad queries (6.5) ----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "category=refund",
        "category=Billing",
        "priority=urgent",
        "priority=HIGH",
        "status=in_progress",
        "flagged=maybe",
        "page=0",
        "page=-1",
        "page=abc",
        "page_size=0",
        "page_size=101",
        "page_size=abc",
    ],
)
def test_a_bad_query_value_returns_422_invalid_query(client, query):
    response = client.get(f"/tickets?{query}")

    assert response.status_code == 422, query
    _assert_error_shape(response.json(), "invalid_query", with_details=True)


def test_the_page_size_limit_is_inclusive(five_tickets):
    """100 is allowed; 101 is not."""
    assert five_tickets.get("/tickets?page_size=100").status_code == 200
    assert five_tickets.get("/tickets?page_size=101").status_code == 422


# --- the filter values match config.py ------------------------------------
# The API declares its allowed filter values separately from config.py so that
# FastAPI can reject bad ones itself. These tests stop the two drifting apart.


def test_the_api_filter_values_match_config():
    assert set(CategoryFilter.__args__) == set(config.CATEGORIES)
    assert set(PriorityFilter.__args__) == set(config.PRIORITIES)
    assert set(StatusFilter.__args__) == set(config.STATUSES)


def test_every_configured_value_is_accepted_as_a_filter(client):
    for name, values in (
        ("category", config.CATEGORIES),
        ("priority", config.PRIORITIES),
        ("status", config.STATUSES),
    ):
        for value in values:
            response = client.get(f"/tickets?{name}={value}")
            assert response.status_code == 200, f"{name}={value}"
