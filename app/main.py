"""The FastAPI application object, and the service's startup and shutdown.

This module is the entry point: `uvicorn app.main:app` runs the service.
Later chunks add the startup sequence here (section 7.2 of DESIGN.md): check the
settings, create the database, reset any `under_review` tickets left behind by a
restart, and start the background worker loops.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Loura Support Ticket Classifier",
    description="Ingests support tickets and classifies them in the background.",
)
