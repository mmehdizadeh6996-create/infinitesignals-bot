"""Black Friday campaign: one switch that changes the whole bot for a time window.

  /campaign CODE START END   e.g. /campaign BLACKFRIDAY now 2026-11-30T23:59   (UTC; date or date-time)
  /campaign off              stop it now
  /campaign                  status + messages sent + purchases

While it runs (START <= now < END):
  * everyone who opens the bot, the plan list or checkout gets the campaign code automatically;
  * every other discount is paused (the 35% trial offer, codes typed by hand), then comes back by itself;
  * users who have not paid get motivational messages on a calm schedule (each message once per user):
      launch      right when it starts
      daily       once a day at DAILY_HOUR UTC, with rotating copy, at least GAP_H hours after the last one
      last24      the final day
      last6       the final hours
    Active subscribers and anyone who already bought with the campaign code are never messaged.
"""
import asyncio
import datetime as dt
import html
import logging

from aiogram import Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import db
import growth
import wake
from config import ADMIN_IDS

log = logging.getLogger("blackfriday")
router = Router()

DAILY_HOUR = 16  # UTC: evening in Europe, morning in the Americas
GAP_H = 14  # hours between regular messages to one user
FINAL_GAP_H = 3  # the two final-day messages only need this much space
NEW_USER_H = 6  # a user who just joined already saw the offer in the welcome
CHECK_EVERY = 2  # scheduler slots (30 min)
BATCH = 200

MIGRATE = ["CREATE TABLE IF NOT EXISTS bf_sent(uid BIGINT NOT NULL, tag TEXT NOT NULL, "
           "at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (uid, tag))"]

DISCLAIMER = "<i>Trading carries risk. Not financial advice.</i>"


def _when(end: dt.datetime) -> str:
    return f"{end:%b} {end.day}, {end:%H:%M} UTC"


DAILY = [
    "⏰ <b>Black Friday is still on: {pct}% OFF</b>\n\nIt ends {when}. Pick a plan, send USDT (TRC20) and "
    "your access activates automatically. It takes about a minute.",
    "🎯 <b>Trade with a plan, not a feeling</b>\n\nClear entry zones, a stop-loss and two targets on every "
    "setup, tracked live until the result. Black Friday price: <b>{pct}% OFF</b> until {when}.",
    "🛡 <b>Risk first</b>\n\nEvery setup comes with a defined stop-loss, so you know your risk before you "
    "enter. Get it at <b>{pct}% OFF</b> until {when}.",
    "📲 <b>Never miss a setup</b>\n\nSetups and live updates arrive straight in Telegram, so you do not have "
    "to sit in front of the charts. <b>{pct}% OFF</b> until {when}.",
    "💸 <b>The sale price ends {when}</b>\n\nAfter that, plans go back to full price. Your {pct}% Black Friday "
    "discount is already applied below.",
]
LAUNCH = ("🖤 <b>BLACK FRIDAY is live: {pct}% OFF every plan</b>\n\nYour discount is applied automatically, "
          "so the prices below already include it. Valid until {when}.\n\n"
          "Clear entries, defined stop-losses and live tracking, all inside Telegram.")
LAST24 = ("⚡️ <b>24 hours left: {pct}% OFF ends {when}</b>\n\nIf you have been thinking about it, "
          "this is the moment. Choose a plan below and you are in within minutes.")
LAST6 = ("🚨 <b>Last call: Black Friday ends in about 6 hours</b>\n\n{pct}% OFF every plan, until {when}. "
         "After that, full price.")


def candidates(now: dt.datetime, start: dt.datetime, end: dt.datetime):
    """[(tag, gap_hours, template)] that may be sent right now, highest priority first."""
    left = end - now
    out = []
    if left <= dt.timedelta(hours=6):
        out.append(("last6", FINAL_GAP_H, LAST6))
    elif left <= dt.timedelta(hours=24):
        out.append(("last24", FINAL_GAP_H, LAST24))
    out.append(("launch", GAP_H, LAUNCH))  # a user's first campaign message is always the intro
    if left > dt.timedelta(hours=6) and now.hour >= DAILY_HOUR:
        day = (now - start).days  # rotates the copy, one per day
        out.append((f"d:{now:%Y-%m-%d}", GAP_H, DAILY[day % len(DAILY)]))
    return out


def _pick_query(tag_gap_final: bool) -> str:
    join_guard = "" if tag_gap_final else f"AND joined_at < now() - interval '{NEW_USER_H} hours' "
    return (
        f"SELECT id FROM users WHERE {growth.NOT_PAID} {join_guard}"
        "AND NOT EXISTS (SELECT 1 FROM bf_sent s WHERE s.uid = users.id AND s.tag = $1) "
        "AND NOT EXISTS (SELECT 1 FROM bf_sent s WHERE s.uid = users.id AND s.at > now() - make_interval(hours => $2)) "
        "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.user_id = users.id AND p.status = 'paid' AND p.promo_code = $3) "
        f"LIMIT {BATCH}")


