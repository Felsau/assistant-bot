"""FastAPI app exposing the Telegram webhook and the daily-digest cron endpoint.

Flow:
  Telegram → POST /webhook → handlers → reply via Telegram API
  Cron service → POST /cron/daily-digest → morning summary to each user
"""

from __future__ import annotations

import calendar
import os
from collections import deque
from datetime import timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

load_dotenv()

# Imported after load_dotenv so module-level clients see the env vars.
from ai import classifier  # noqa: E402
from bot import clock, handlers, report, telegram_client, voice  # noqa: E402
from db import supabase_client  # noqa: E402

app = FastAPI(title="Personal Assistant Bot")

_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
_CRON_SECRET = os.environ.get("CRON_SECRET")

# Restrict who can use the bot (comma-separated Telegram user IDs). Empty = open.
_ALLOWED_USER_IDS = {
    uid.strip() for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# The command menu shown in Telegram when the user types "/".
_BOT_COMMANDS = [
    {"command": "start", "description": "Start and see what I can do"},
    {"command": "help", "description": "How to use me"},
    {"command": "today", "description": "What's on today"},
    {"command": "tasks", "description": "Show my open tasks"},
    {"command": "done", "description": "Mark a task complete"},
    {"command": "week", "description": "This week's spending summary"},
    {"command": "spent", "description": "This month's spending summary"},
    {"command": "budget", "description": "Set or view monthly budgets"},
    {"command": "report", "description": "Spending report with a chart"},
    {"command": "find", "description": "Search your notes, tasks, expenses"},
    {"command": "recurring", "description": "Manage recurring expenses"},
    {"command": "export", "description": "Download your transactions as CSV"},
]


# Telegram re-delivers an update if the webhook is slow to answer. Remember the
# recent update_ids we've handled so a redelivery can't double-log money. This
# is per-process (fine for a single free-tier instance); scale out via a shared
# store if you ever run multiple workers.
_SEEN_UPDATES: deque[int] = deque(maxlen=1000)
_SEEN_SET: set[int] = set()


def _already_processed(update_id) -> bool:
    """Record ``update_id`` and report whether it was already seen."""
    if update_id is None:
        return False
    if update_id in _SEEN_SET:
        return True
    if len(_SEEN_UPDATES) == _SEEN_UPDATES.maxlen:
        _SEEN_SET.discard(_SEEN_UPDATES[0])
    _SEEN_UPDATES.append(update_id)
    _SEEN_SET.add(update_id)
    return False


def _is_allowed(user_id: str) -> bool:
    return not _ALLOWED_USER_IDS or str(user_id) in _ALLOWED_USER_IDS


def _public_base_url() -> str | None:
    """Best-effort public base URL, from an explicit var or the host's."""
    if os.environ.get("WEBHOOK_URL"):
        return os.environ["WEBHOOK_URL"]
    if os.environ.get("RENDER_EXTERNAL_URL"):  # provided by Render
        return os.environ["RENDER_EXTERNAL_URL"]
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):  # provided by Railway
        return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
    return None


