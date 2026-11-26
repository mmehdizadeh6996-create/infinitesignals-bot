"""Public results channel: proof that the signals are real.

Every signal is posted to the channel AFTER it closes (target 2, break-even, stop loss or expired),
wins and losses alike, plus a daily summary and a pinned track-record message. Live signals stay
paid-only, so the channel shows the record without giving the product away.

Setup (once): add the bot as an admin of the channel with permission to post and pin, then post
/setchannel in the channel. The bot links itself, deletes that post and messages the admins.
Admins in the bot chat: /channel (status) · /channel off · /channel <chat_id>.
"""
import asyncio
import datetime as dt
import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import analysis
import db
import wake
from config import ADMIN_IDS

log = logging.getLogger("channel")
router = Router()

BACKFILL = 10  # on linking, only the latest closed signals are posted one by one; the summary covers the rest
SUMMARY_HOUR = 20  # UTC, once a day
CHECK_EVERY = 2  # scheduler slots (30 min)
BOT_LINK = "https://t.me/Infinitesignalsbot?start=ch_results"
CLOSED = ("tp2", "tp1_be", "sl", "expired")
DISCLAIMER = "<i>Past results do not guarantee future results. Trading carries risk. Not financial advice.</i>"

MIGRATE = ["CREATE TABLE IF NOT EXISTS channel_posted(signal_id INT PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now())"]

LABEL = {
    "tp2": ("🏆", "Target 2 hit", "+3R"),
    "tp1_be": ("🟡", "Break-even after Target 1", "0R"),
    "sl": ("❌", "Stop loss hit", "−1R"),
    "expired": ("⌛️", "Expired", "no result"),
}


def cta_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Start your 3-day free trial", url=BOT_LINK)]])


def _utc(t) -> str:
    return f"{t:%b} {t.day}, {t:%H:%M} UTC" if t else "—"


def result_text(g) -> str:
    icon, label, r = LABEL[g["status"]]
    side = "LONG" if g["side"] == "LONG" else "SHORT"
    return (
        f"{icon} <b>#{g['id']} {html.escape(g['symbol'])}/USDT {side}</b> — {label} ({r})\n\n"
        f"Entry <code>{analysis.fmt(g['entry'])}</code> · Stop <code>{analysis.fmt(g['sl'])}</code>\n"
        f"Target 1 <code>{analysis.fmt(g['tp1'])}</code> · Target 2 <code>{analysis.fmt(g['tp2'])}</code>\n"
        f"Opened {_utc(g['created_at'])}\nClosed {_utc(g['closed_at'])}\n\n"
        "Measured from the market entry price shown in the signal.")


Q_STATS = ("SELECT count(*) FILTER (WHERE status='tp2') tp2, count(*) FILTER (WHERE status='tp1_be') be, "
           "count(*) FILTER (WHERE status='sl') sl, count(*) FILTER (WHERE status='expired') expired, "
           "count(*) FILTER (WHERE status IN ('open','tp1')) open, "
           "count(*) FILTER (WHERE status IN ('tp2','tp1_be','sl','expired') "
           "AND closed_at > now() - interval '24 hours') today FROM signals")


def summary_text(s, title="📊 <b>Track record</b>") -> str:
    closed = s["tp2"] + s["be"] + s["sl"]
    net = 3 * s["tp2"] - s["sl"]
    safe = f"{(s['tp2'] + s['be']) / closed * 100:.0f}%" if closed else "—"
    return (
        f"{title}\nAll signals, measured from the market entry price.\n\n"
        f"🏆 Target 2 hit (+3R): {s['tp2']}\n"
        f"🟡 Break-even after Target 1 (0R): {s['be']}\n"
        f"❌ Stop loss (−1R): {s['sl']}\n"
        f"⌛️ Expired: {s['expired']}\n"
        f"⏳ Open: {s['open']}\n\n"
        f"🛡 Not-a-loss rate: {safe}\n"
        f"📐 Net result: {net:+d}R\n\n"
        "Every signal is posted here after it closes, wins and losses alike.\n\n" + DISCLAIMER)


async def channel_id():
    v = await db.pool.fetchval("SELECT value FROM settings WHERE key = 'results_channel'")
    return int(v) if v else None


async def _post(bot, chat, text, kb=None):
    """Sends one message. Returns the message, or None (rate limit: retry later; other error: logged)."""
    try:
        return await bot.send_message(chat, text, reply_markup=kb)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
    except Exception as e:
        log.warning("channel post failed: %s", e)
    return None


