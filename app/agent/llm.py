"""Модели Groq и вызов с запасной моделью.

Открытые модели иногда возвращают битый вызов инструмента (Groq отвечает 400 tool_use_failed)
или упираются в лимит бесплатного тарифа. В обоих случаях пробуем запасную модель.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class LLMs:
    main: Any  # расследование и исправление (function calling)
    fast: Any  # критик и описание PR
    fallback: Any | None = None


class MissingKeyLLM:
    """Заглушка без ключа: сервис стартует, а запуск завершается понятной ошибкой."""

    def bind_tools(self, tools: list) -> "MissingKeyLLM":
        return self

    async def ainvoke(self, messages: list) -> AIMessage:
        raise RuntimeError("GROQ_API_KEY не задан: добавьте ключ в .env и перезапустите сервис")


def build_llms(settings: Settings) -> LLMs:
    if not settings.groq_api_key:
        log.warning("GROQ_API_KEY is not set: runs will fail until it is configured")
        return LLMs(main=MissingKeyLLM(), fast=MissingKeyLLM(), fallback=None)

    from langchain_groq import ChatGroq

    def make(model: str) -> ChatGroq:
        return ChatGroq(
            model=model,
            temperature=0,
            api_key=settings.groq_api_key,
            max_retries=2,
            timeout=settings.llm_timeout,
        )

    return LLMs(
        main=make(settings.groq_model_main),
        fast=make(settings.groq_model_fast),
        fallback=make(settings.groq_model_fallback) if settings.groq_model_fallback else None,
    )


def usage_tokens(message: BaseMessage) -> int:
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("total_tokens", 0) or 0)


async def ainvoke_with_fallback(
    primary: Any,
    fallback: Any | None,
    messages: Sequence[BaseMessage],
    *,
    tools: list | None = None,
    on_fallback: Callable[[Exception], Awaitable[None]] | None = None,
) -> AIMessage:
    try:
        model = primary.bind_tools(tools) if tools else primary
        return await model.ainvoke(list(messages))
    except Exception as exc:
        if fallback is None:
            raise
        log.warning("primary model failed (%s), switching to fallback", exc.__class__.__name__)
        if on_fallback is not None:
            await on_fallback(exc)
        model = fallback.bind_tools(tools) if tools else fallback
        return await model.ainvoke(list(messages))
