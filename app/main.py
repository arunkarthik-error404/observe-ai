"""Application entry point.

    uvicorn app.main:app --reload

Routes live in `app/api/`, agent behaviour in `app/tools/`, and the data layer
behind `app/repositories/`. This file only wires them together.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import logging_conf
from .api import console_router, webhook_router
from .config import settings
from .tools import WRITER

logging_conf.configure()
log = logging.getLogger("claims.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("startup", extra={"backend": settings().backend})
    yield
    # Flush any in-flight post-call writes before the process goes away, so a
    # deploy or a scale-down cannot drop an interaction record.
    WRITER.shutdown(wait=True)
    log.info("shutdown")


app = FastAPI(
    title="Observe Insurance – Claims Voice Agent",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(webhook_router)
app.include_router(console_router)
