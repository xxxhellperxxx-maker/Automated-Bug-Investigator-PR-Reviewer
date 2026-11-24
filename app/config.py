import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки берутся из переменных окружения или файла .env (см. .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    public_base_url: str = "http://localhost:8000"
    data_dir: str = "data"
    log_level: str = "INFO"

    # LLM (Groq)
    groq_api_key: str = ""
    # Список моделей Groq меняется: проверяйте актуальный в console.groq.com -> Models.
    groq_model_main: str = "openai/gpt-oss-120b"
    groq_model_fallback: str = "qwen/qwen3.8-27b"
    groq_model_fast: str = "openai/gpt-oss-20b"
    llm_timeout: float = 60.0

    # GitHub
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str = ""
    watched_repos: str = ""  # через запятую: owner/name,owner/other
    issue_label: str = "bug"
    issue_poll_interval: int = 60
    issue_backfill: bool = False

    # Telegram (необязательно)
    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""

    # Агент
    max_fix_attempts: int = 3
    agent_max_iterations: int = 6
    branch_prefix: str = "ai-fix/"
    ci_poll_interval: float = 20.0
    ci_timeout: float = 600.0
    ci_no_runs_grace: float = 90.0

    # Локальная песочница для тестов (Docker Engine API через сокет)
    sandbox_enabled: bool = True
    docker_socket: str = "/var/run/docker.sock"
    sandbox_image: str = "python:3.12-slim"
    sandbox_test_timeout: float = 180.0

    # Локальный режим: git-проекты в папке на компьютере (в Docker подключена в /projects)
    local_projects_dir: str = "/projects"
    local_projects_host_dir: str = ""  # тот же путь на компьютере пользователя — для подсказок в отчёте

    # LangSmith: библиотека читает os.environ, поэтому значения из .env пробрасываются туда при старте.
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "ai-bug-investigator"

    @property
    def watched_repo_list(self) -> list[str]:
        return [r.strip() for r in self.watched_repos.split(",") if r.strip()]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def default_chat_id(self) -> str | None:
        value = self.telegram_default_chat_id.strip()
        return value or None

    @property
    def checkpoint_db(self) -> str:
        return str(Path(self.data_dir) / "checkpoints.db")

    @property
    def runs_db(self) -> str:
        return str(Path(self.data_dir) / "runs.db")

    def export_langsmith_env(self) -> None:
        if self.langsmith_tracing and self.langsmith_api_key:
            os.environ.setdefault("LANGSMITH_TRACING", "true")
            os.environ.setdefault("LANGSMITH_API_KEY", self.langsmith_api_key)
            os.environ.setdefault("LANGSMITH_PROJECT", self.langsmith_project)


@lru_cache
def get_settings() -> Settings:
    return Settings()
