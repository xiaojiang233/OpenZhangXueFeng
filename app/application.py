"""Construct the app without network calls or required provider credentials."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import Settings, settings as default_settings
from storage import ConversationStore

from .routes import router
from .services import ProviderServices
from .session import SessionCoordinator
from .api_settings import ApiSettingsStore, router as settings_router


def create_app(settings: Settings | None = None, store: ConversationStore | None = None,
               services: ProviderServices | None = None) -> FastAPI:
    settings = settings or default_settings
    api_settings_store = ApiSettingsStore(settings.data_dir / "api_settings.json")
    settings = api_settings_store.load(settings)

    @asynccontextmanager
    async def lifespan(application):
        logging.getLogger("openzxf").info("Services configured: %s", application.state.services.readiness)
        yield
        for session in tuple(application.state.sessions):
            session.closed = True
            await session.cancel(notify=False)

    application = FastAPI(title="OpenZhangXueFeng", version="2.0.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.api_settings_store = api_settings_store
    application.state.store = store or ConversationStore(settings.data_dir)
    application.state.services = services.reconfigure(settings) if services else ProviderServices(settings)
    application.state.coordinator = SessionCoordinator()
    application.state.sessions = application.state.coordinator.sessions
    application.include_router(router)
    application.include_router(settings_router)
    static_dir = Path(__file__).resolve().parent.parent / "static"
    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    @application.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return application