@app.on_event("startup")
async def register_webhook() -> None:
    """Auto-register the command menu and Telegram webhook on boot."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return

    try:
        await telegram_client.set_my_commands(_BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001 — don't crash boot over this
        print(f"[startup] setMyCommands failed: {exc}")

    base = _public_base_url()
    if not base:
        return
    webhook_url = base.rstrip("/") + "/webhook"
    try:
        await telegram_client.set_webhook(webhook_url, _WEBHOOK_SECRET)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] webhook registration failed: {exc}")
    else:
        print(f"[startup] webhook set to {webhook_url}")


@app.get("/")
async def health() -> dict:
    return {"status": "ok", "service": "assistant-bot"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    # Verify the secret Telegram echoes back (set via setWebhook secret_token).
    if _WEBHOOK_SECRET and x_telegram_bot_api_secret_token != _WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid secret token")

    update = await request.json()

    # Ignore redeliveries of an update we've already handled.
    if _already_processed(update.get("update_id")):
        return {"ok": True}

    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
        return {"ok": True}

    # Edited messages aren't re-processed: re-running classification on an edit
    # would insert a second expense/task/etc. rather than updating the first.
    # (Use "change coffee to 80" to edit an already-saved entry instead.)
    message = update.get("message")
    if not message:
        return {"ok": True}  # ignore non-message updates

    chat_id = message["chat"]["id"]
    user_id = str(message["from"]["id"])

    if not _is_allowed(user_id):
        await telegram_client.send_message(chat_id, "This bot is private.")
        return {"ok": True}

    try:
        await run_in_threadpool(supabase_client.upsert_user, user_id, chat_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[webhook] upsert_user failed: {exc}")

    text = message.get("text")

    # Photos → read as a receipt and log the expense(s).
    if not text and message.get("photo"):
        await _handle_receipt(
            chat_id, user_id, message["photo"][-1]["file_id"], message.get("caption")
        )
        return {"ok": True}

    # /report sends a chart image, so it's handled here, not via handle_message.
    if text and text.startswith("/report"):
        await _handle_report(chat_id, user_id)
        return {"ok": True}

    # Voice notes → transcribe, then treat the transcript as text.
    if not text and message.get("voice"):
        try:
            text = await voice.transcribe(message["voice"]["file_id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[webhook] transcription failed: {exc}")
            text = None
        if not text:
            note = (
                "Couldn't read that voice message. Type it instead."
                if voice.enabled()
                else "Voice isn't set up. Type it instead."
            )
            await telegram_client.send_message(chat_id, note)
            return {"ok": True}
        await telegram_client.send_message(chat_id, f'"{text}"')

    # /export sends a file, so it's handled here rather than via handle_message.
    if text and text.startswith("/export"):
        await _handle_export(chat_id, user_id)
        return {"ok": True}

    try:
        replies = await run_in_threadpool(handlers.handle_message, user_id, text)
    except Exception as exc:  # noqa: BLE001 — never 500 back to Telegram
        print(f"[webhook] handle_message failed: {exc}")
        replies = [{"text": "Sorry, something went wrong. Try again.", "reply_markup": None}]

    for r in replies:
        await telegram_client.send_message(chat_id, r["text"], r.get("reply_markup"))
    return {"ok": True}


async def _handle_receipt(
    chat_id: int, user_id: str, file_id: str, caption: str | None = None
) -> None:
    """Download a photo, read it as a receipt, and log the expense."""
    try:
        image = await telegram_client.download_file(file_id)
        data = await run_in_threadpool(classifier.extract_receipt, image, caption=caption)
    except Exception as exc:  # noqa: BLE001
        print(f"[receipt] failed: {exc}")
        await telegram_client.send_message(chat_id, "Couldn't read that image. Type the amount instead.")
        return

    if not data or data.get("amount") in (None, ""):
        await telegram_client.send_message(
            chat_id, "Couldn't find a total on that receipt. Type the amount instead."
        )
        return

    try:
        replies = await run_in_threadpool(handlers.record_receipt, user_id, data)
        for r in replies:
            await telegram_client.send_message(chat_id, r["text"], r.get("reply_markup"))
    except Exception as exc:  # noqa: BLE001 — never leave the user without a reply
        print(f"[receipt] recording failed: {exc}")
        await telegram_client.send_message(chat_id, "Sorry, something went wrong. Try again.")


async def _handle_report(chat_id: int, user_id: str) -> None:
    """Send a spending report plus a category bar chart."""
    try:
        text, by_category = await run_in_threadpool(handlers.report_text, user_id)
    except Exception as exc:  # noqa: BLE001 — never leave the user without a reply
        print(f"[report] failed: {exc}")
        await telegram_client.send_message(chat_id, "Sorry, something went wrong. Try again.")
        return
    try:
        image = await run_in_threadpool(
            report.render_category_chart, by_category, text.splitlines()[0]
        )
    except Exception as exc:  # noqa: BLE001 — chart is a nice-to-have
        print(f"[report] chart failed: {exc}")
        image = None
    try:
        if image:
            await telegram_client.send_photo(chat_id, image, caption=text)
        else:
            await telegram_client.send_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        print(f"[report] send failed: {exc}")


async def _handle_export(chat_id: int, user_id: str) -> None:
    """Export all of the user's transactions as a CSV file."""
    try:
        rows = await run_in_threadpool(supabase_client.list_transactions, user_id, limit=10000)
        if not rows:
            await telegram_client.send_message(chat_id, "Nothing to export yet.")
            return
        csv_bytes = await run_in_threadpool(handlers.build_transactions_csv, rows)
        await telegram_client.send_document(
            chat_id, "transactions.csv", csv_bytes, caption="Your transactions"
        )
    except Exception as exc:  # noqa: BLE001 — never leave the user without a reply
        print(f"[export] failed: {exc}")
        await telegram_client.send_message(chat_id, "Sorry, something went wrong. Try again.")


