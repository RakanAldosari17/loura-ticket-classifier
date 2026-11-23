"""The background worker loop and the processing steps for one ticket (section 7).

A worker claims a pending ticket, scores it for prompt injection, calls the
model, validates the reply, and records the result. The order of those steps
(section 7.4) is deliberate and must not change.

Two things about how this runs. First, the workers live **inside the API
process** as asyncio tasks, so one command starts everything. Second, every
blocking call — SQLite and the model — is handed to a thread with
`asyncio.to_thread`, because a blocking call made directly in an asyncio task
would freeze the whole process, API included, until it returned.
"""

import asyncio
import logging

from app import classifier, db, scoring
from app.config import Settings
from app.models.base import ModelClient, ModelUnavailableError

logger = logging.getLogger(__name__)


async def run_workers(settings: Settings, model: ModelClient) -> list[asyncio.Task]:
    """Start the worker loops and return their tasks (7.1).

    The caller keeps the tasks so it can cancel them on shutdown.
    """
    tasks = [
        asyncio.create_task(worker_loop(number, settings, model), name=f"worker-{number}")
        for number in range(1, settings.worker_count + 1)
    ]
    logger.info("started %d worker(s)", len(tasks))
    return tasks


async def stop_workers(tasks: list[asyncio.Task]) -> None:
    """Cancel the worker loops and wait for them to finish (7.1).

    Graceful shutdown is not built (1.6): a ticket being processed right now is
    left `under_review`, and the next startup resets it to `pending`. Waiting
    here is still worth doing, because it lets each task's `finally` run instead
    of the process exiting mid-statement.
    """
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("stopped %d worker(s)", len(tasks))


async def worker_loop(number: int, settings: Settings, model: ModelClient) -> None:
    """Claim and process pending tickets, for ever (7.3).

    The loop must never die. If it did, the service would keep answering HTTP
    requests while silently classifying nothing — the worst kind of failure,
    because everything looks fine. So every unexpected error is caught here as
    well as in `process_ticket`, and only cancellation gets out.
    """
    logger.info("worker %d running", number)
    while True:
        try:
            ticket = await asyncio.to_thread(db.claim_next_pending)
            if ticket is None:
                await asyncio.sleep(settings.poll_interval_seconds)
                continue

            await process_ticket(ticket, settings, model)
        except asyncio.CancelledError:
            # Shutdown. Re-raised so the task really does stop.
            logger.info("worker %d cancelled", number)
            raise
        except Exception:
            # Should be unreachable: process_ticket handles its own errors. If
            # something still gets here, the loop keeps going rather than dying.
            logger.exception("worker %d: unexpected error in the loop", number)
            await asyncio.sleep(settings.poll_interval_seconds)


async def process_ticket(ticket: dict, settings: Settings, model: ModelClient) -> None:
    """Take one claimed ticket through the steps in 7.4, in that exact order.

    The ticket arrives `under_review`, and this function must leave it
    `classified` or `failed`. Any unexpected exception becomes `internal_error`
    (A30), so a bug in our code cannot leave a ticket stuck `under_review`
    waiting for a restart, and cannot send it round the loop again for ever.
    """
    ticket_id = ticket["id"]
    try:
        await _classify_ticket(ticket, settings, model)
    except asyncio.CancelledError:
        # Shutdown mid-ticket. The ticket stays `under_review` and the next
        # startup resets it to `pending` (7.2), so it is not lost.
        raise
    except Exception:
        # A bug in our code, not a model failure. The full exception goes to the
        # log with the ticket id; nothing about it is stored (A30).
        logger.exception("ticket %s: unexpected error while processing", ticket_id)
        _record(db.mark_failed(ticket_id, "internal_error", False), ticket_id, "failed")


