"""Thin Telegram glue. All decisions live in bridge.app."""
from __future__ import annotations

import asyncio
import logging
import uuid

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from ..app import Bridge, Channel
from ..live.plan import Plan

log = logging.getLogger("bridge.telegram")
PENDING_KEY = "pending_plans"
ASK_LINE = "\n\nThis only reads data; nothing is changed. Approve? Tap a button, or type yes or no."

COMMANDS = [("ask", "Ask a question about Fusion data"), ("new", "Start a fresh conversation"),
            ("usage", "Your questions today and the monthly budget"), ("help", "What I can do")]


def command_menu() -> list[BotCommand]:
    """The menu Telegram shows when a user types a slash."""
    return [BotCommand(name, desc) for name, desc in COMMANDS]


def _pending(app: Application) -> dict:
    return app.bot_data.setdefault(PENDING_KEY, {})


class TelegramApprover:
    """Posts the plan with Approve/Deny buttons and waits. Only the requesting user can answer."""

    def __init__(self, app: Application, chat_id: int, user_id: int):
        self.app, self.chat_id, self.user_id = app, chat_id, user_id

    async def __call__(self, plan: Plan) -> bool:
        pid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        _pending(self.app)[pid] = (self.user_id, fut, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"plan:{pid}:y"),
                                    InlineKeyboardButton("Deny", callback_data=f"plan:{pid}:n")]])
        msg = await self.app.bot.send_message(self.chat_id, plan.render() + ASK_LINE, reply_markup=kb)
        _pending(self.app)[pid] = (self.user_id, fut, msg)
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
    owner, fut = entry[0], entry[1]
    if q.from_user.id != owner:                                # nobody else can approve your query
        await q.answer("Only the requester can answer this.", show_alert=True)
        return
    if not fut.done():
        fut.set_result(choice == "y")
    await q.answer()
    await q.edit_message_text(q.message.text.replace(ASK_LINE, "") +
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


YES = {"y", "yes", "ok", "okay", "approve", "approved", "go", "نعم", "موافق"}
NO = {"n", "no", "deny", "cancel", "stop", "لا", "الغاء", "إلغاء"}


async def answer_waiting_plan(app: Application, user_id: int, text: str) -> bool:
    """If this user has a plan waiting and typed a clear yes or no, treat it as the button. Returns True if handled."""
    word = text.strip().lower().rstrip(".!")
    if word not in YES and word not in NO:
        return False
    for pid, entry in list(_pending(app).items()):
        owner, fut, msg = entry
        if owner == user_id and not fut.done():
            fut.set_result(word in YES)
            if msg is not None:
                try:
                    await msg.edit_text(msg.text.replace(ASK_LINE, "") + ("\n\nApproved." if word in YES else "\n\nDenied."))
                except Exception:
                    pass
            return True
    return False


def build_application(token: str, bridge: Bridge, request=None) -> Application:
    """`request` replaces the HTTP layer; tests pass a fake so the real handlers run with no network."""
    builder = Application.builder().token(token).concurrent_updates(True)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()

    async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
        """Anything a handler did not catch: log it with its traceback (secrets blanked) and tell the user once."""
        log.error("Unhandled error in a Telegram handler", exc_info=ctx.error)
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            try:
                await app.bot.send_message(chat.id, "Something went wrong. It has been logged.")
            except Exception:
                log.error("Could not send the error notice either")

    app.add_error_handler(on_error)

    def cmd(name: str):
        async def h(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not update.effective_user or not update.effective_chat:
                return
            text = " ".join(ctx.args or [])
            log.info("message user=%s command=%s chars=%d", update.effective_user.id, name, len(text))
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
            if await answer_waiting_plan(app, update.effective_user.id, update.message.text):
                log.info("message user=%s command=plan-answer", update.effective_user.id)
                return                                            # "yes" or "no" typed instead of tapping a button
            log.info("message user=%s command=ask chars=%d", update.effective_user.id, len(update.message.text))
            try:
                await bridge.handle("ask", update.message.text, make_channel(app, update))
            except Exception:
                log.exception("handler failed")
                await app.bot.send_message(update.effective_chat.id, "Something went wrong. It has been logged.")

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^plan:"))
    return app
