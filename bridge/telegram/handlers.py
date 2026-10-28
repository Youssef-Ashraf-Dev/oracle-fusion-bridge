"""Thin Telegram glue. All decisions live in bridge.app."""
from __future__ import annotations

import asyncio
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from ..app import Bridge, Channel
from ..live.plan import Plan

log = logging.getLogger("bridge.telegram")
PENDING_KEY = "pending_plans"


def _pending(app: Application) -> dict:
    return app.bot_data.setdefault(PENDING_KEY, {})


class TelegramApprover:
    """Posts the plan with Approve/Deny buttons and waits. Only the requesting user can answer."""

    def __init__(self, app: Application, chat_id: int, user_id: int):
        self.app, self.chat_id, self.user_id = app, chat_id, user_id

    async def __call__(self, plan: Plan) -> bool:
        pid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        _pending(self.app)[pid] = (self.user_id, fut)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"plan:{pid}:y"),
                                    InlineKeyboardButton("Deny", callback_data=f"plan:{pid}:n")]])
        msg = await self.app.bot.send_message(self.chat_id, plan.render() + "\n\nApprove these data calls?", reply_markup=kb)
        try:
            return await fut
        except asyncio.CancelledError:                       # approval timed out upstream
            try:
                await msg.edit_text(plan.render() + "\n\nExpired.")
            except Exception:
                pass
            raise
        finally:
            _pending(self.app).pop(pid, None)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, pid, choice = (q.data or "").split(":")
    except ValueError:
        return await q.answer()
    entry = _pending(ctx.application).get(pid)
    if not entry:
        await q.answer("This request has expired.", show_alert=True)
        return
    owner, fut = entry
    if q.from_user.id != owner:                                # nobody else can approve your query
        await q.answer("Only the requester can answer this.", show_alert=True)
        return
    if not fut.done():
        fut.set_result(choice == "y")
    await q.answer()
    await q.edit_message_text(q.message.text.replace("\n\nApprove these data calls?", "") +
                              ("\n\nApproved." if choice == "y" else "\n\nDenied."))


def make_channel(app: Application, update: Update) -> Channel:
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    bot = app.bot
    status_msg: dict = {}

    async def send_text(t: str):
        await bot.send_message(chat_id, t[:4000], disable_web_page_preview=True)

    async def status(t: str):
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        if "m" in status_msg:
            try:
                await status_msg["m"].edit_text(t)
                return
            except Exception:
                pass
        status_msg["m"] = await bot.send_message(chat_id, t)

    return Channel(user_id, chat_id, send_text, TelegramApprover(app, chat_id, user_id), status)


def build_application(token: str, bridge: Bridge) -> Application:
    app = Application.builder().token(token).concurrent_updates(True).build()

    def cmd(name: str):
        async def h(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not update.effective_user or not update.effective_chat:
                return
            text = " ".join(ctx.args or [])
            try:
                await bridge.handle(name, text, make_channel(app, update))
            except Exception:
                log.exception("handler failed")
                await app.bot.send_message(update.effective_chat.id, "Something went wrong. It has been logged.")
        return h

    for name in ("start", "help", "new", "usage", "ask"):
        app.add_handler(CommandHandler(name, cmd(name)))

    async def plain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_chat and update.message and update.message.text:
            try:
                await bridge.handle("ask", update.message.text, make_channel(app, update))
            except Exception:
                log.exception("handler failed")
                await app.bot.send_message(update.effective_chat.id, "Something went wrong. It has been logged.")

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^plan:"))
    return app