async def _classify_ticket(
    ticket: dict, settings: Settings, model: ModelClient
) -> None:
    """The six steps of 7.4."""
    ticket_id = ticket["id"]

    # Step 1. L1b: ticket suspicion score (input check before the model).
    score = await asyncio.to_thread(
        scoring.input_score, ticket["subject"], ticket["body"]
    )
    if scoring.is_rejected(score):
        # The model is never called, and there is no retry: a retry would give
        # the injection a second attempt.
        logger.info(
            "ticket %s: failed, suspected_injection (input score %d)", ticket_id, score
        )
        _record(
            db.mark_failed(ticket_id, "suspected_injection", True), ticket_id, "failed"
        )
        return
    flagged = scoring.is_flagged(score)

    # Step 2. L2: prompt structure (instructions kept separate from the ticket).
    instructions = classifier.INSTRUCTIONS
    ticket_text = classifier.build_ticket_text(ticket["subject"], ticket["body"])

    # Step 3. The model, with one retry.
    try:
        reply = await _call_model_with_one_retry(
            model, instructions, ticket_text, settings, ticket_id
        )
    except ModelUnavailableError:
        logger.info("ticket %s: failed, model_unavailable", ticket_id)
        _record(
            db.mark_failed(ticket_id, "model_unavailable", flagged),
            ticket_id,
            "failed",
        )
        return

    # Step 4. L3: output checks, the shape half. No retry: a broken reply may be
    # an injection that got past step 1, and a retry would give it another go.
    try:
        result = classifier.validate_reply(reply)
    except classifier.InvalidOutputError as error:
        logger.info("ticket %s: failed, invalid_output (%s)", ticket_id, error)
        _record(
            db.mark_failed(ticket_id, "invalid_output", flagged), ticket_id, "failed"
        )
        return

    # Step 5. L3: the summary suspicion score, phrases only.
    summary_score = scoring.phrase_score(result.summary)
    if scoring.is_rejected(summary_score):
        logger.info(
            "ticket %s: failed, suspected_injection (summary score %d)",
            ticket_id,
            summary_score,
        )
        _record(
            db.mark_failed(ticket_id, "suspected_injection", True), ticket_id, "failed"
        )
        return
    if scoring.is_flagged(summary_score):
        flagged = True

    # Step 6. The result. `flagged` from step 1 carries through to here.
    logger.info(
        "ticket %s: classified %s/%s flagged=%s",
        ticket_id,
        result.category,
        result.priority,
        flagged,
    )
    _record(
        db.mark_classified(
            ticket_id, result.category, result.priority, result.summary, flagged
        ),
        ticket_id,
        "classified",
    )


async def _call_model_with_one_retry(
    model: ModelClient,
    instructions: str,
    ticket_text: str,
    settings: Settings,
    ticket_id: str,
) -> str:
    """Call the model, and on failure wait and try once more (7.4 step 3).

    The timeout is enforced here rather than inside the client (10.1), so it
    applies to every client: one that forgot its own timeout would otherwise
    hang this worker for ever and leave the ticket `under_review` until a
    restart.
    """
    for attempt in (1, 2):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(model.classify, instructions, ticket_text),
                timeout=settings.model_timeout_seconds,
            )
        except (ModelUnavailableError, asyncio.TimeoutError) as error:
            if attempt == 2:
                raise ModelUnavailableError(str(error)) from error
            logger.info(
                "ticket %s: model call failed (%s), retrying once in %ss",
                ticket_id,
                type(error).__name__,
                settings.retry_wait_seconds,
            )
            await asyncio.sleep(settings.retry_wait_seconds)

    # Unreachable: the loop either returns or raises.
    raise ModelUnavailableError("the model could not be reached")


def _record(applied: bool, ticket_id: str, intended: str) -> None:
    """Note when a guarded write did not apply.

    `mark_classified` and `mark_failed` only change a ticket that is still
    `under_review`. A `False` means it was not: a restart reset it to `pending`
    and another worker has taken it. That is handled, not an error — but it is
    worth a warning, because it is the only sign that two workers overlapped on
    one ticket, and silence here would hide it.
    """
    if not applied:
        logger.warning(
            "ticket %s: not marked %s because it was no longer under_review; "
            "another worker has it",
            ticket_id,
            intended,
        )
