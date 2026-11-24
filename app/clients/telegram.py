"""Telegram Bot API: отправка сообщений и long polling (не нужен публичный адрес)."""

from typing import Any, Protocol

import httpx


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Токен входит в URL. Логгер httpx приглушён в app.main, чтобы токен не попал в логи.
        self._http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/", timeout=40.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(
        self, method: str, params: dict[str, Any] | None = None, http_timeout: float = 40.0
    ) -> Any:
        # params — отдельный dict: у getUpdates свой параметр timeout, это не HTTP-таймаут.
        try:
            resp = await self._http.post(method, json=params or {}, timeout=http_timeout)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{method}: {exc.__class__.__name__}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'unknown error')}")
        return data["result"]

    async def send_message(
        self, chat_id: str, html: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html[:4000],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            params["reply_markup"] = {
                "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]
            }
        return await self.call("sendMessage", params)

    async def edit_text(self, chat_id: str, message_id: int, html: str) -> None:
        await self.call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": html[:4000], "parse_mode": "HTML"},
        )

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", params, http_timeout=timeout + 15)


class Notifier(Protocol):
    async def request_approval(self, run_id: str, chat_id: str, html: str) -> None: ...

    async def send(self, chat_id: str, html: str) -> None: ...


class TelegramNotifier:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def request_approval(self, run_id: str, chat_id: str, html: str) -> None:
        await self.client.send_message(
            chat_id,
            html,
            buttons=[[("Одобрить", f"approve:{run_id}"), ("Отклонить", f"reject:{run_id}")]],
        )

    async def send(self, chat_id: str, html: str) -> None:
        await self.client.send_message(chat_id, html)
