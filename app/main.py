import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import pages, runs, webhooks
from app.config import get_settings
from app.container import Container, build_container


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx пишет URL каждого запроса, а URL Telegram содержит токен бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app(container: Container | None = None, *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container
        if c is None:
            settings = get_settings()
            configure_logging(settings.log_level)
            c = await build_container(settings)
        app.state.container = c
        await c.start(recover=start_background, pollers=start_background)
        try:
            yield
        finally:
            await c.stop()

    app = FastAPI(
        title="AI Bug Investigator",
        version="0.1.0",
        description="Агент разбирает ошибку, находит причину в GitHub-репозитории, чинит код, "
        "прогоняет тесты и открывает Pull Request после одобрения человеком.",
        lifespan=lifespan,
    )
    app.include_router(runs.router)
    app.include_router(webhooks.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/status", tags=["system"])
    async def service_status() -> dict:
        """Какие интеграции настроены (без значений ключей) — для предупреждений в интерфейсе."""
        c = app.state.container
        s = c.settings
        return {
            "groq": bool(s.groq_api_key),
            "github": bool(s.github_token),
            "telegram": s.telegram_enabled,
            "telegram_bot": c.telegram_bot_username,
            "sandbox": c.sandbox is not None,
            "local_projects": c.local_repos.list_projects() if c.local_repos else None,
            "langsmith": s.langsmith_tracing and bool(s.langsmith_api_key),
            "watched_repos": s.watched_repo_list,
            "models": {
                "main": s.groq_model_main,
                "fast": s.groq_model_fast,
                "fallback": s.groq_model_fallback,
            },
        }

    return app


app = create_app()