async def _send(bot, uid: int, tag: str, text: str, pct: int) -> bool:
    try:
        await bot.send_message(uid, text + "\n\n" + DISCLAIMER, reply_markup=growth.plans_markup(pct))
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return False
    except Exception as e:  # blocked the bot etc.: record it anyway so we never retry
        log.info("black friday %s to %s failed: %s", tag, uid, e)
    await db.pool.execute("INSERT INTO bf_sent(uid, tag) VALUES($1, $2) ON CONFLICT DO NOTHING", uid, tag)
    return True


async def run_once(bot) -> int:
    camp = await growth.auto_offer()
    if not camp:
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    sent = 0
    for tag, gap, tpl in candidates(now, camp["start"], camp["end"]):
        text = tpl.format(pct=camp["percent"], when=_when(camp["end"]))
        final = tag in ("last24", "last6")
        for r in await db.pool.fetch(_pick_query(final), tag, gap, camp["code"]):
            if await _send(bot, r["id"], tag, text, camp["percent"]):
                sent += 1
            await asyncio.sleep(0.1)
    return sent


async def loop(bot):
    await wake.tick()
    while True:
        try:
            n = await run_once(bot)
            if n:
                log.info("black friday: %s messages sent", n)
        except Exception:
            log.exception("black friday")
        await wake.every(CHECK_EVERY)


async def on_startup(bot):
    for sql in MIGRATE:
        await db.pool.execute(sql)
    asyncio.create_task(loop(bot))


# ------------------------------------------------------------------ admin
def _parse(tok: str):
    if tok.lower() == "now":
        return dt.datetime.now(dt.timezone.utc)
    d = dt.datetime.fromisoformat(tok)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


async def _status() -> str:
    rows = await db.pool.fetch("SELECT key, value FROM settings WHERE key LIKE 'auto_%'")
    cfg = {r["key"]: r["value"] for r in rows}
    if not cfg.get("auto_code"):
        return ("🖤 <b>Black Friday campaign: not set</b>\n\n"
                "<code>/campaign BLACKFRIDAY now 2026-11-30T23:59</code> (UTC)\n"
                "Create the code first: <code>/promo_add BLACKFRIDAY 65</code>")
    code = cfg["auto_code"]
    start, end = dt.datetime.fromisoformat(cfg["auto_start"]), dt.datetime.fromisoformat(cfg["auto_end"])
    now = dt.datetime.now(dt.timezone.utc)
    state = "⏳ scheduled" if now < start else ("✅ LIVE" if now < end else "🏁 finished")
    sent = await db.pool.fetch("SELECT tag, count(*) n FROM bf_sent GROUP BY tag ORDER BY min(at)")
    paid = await db.pool.fetchrow(
        "SELECT count(*) n, coalesce(sum(amount_micro), 0) rev FROM payments WHERE status = 'paid' AND promo_code = $1", code)
    p = await db.pool.fetchrow("SELECT percent FROM promo_codes WHERE code = $1", code)
    lines = "\n".join(f"• <code>{html.escape(r['tag'])}</code>: {r['n']}" for r in sent) or "• none yet"
    return (f"🖤 <b>Black Friday campaign</b> {state}\n\n"
            f"Code <code>{html.escape(code)}</code> · {p['percent'] if p else '?'}% off\n"
            f"From {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC\n\n"
            f"<b>Messages sent</b>\n{lines}\n\n"
            f"<b>Bought with it:</b> {paid['n']} · {float(paid['rev']) / 1_000_000:,.2f} USDT\n\n"
            "While it is live, every other discount is paused.\n"
            "<code>/campaign off</code> stops it.")


@router.message(Command("campaign"))
async def campaign_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if parts and parts[0].lower() == "off":
        await db.pool.execute("DELETE FROM settings WHERE key LIKE 'auto_%'")
        growth.auto_cache_clear()
        return await m.answer("⛔️ Campaign stopped. Normal discounts are back.")
    if parts:
        usage = "Usage: <code>/campaign CODE START END</code>\nExample: <code>/campaign BLACKFRIDAY now 2026-11-30T23:59</code> (UTC)"
        try:
            code, start, end = parts[0].upper(), _parse(parts[1]), _parse(parts[2])
            if end <= start or end <= dt.datetime.now(dt.timezone.utc):
                raise ValueError
        except (IndexError, ValueError):
            return await m.answer(usage + "\nThe end must be in the future and after the start.")
        if not await db.pool.fetchval("SELECT 1 FROM promo_codes WHERE code = $1", code):
            return await m.answer(f"Code <code>{html.escape(code)}</code> does not exist. "
                                  "Create it first: <code>/promo_add BLACKFRIDAY 65</code>")
        await db.pool.execute("UPDATE promo_codes SET active = true, expires_at = $2 WHERE code = $1", code, end)
        for k, v in (("auto_code", code), ("auto_start", start.isoformat()), ("auto_end", end.isoformat())):
            await db.pool.execute("INSERT INTO settings(key, value) VALUES($1, $2) "
                                  "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", k, v)
        growth.auto_cache_clear()
    await m.answer(await _status())