async def _handle_callback(cb: dict) -> None:
    user_id = str(cb["from"]["id"])
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    if not _is_allowed(user_id):
        await telegram_client.answer_callback_query(cb_id, "Not allowed")
        return

    try:
        result = await run_in_threadpool(handlers.handle_callback, user_id, data)
    except Exception as exc:  # noqa: BLE001
        print(f"[callback] failed: {exc}")
        await telegram_client.answer_callback_query(cb_id, "Something went wrong")
        return

    await telegram_client.answer_callback_query(cb_id, result.get("answer"))
    if result.get("edit_text") and chat_id and message_id:
        try:
            await telegram_client.edit_message_text(
                chat_id, message_id, result["edit_text"], result.get("reply_markup")
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[callback] edit failed: {exc}")


@app.post("/cron/daily-digest")
async def daily_digest(
    secret: str | None = None,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """Send each known user a friendly summary of their day.

    Protect with CRON_SECRET and call from a scheduler (e.g. cron-job.org).
    """
    provided = x_cron_secret or secret
    if not _CRON_SECRET or provided != _CRON_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    users = await run_in_threadpool(supabase_client.list_users)
    sent = 0
    for u in users:
        try:
            rows = await run_in_threadpool(supabase_client.query, u["user_id"], "today")
            text = await run_in_threadpool(
                classifier.format_query_reply,
                "Summarize what's on for today from this data. If nothing, say the day is clear.",
                rows,
            )
            warn = await run_in_threadpool(handlers.budget_warnings, u["user_id"])
            if warn:
                text += "\n\n" + warn
            await telegram_client.send_message(u["chat_id"], text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[digest] failed for {u.get('user_id')}: {exc}")

    return {"ok": True, "sent": sent}


@app.post("/cron/reminders")
async def fire_reminders(
    secret: str | None = None,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """Deliver any reminders that are now due. Call every minute or few."""
    if not _CRON_SECRET or (x_cron_secret or secret) != _CRON_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    users = await run_in_threadpool(supabase_client.list_users)
    chat_of = {u["user_id"]: u["chat_id"] for u in users}
    due = await run_in_threadpool(supabase_client.due_reminders, clock.now().isoformat())
    sent = 0
    for r in due:
        chat_id = chat_of.get(r["user_id"])
        if not chat_id:
            # No known chat to deliver to (e.g. user row missing) — leave the
            # reminder due rather than marking it sent/rescheduled for nothing.
            continue
        try:
            await telegram_client.send_message(chat_id, "Reminder: " + r.get("text", ""))
            sent += 1
            # Repeating reminders roll forward to their next occurrence; one-offs
            # are marked sent so they don't fire again.
            next_at = clock.next_occurrence(r.get("remind_at"), r.get("repeat"), r.get("anchor_day"))
            if next_at:
                await run_in_threadpool(supabase_client.reschedule_reminder, r["id"], next_at)
            else:
                await run_in_threadpool(supabase_client.mark_reminder_sent, r["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[reminders] failed for {r.get('id')}: {exc}")

    # Appointment heads-up: ping EVENT_LEAD_MINUTES before each event starts.
    lead = timedelta(minutes=handlers.EVENT_LEAD_MIN)
    events = await run_in_threadpool(
        supabase_client.unnotified_events, (clock.now() + lead).isoformat()
    )
    announced = 0
    for ev in events:
        chat_id = chat_of.get(ev["user_id"])
        if not chat_id:
            continue
        try:
            start = clock.to_local(ev.get("starts_at"))
            # An event more than a day past (e.g. the bot was down) is retired
            # quietly rather than announced as "coming up".
            if start and start < clock.now() - timedelta(hours=24):
                await run_in_threadpool(supabase_client.mark_event_notified, ev["id"])
                continue
            when = ""
            if start:
                day = "" if start.date() == clock.today() else f"{start:%b %d} "
                when = f" at {day}{start:%H:%M}"
            text = f"Coming up{when}: {ev.get('title', '')}"
            if ev.get("location"):
                text += f" ({ev['location']})"
            await telegram_client.send_message(chat_id, text)
            await run_in_threadpool(supabase_client.mark_event_notified, ev["id"])
            announced += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[events] failed for {ev.get('id')}: {exc}")

    return {"ok": True, "sent": sent, "events": announced}


@app.post("/cron/recurring")
async def post_recurring(
    secret: str | None = None,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """Post due recurring expenses for today. Call once a day."""
    if not _CRON_SECRET or (x_cron_secret or secret) != _CRON_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    today = clock.today()
    posted = 0
    recurring = await run_in_threadpool(supabase_client.all_recurring)
    for r in recurring:
        day = int(r.get("day_of_month", 1))
        if day > today.day:
            continue  # target day hasn't arrived yet this month
        last = str(r.get("last_posted") or "")[:7]
        if last == today.strftime("%Y-%m"):
            continue  # already posted this month
        # Catch up if a cron run was missed on the exact day, but still record
        # the transaction against the day it was meant to occur on.
        occurred_on = today.replace(day=min(day, calendar.monthrange(today.year, today.month)[1]))
        try:
            await run_in_threadpool(supabase_client.insert_transaction, r["user_id"], {
                "kind": r.get("kind", "expense"),
                "amount": r.get("amount"),
                "currency": r.get("currency"),
                "category": r.get("category"),
                "note": r.get("note"),
                "occurred_on": occurred_on.isoformat(),
            })
            await run_in_threadpool(supabase_client.mark_recurring_posted, r["id"], today.isoformat())
            posted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[recurring] failed for {r.get('id')}: {exc}")
    return {"ok": True, "posted": posted}
