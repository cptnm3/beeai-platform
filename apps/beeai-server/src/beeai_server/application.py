# Copyright 2025 © BeeAI a Series of LF Projects, LLC
# SPDX-License-Identifier: Apache-2.0

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Iterable

import procrastinate
from acp_sdk import ACPError
from acp_sdk.server.errors import (
    acp_error_handler,
    validation_exception_handler,
    catch_all_exception_handler,
    http_exception_handler as acp_http_exception_handler,
)
from starlette.requests import Request

from beeai_server.run_workers import run_workers
from beeai_server.utils.fastapi import NoCacheStaticFiles
from fastapi import FastAPI, APIRouter
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import ORJSONResponse
from kink import inject, di, Container
from starlette.responses import FileResponse
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.exceptions import HTTPException as StarletteHttpException
from opentelemetry.metrics import get_meter, Observation, CallbackOptions
from fastapi.exceptions import RequestValidationError

from beeai_server.telemetry import INSTRUMENTATION_NAME, shutdown_telemetry
from beeai_server.bootstrap import bootstrap_dependencies_sync
from beeai_server.configuration import Configuration
from beeai_server.exceptions import (
    ManifestLoadError,
    DuplicateEntityError,
    PlatformError,
)
from beeai_server.api.routes.provider import router as provider_router
from beeai_server.api.routes.acp import router as acp_router
from beeai_server.api.routes.env import router as env_router
from beeai_server.api.routes.files import router as files_router
from beeai_server.api.routes.llm import router as llm_router
from beeai_server.api.routes.ui import router as ui_router
from beeai_server.api.routes.vector_stores import router as vector_stores_router

logger = logging.getLogger(__name__)


def extract_messages(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [(exc_type, msg) for e in exc.exceptions for exc_type, msg in extract_messages(e)]
    else:
        return [(type(exc).__name__, str(exc))]


def register_global_exception_handlers(app: FastAPI):
    @app.exception_handler(PlatformError)
    async def entity_not_found_exception_handler(request, exc: ManifestLoadError | DuplicateEntityError):
        return await http_exception_handler(request, HTTPException(status_code=exc.status_code, detail=str(exc)))

    @app.exception_handler(Exception)
    @app.exception_handler(HTTPException)
    async def custom_http_exception_handler(request: Request, exc):
        """Include detail in all unhandled exceptions.

        This is not the beset security practice as it can reveal details about the internal workings of this service,
        but this is an open-source service anyway, so the risk is acceptable
        """

        logger.error("Error during HTTP request: %s", repr(extract_messages(exc)))

        if request.url.path.startswith("/api/v1/acp"):
            match exc:
                case ACPError():
                    return await acp_error_handler(request, exc)
                case StarletteHttpException():
                    return await acp_http_exception_handler(request, exc)
                case RequestValidationError():
                    return await validation_exception_handler(request, exc)
                case _:
                    return await catch_all_exception_handler(request, exc)

        match exc:
            case HTTPException():
                exception = exc
            case _:
                exception = HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=repr(extract_messages(exc)))
        return await http_exception_handler(request, exception)


def mount_routes(app: FastAPI):
    static_directory = pathlib.Path(__file__).parent.joinpath("static")
    if not static_directory.joinpath("index.html").exists():  # this check is for running locally
        raise RuntimeError("Could not find static files -- ensure that beeai-ui is built: `mise build:beeai-ui`")

    ui_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    ui_app.mount("/", NoCacheStaticFiles(directory=static_directory, html=True))
    ui_app.add_exception_handler(
        404,
        lambda _req, _exc: FileResponse(
            static_directory / "index.html",
            status_code=200,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
        ),
    )

    server_router = APIRouter()
    server_router.include_router(acp_router, prefix="/acp")
    server_router.include_router(provider_router, prefix="/providers", tags=["providers"])
    server_router.include_router(env_router, prefix="/variables", tags=["variables"])
    server_router.include_router(files_router, prefix="/files", tags=["files"])
    server_router.include_router(llm_router, prefix="/llm", tags=["llm"])
    server_router.include_router(ui_router, prefix="/ui", tags=["ui"])
    server_router.include_router(vector_stores_router, prefix="/vector_stores", tags=["vector_stores"])

    app.mount("/healthcheck", lambda: "OK")
    app.include_router(server_router, prefix="/api/v1", tags=["provider"])
    app.mount("/", ui_app)


def register_telemetry():
    meter = get_meter(INSTRUMENTATION_NAME)

    def scrape_platform_status(options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(value=1)

    meter.create_observable_gauge("platform_status", callbacks=[scrape_platform_status])

    # TODO: extract to a separate "metrics exporter" pod
    # def scrape_providers_by_status(options: CallbackOptions) -> Iterable[Observation]:
    #     providers = provider_container.loaded_providers.values()
    #     for status in ProviderStatus:
    #         count = 0
    #         for provider in providers:
    #             if provider.state == status:
    #                 count += 1
    #         yield Observation(
    #             value=count,
    #             attributes={
    #                 "status": status,
    #             },
    #         )

    # meter.create_observable_gauge("providers_by_status", callbacks=[scrape_providers_by_status])


def app(*, dependency_overrides: Container | None = None) -> FastAPI:
    """Entrypoint for API application, called by Uvicorn"""

    logger.info("Bootstrapping dependencies...")
    bootstrap_dependencies_sync(dependency_overrides=dependency_overrides)
    configuration = di[Configuration]

    @asynccontextmanager
    @inject
    async def lifespan(_app: FastAPI, procrastinate_app: procrastinate.App):
        try:
            register_telemetry()
            async with procrastinate_app.open_async(), run_workers(app=procrastinate_app):
                try:
                    yield
                finally:
                    shutdown_telemetry()
        except Exception as e:
            logger.error("Error during startup: %s", repr(extract_messages(e)))
            raise

    app = FastAPI(
        lifespan=lifespan,
        default_response_class=ORJSONResponse,  # better performance then default + handle NaN floats
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        servers=[{"url": f"http://localhost:{configuration.port}"}],
    )

    logger.info("Mounting routes...")
    mount_routes(app)
    register_global_exception_handlers(app)
    return app
