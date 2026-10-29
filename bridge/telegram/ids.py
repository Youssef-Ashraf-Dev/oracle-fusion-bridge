"""Who has been messaging the bot? Used to find the numeric Telegram IDs to register."""
from __future__ import annotations

from typing import Iterable


def senders(updates: Iterable) -> list[dict]:
    """Unique senders of private messages, in the order first seen. Group messages and non-message updates are skipped."""
    seen: dict[int, dict] = {}
    for u in updates:
        msg = getattr(u, "message", None) or getattr(u, "edited_message", None)
        user = getattr(msg, "from_user", None) if msg else None
        chat = getattr(msg, "chat", None) if msg else None
        if user is None or getattr(user, "is_bot", False) or getattr(chat, "type", "private") != "private":
            continue
        seen.setdefault(user.id, {"id": user.id, "name": " ".join(x for x in (user.first_name, user.last_name) if x),
                                  "username": f"@{user.username}" if user.username else ""})
    return list(seen.values())
