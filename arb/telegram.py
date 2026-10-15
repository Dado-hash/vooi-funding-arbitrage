"""Telegram: notifiche e approvazione delle proposte con pulsanti. Nessun comando testuale viene interpretato."""

import os
from datetime import datetime, timezone

import httpx

from .config import load_dotenv

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # limite Telegram: 4096 caratteri per messaggio


class TelegramError(RuntimeError):
    pass


def _call(method: str, **params):
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN mancante in .env (vedi README)")
    r = httpx.post(API.format(token=token, method=method), json=params, timeout=20 + int(params.get("timeout", 0)))
    data = r.json()
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data["result"]


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            parts.append(current + line[: size - len(current)])
            line, current = line[size - len(current):], ""
        if len(current) + len(line) > size:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts


def send(text: str) -> int:
    """Invia `text` (testo semplice) alla chat configurata. Restituisce il numero di messaggi."""
    load_dotenv()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise TelegramError("TELEGRAM_CHAT_ID mancante in .env: esegui `arb tg chat-id`")
    parts = chunks(text)
    for part in parts:
        _call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)
    return len(parts)


def chat_ids() -> list[dict]:
    """Chat che hanno scritto al bot di recente: serve a ricavare TELEGRAM_CHAT_ID."""
    seen = {}
    for u in _call("getUpdates"):
        chat = (u.get("message") or u.get("edited_message") or {}).get("chat")
        if chat:
            seen[chat["id"]] = {"chat_id": chat["id"], "name": chat.get("username") or chat.get("first_name")}
    return list(seen.values())


# --- approvazioni: solo pulsanti, mai comandi testuali ---

def _allowed_user() -> int:
    load_dotenv()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise TelegramError("TELEGRAM_CHAT_ID mancante in .env: esegui `arb tg chat-id`")
    return int(chat_id)


def send_proposal(p: dict) -> int:
    """Invia la proposta con i pulsanti Approva/Rifiuta. Restituisce il message_id."""
    expires = p["expires_ts"][11:16]
    text = f"PROPOSTA #{p['id']} (scade {expires} UTC)\n\n{p['summary']}"
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approva", "callback_data": f"a:{p['id']}"},
        {"text": "❌ Rifiuta", "callback_data": f"r:{p['id']}"},
    ]]}
    msg = _call("sendMessage", chat_id=_allowed_user(), text=text, reply_markup=keyboard,
                disable_web_page_preview=True)
    return msg["message_id"]


def _offset(conn) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key = 'tg_offset'").fetchone()
    return int(row["value"]) if row else 0


def _save_offset(conn, offset: int) -> None:
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('tg_offset', ?)", (str(offset),))
    conn.commit()


def poll(conn, timeout: int = 0) -> list[dict]:
    """Legge i tap sui pulsanti e aggiorna le proposte. Ignora tutto il resto (testo compreso).

    Restituisce le decisioni registrate: [{"id", "decision"}].
    """
    from . import approvals

    allowed = _allowed_user()
    updates = _call("getUpdates", offset=_offset(conn), timeout=timeout, allowed_updates=["callback_query"])
    decisions = []
    for u in updates:
        _save_offset(conn, u["update_id"] + 1)
        cq = u.get("callback_query")
        if not cq:
            continue
        if cq["from"]["id"] != allowed:
            _call("answerCallbackQuery", callback_query_id=cq["id"], text="Non autorizzato")
            continue
        action, _, pid = (cq.get("data") or "").partition(":")
        p = approvals.get(conn, pid)
        if not p or action not in ("a", "r"):
            _call("answerCallbackQuery", callback_query_id=cq["id"], text="Proposta sconosciuta")
            continue
        if p["status"] != "pending":
            note = f"Già {p['status']}"
        elif approvals.is_expired(p):
            approvals.set_status(conn, pid, "expired")
            note = "Scaduta: non verrà eseguita"
        else:
            status = "approved" if action == "a" else "rejected"
            approvals.set_status(conn, pid, status, decided_ts=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            decisions.append({"id": pid, "decision": status})
            note = "Approvata: eseguo a breve" if status == "approved" else "Rifiutata"
        _call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        msg = cq.get("message")
        if msg:
            _call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                  text=f"{msg.get('text', '')}\n\n→ {note}")
    return decisions


def wait_decision(conn, pid: str, timeout_s: int) -> str:
    """Long polling finché la proposta `pid` non è decisa o scade. Restituisce lo stato finale."""
    import time

    from . import approvals

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        poll(conn, timeout=min(50, max(1, int(deadline - time.monotonic()))))
        p = approvals.get(conn, pid)
        if p is None:
            return "unknown"
        if p["status"] != "pending":
            return p["status"]
        if approvals.is_expired(p):
            approvals.set_status(conn, pid, "expired")
            return "expired"
    return approvals.get(conn, pid)["status"]
