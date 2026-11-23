"""Submit the ten sample tickets through the running service's API.

Reads `data/sample_tickets.json` and POSTs each ticket to `POST /tickets`,
printing every reply.

Run the service in one terminal and this in another:

    uvicorn app.main:app
    python scripts/load_samples.py

To reproduce the expected results in DESIGN.md section 15, start the service
with the fake model's broken replies turned off:

    FAKE_FAILURE_RATE=0 uvicorn app.main:app

This goes through the **HTTP API**, not the database, on purpose: it is the same
path the email tool and the web form backend would use, so running it exercises
the front door rather than working around it.

Pass `--wait` to poll until every ticket has been classified or failed, then
print the results table. Without it the script submits and exits, which is what
the API's 202 means: accepted, not yet classified.
"""

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "sample_tickets.json"
DEFAULT_URL = "http://127.0.0.1:8000"

# How long to wait for the workers, with --wait. Two workers polling once a
# second will finish ten tickets well inside this.
_POLL_SECONDS = 0.5
_MAX_WAIT_SECONDS = 60


def submit_all(client: httpx.Client, tickets: list[dict]) -> int:
    """POST every ticket and print the reply. Returns how many were accepted."""
    accepted = 0
    print(f"Submitting {len(tickets)} tickets to {client.base_url}")
    print("-" * 72)

    for ticket in tickets:
        try:
            response = client.post("/tickets", json=ticket)
        except httpx.RequestError as error:
            print(f"  {ticket['id']}  could not reach the service: {error}")
            continue

        body = response.json()
        if response.status_code == 202:
            accepted += 1
            print(f"  {ticket['id']}  {response.status_code}  {json.dumps(body)}")
        else:
            # A duplicate id on a second run is the normal case here, not a bug.
            print(f"  {ticket['id']}  {response.status_code}  {json.dumps(body)}")

    return accepted


def wait_for_results(client: httpx.Client, expected: int) -> list[dict]:
    """Poll the list endpoint until nothing is pending or under review."""
    print()
    print("Waiting for the workers...")
    deadline = time.monotonic() + _MAX_WAIT_SECONDS

    while time.monotonic() < deadline:
        page = client.get("/tickets", params={"page_size": 100}).json()
        items = page["items"]
        unfinished = [i for i in items if i["status"] in ("pending", "under_review")]
        if len(items) >= expected and not unfinished:
            return items
        time.sleep(_POLL_SECONDS)

    print(f"  gave up after {_MAX_WAIT_SECONDS}s; showing what there is")
    return client.get("/tickets", params={"page_size": 100}).json()["items"]


def print_results(items: list[dict]) -> None:
    """Print the results table, for comparing with section 15 by eye."""
    print()
    print(f"{'id':9} {'status':11} {'category':10} {'priority':9} {'flag':6} reason")
    print("-" * 72)
    for ticket in sorted(items, key=lambda t: t["id"]):
        print(
            f"{ticket['id']:9} {ticket['status']:11} "
            f"{str(ticket['category'] or '-'):10} "
            f"{str(ticket['priority'] or '-'):9} "
            f"{str(ticket['flagged']):6} {ticket['failure_reason'] or '-'}"
        )


def main(argv: list[str]) -> int:
    """Submit the samples, and optionally wait for their results."""
    url = DEFAULT_URL
    wait = False
    for argument in argv[1:]:
        if argument == "--wait":
            wait = True
        elif argument.startswith("http"):
            url = argument
        else:
            print(f"usage: {argv[0]} [--wait] [base-url]")
            return 2

    tickets = json.loads(SAMPLES.read_text())

    with httpx.Client(base_url=url, timeout=10.0) as client:
        try:
            client.get("/tickets", params={"page_size": 1})
        except httpx.RequestError:
            print(f"Could not reach the service at {url}.")
            print("Start it first:  uvicorn app.main:app")
            return 1

        accepted = submit_all(client, tickets)
        print()
        print(f"{accepted} of {len(tickets)} accepted (202).")

        if wait:
            print_results(wait_for_results(client, len(tickets)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
