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
from app.config import load_settings
from app.injection_detector import load_detector
from app.models import get_model_client

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

    So far this is steps 1 to 3 of 7.2. The restart reset (step 4) and the
    workers (step 5) arrive in Chunk 6.
    """
    # Step 1: settings, and the model client they ask for. Building the client
    # here is the startup check: a real provider with no key raises and the
    # service never comes up, rather than falling back to the fake and looking
    # healthy while classifying everything with a keyword matcher.
    settings = load_settings()
    app.state.settings = settings
    app.state.model_client = get_model_client(settings)
    logger.info("model provider %s ready", settings.model_provider)

    # Step 2: the detector. If its file is missing this raises and the service
    # does not start, which is the point: a defence that quietly disappears is
    # worse than one that is obviously absent.
    load_detector()

    # Step 3.
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
