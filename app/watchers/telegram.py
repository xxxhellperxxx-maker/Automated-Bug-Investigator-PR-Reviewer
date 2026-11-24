"""Long polling Telegram (без вебхука): кнопки «Одобрить/Отклонить» и регистрация чата по /start.

Chat ID пользователь не ищет сам: он пишет боту /start, бот запоминает чат и username,
а на сайте достаточно указать @username.
"""

import asyncio
import logging
from html import escape
from typing import Any

from app.clients.telegram import TelegramClient, TelegramError
from app.runner import RunConflictError, RunManager
from app.schemas import RunStatus
from app.store import Store

log = logging.getLogger(__name__)


class TelegramPoller:
    def __init__(self, client: TelegramClient, store: Store, runner: RunManager) -> None:
        self.client = client
        self.store = store
        self.runner = runner
        self.offset: int | None = None

    async def handle_update(self, update: dict[str, Any]) -> None:
        if update.get("message"):
            await self._register_chat(update["message"])
            return
        query = update.get("callback_query")
        if not query:
            return
        action, _, run_id = str(query.get("data", "")).partition(":")
        if action not in ("approve", "reject") or not run_id:
            return
        reply = await self._decide(query, action == "approve", run_id)
        try:
            await self.client.answer_callback(query["id"], reply)
        except TelegramError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    async def _register_chat(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        # Только личные чаты: запрос на одобрение — решение конкретного человека.
        if chat.get("type") != "private" or not chat.get("id"):
            return
        user = message.get("from") or {}
        username = user.get("username")
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None
        await self.store.save_telegram_chat(str(chat["id"]), username, name)
        how = f"@{escape(username)} или этот ID" if username else "этот ID"
        text = (
            "Готово, я запомнил этот чат.\n"
            f"Ваш chat ID: <code>{chat['id']}</code>\n\n"
            f"На сайте AI Bug Investigator в поле Telegram укажите {how} — "
            "запросы на одобрение исправлений придут сюда."
        )
        try:
            await self.client.send_message(str(chat["id"]), text)
        except TelegramError as exc:
            log.warning("telegram reply failed: %s", exc)

    async def _decide(self, query: dict[str, Any], approved: bool, run_id: str) -> str:
        row = await self.store.get_row(run_id)
        if row is None:
            return "Запуск не найден"
        chat_id = str((query.get("message") or {}).get("chat", {}).get("id", ""))
        # Решение принимается только из того чата, куда агент отправил запрос.
        if chat_id != str(row.get("chat_id")):
            return "Нет прав на это решение"
        if row["status"] != RunStatus.AWAITING_APPROVAL:
            return "Решение уже принято"
        user = query.get("from") or {}
        actor = f"telegram:{user.get('username') or user.get('id')}"
        try:
            await self.runner.resume(run_id, approved, actor)
        except RunConflictError:
            return "Решение уже принято"
        message = query.get("message") or {}
        if message.get("message_id"):
            verdict = "Одобрено, создаю Pull Request" if approved else "Отклонено"
            try:
                await self.client.edit_text(
                    chat_id,
                    message["message_id"],
                    f"{escape(message.get('text', ''))[:3500]}\n\n<b>{verdict}</b>",
                )
            except TelegramError:
                pass  # снять кнопки не обязательно для корректности
        return "Одобрено" if approved else "Отклонено"

    async def run_forever(self) -> None:
        while True:
            try:
                updates = await self.client.get_updates(self.offset)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram polling error: %s", exc)
                await asyncio.sleep(5)
