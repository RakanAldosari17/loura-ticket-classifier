"""The FastAPI application object, and the service's startup and shutdown.

This module is the entry point: `uvicorn app.main:app` runs the service.
Later chunks add the rest of the startup sequence here (section 7.2 of
DESIGN.md): the settings and detector checks, the reset of `under_review`
tickets left behind by a restart, and the background worker loops.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import db
from app.api import register_error_handlers, router
from app.injection_detector import load_detector

# One line per lifecycle event, at INFO (A27). Configured once, here, because
# this module is the entry point and nothing else should touch global logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the startup sequence before the service accepts requests (7.2).

    A lifespan handler runs once on startup and once on shutdown, which is what
    makes it the right place for work that must happen before the first request
    and be undone after the last one.

    So far this is steps 2 and 3 of 7.2. The settings check (step 1), the
    restart reset (step 4) and the workers (step 5) arrive in later chunks.
    """
    # Step 2: load the detector before anything can be classified. If its file
    # is missing this raises and the service does not start, which is the point:
    # a defence that quietly disappears is worse than one obviously absent.
    load_detector()
    db.init_db()
    logger.info("database ready")
    yield


app = FastAPI(
    title="Loura Support Ticket Classifier",
    description="Ingests support tickets and classifies them in the background.",
    lifespan=lifespan,
)

register_error_handlers(app)
app.include_router(router)