async def link(bot, chat: int) -> bool:
    """Links the channel, posts and pins the track record, and marks old closed signals as already covered."""
    await db.pool.execute(
        "INSERT INTO settings(key, value) VALUES('results_channel', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(chat))
    await db.pool.execute(
        "INSERT INTO channel_posted(signal_id) SELECT id FROM signals WHERE status IN ('tp2','tp1_be','sl','expired') "
        "AND id NOT IN (SELECT id FROM signals WHERE status IN ('tp2','tp1_be','sl','expired') "
        "ORDER BY closed_at DESC NULLS LAST, id DESC LIMIT $1) ON CONFLICT DO NOTHING", BACKFILL)
    msg = await _post(bot, chat, summary_text(await db.pool.fetchrow(Q_STATS)), cta_kb())
    if not msg:
        return False
    try:
        await bot.pin_chat_message(chat, msg.message_id, disable_notification=True)
    except Exception as e:
        log.info("pin failed: %s", e)
    return True


async def run_once(bot) -> int:
    chat = await channel_id()
    if not chat:
        return 0
    sent = 0
    rows = await db.pool.fetch(
        "SELECT * FROM signals WHERE status IN ('tp2','tp1_be','sl','expired') "
        "AND NOT EXISTS (SELECT 1 FROM channel_posted c WHERE c.signal_id = signals.id) "
        "ORDER BY closed_at NULLS FIRST, id LIMIT 20")
    for g in rows:
        if not await _post(bot, chat, result_text(g), cta_kb()):
            return sent  # stop; try again next round (rate limit, or the bot lost access)
        await db.pool.execute("INSERT INTO channel_posted(signal_id) VALUES($1) ON CONFLICT DO NOTHING", g["id"])
        sent += 1
        await asyncio.sleep(1.5)
    now = dt.datetime.now(dt.timezone.utc)
    if now.hour >= SUMMARY_HOUR:
        today = f"{now:%Y-%m-%d}"
        last = await db.pool.fetchval("SELECT value FROM settings WHERE key = 'results_summary_day'")
        if last != today:
            s = await db.pool.fetchrow(Q_STATS)
            if s["today"]:
                if await _post(bot, chat, summary_text(s, "📊 <b>Daily results</b>"), cta_kb()):
                    sent += 1
            await db.pool.execute(
                "INSERT INTO settings(key, value) VALUES('results_summary_day', $1) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", today)
    return sent


async def loop(bot):
    await wake.tick()
    while True:
        try:
            n = await run_once(bot)
            if n:
                log.info("channel: %s posts", n)
        except Exception:
            log.exception("channel")
        await wake.every(CHECK_EVERY)


async def on_startup(bot):
    for sql in MIGRATE:
        await db.pool.execute(sql)
    asyncio.create_task(loop(bot))


@router.channel_post(F.text == "/setchannel")
async def setchannel_post(m: Message):
    """Posted by a channel admin inside the channel; only admins can post there."""
    ok = await link(m.bot, m.chat.id)
    try:
        await m.delete()
    except Exception:
        pass
    for a in ADMIN_IDS:
        try:
            await m.bot.send_message(
                a, (f"✅ Results channel linked: <b>{html.escape(m.chat.title or str(m.chat.id))}</b>\n"
                    "The track record is posted and pinned. Each signal now appears there after it closes.")
                if ok else
                "⚠️ I saw /setchannel but could not post in that channel. Give the bot the permission to "
                "post messages, then send /setchannel again.")
        except Exception:
            pass


@router.message(Command("channel"))
async def channel_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return
    arg = (command.args or "").strip()
    if arg.lower() == "off":
        await db.pool.execute("DELETE FROM settings WHERE key = 'results_channel'")
        return await m.answer("⛔️ Results channel unlinked. Nothing will be posted.")
    if arg.lstrip("-").isdigit():
        if not await link(m.bot, int(arg)):
            return await m.answer("Could not post in that chat. Is the bot an admin there?")
    chat = await channel_id()
    if not chat:
        return await m.answer("No results channel linked.\nAdd the bot as admin of the channel, then post "
                              "<code>/setchannel</code> inside the channel (or send <code>/channel &lt;chat_id&gt;</code> here).")
    waiting = await db.pool.fetchval(
        "SELECT count(*) FROM signals WHERE status IN ('tp2','tp1_be','sl','expired') "
        "AND NOT EXISTS (SELECT 1 FROM channel_posted c WHERE c.signal_id = signals.id)")
    posted = await db.pool.fetchval("SELECT count(*) FROM channel_posted")
    await m.answer(f"📣 Results channel linked (<code>{chat}</code>)\nResults posted or covered: {posted}\n"
                   f"Waiting to post: {waiting}\nDaily summary at {SUMMARY_HOUR}:00 UTC.\n"
                   "<code>/channel off</code> unlinks it.")
