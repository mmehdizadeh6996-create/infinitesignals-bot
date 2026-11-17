"""Automatic trial follow-up ("win-back"): messages users around the end of their free trial
so they come back and buy. Each user receives each message at most once.

  stage 1  ~12h before the trial ends   reminder, no discount (keeps full price for those who convert anyway)
  stage 2  right after the trial ends   35% offer; the code is applied automatically, prices shown discounted
  stage 3  12h before the offer expires last-chance reminder (the offer lasts WINDOW_H hours after trial end)

Users who already paid, or who hold an active subscription, are never messaged. Users whose trial
had already ended when this feature first ran are marked 9 (skipped) so old users are not blasted.
Admins: /winback shows progress and purchases. Turn off with the env var WINBACK=0.
"""
import asyncio
import html
import logging
import os

from aiogram import Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import Message

import db
import growth
from config import ADMIN_IDS

log = logging.getLogger("winback")
router = Router()

ENABLED = os.getenv("WINBACK", "1") != "0"
CODE, PCT, WINDOW_H = growth.WINBACK_CODE, growth.WINBACK_PCT, growth.WINBACK_HOURS
LAST_CHANCE_AT_H = WINDOW_H - 12  # hours after trial end
CHECK_EVERY = 600  # seconds
BATCH = 200
SKIPPED = 9

MIGRATE = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS winback_stage INT",
    f"UPDATE users SET winback_stage = CASE WHEN trial_end <= now() THEN {SKIPPED} ELSE 0 END "
    "WHERE winback_stage IS NULL",
    "ALTER TABLE users ALTER COLUMN winback_stage SET DEFAULT 0",
    f"INSERT INTO promo_codes(code, percent) VALUES('{CODE}', {PCT}) ON CONFLICT (code) DO NOTHING",
]

_ELIGIBLE = f"{growth.NOT_PAID} AND NOT {growth.HAS_PAID}"
QUERIES = {
    1: ("SELECT id FROM users WHERE coalesce(winback_stage, 0) = 0 AND trial_end > now() "
        f"AND trial_end <= now() + interval '12 hours' AND {_ELIGIBLE} LIMIT {BATCH}"),
    2: ("SELECT id FROM users WHERE coalesce(winback_stage, 0) < 2 AND trial_end <= now() "
        f"AND trial_end + interval '{LAST_CHANCE_AT_H} hours' > now() AND {_ELIGIBLE} LIMIT {BATCH}"),
    3: ("SELECT id FROM users WHERE coalesce(winback_stage, 0) = 2 "
        f"AND trial_end + interval '{LAST_CHANCE_AT_H} hours' <= now() "
        f"AND trial_end + interval '{WINDOW_H} hours' > now() AND {_ELIGIBLE} LIMIT {BATCH}"),
}

DISCLAIMER = "<i>Trading carries risk. Not financial advice.</i>"
TEXT = {
    1: ("⏳ <b>Your free trial ends in about 12 hours</b>\n\n"
        "You have had full access to every setup, entry zone and live tracker update. "
        "Keep it going without a gap: pick a plan below and you are set in under a minute.\n\n" + DISCLAIMER),
    2: (f"🎁 <b>Your trial has ended, but we saved you a deal</b>\n\n"
        f"Get <b>{PCT}% OFF</b> any plan for the next {WINDOW_H} hours. Your code <b>{CODE}</b> is already "
        f"applied, so the prices below include the discount.\n\n"
        "Stay consistent with clear entries, defined stop-losses and live tracking, "
        "without missing a single update.\n\n" + DISCLAIMER),
    3: (f"⚡️ <b>12 hours left on your {PCT}% discount</b>\n\n"
        "After that, plans go back to full price. Choose yours below and you are back in minutes.\n\n"
        + DISCLAIMER),
}


async def _send(bot, uid: int, stage: int) -> bool:
    """Sends one stage message. Returns False only when Telegram asked us to slow down (retry later)."""
    if stage >= 2:  # apply the offer before showing prices, so the buttons and checkout match
        await db.pool.execute("UPDATE users SET promo_code = $2 WHERE id = $1", uid, CODE)
    try:
        await bot.send_message(uid, TEXT[stage], reply_markup=growth.plans_markup(PCT if stage >= 2 else 0))
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return False
    except Exception as e:  # blocked the bot, deleted account, ...: do not retry forever
        log.info("winback stage %s to %s failed: %s", stage, uid, e)
    await db.pool.execute("UPDATE users SET winback_stage = $2 WHERE id = $1", uid, stage)
    return True


async def run_once(bot) -> int:
    sent = 0
    for stage in (1, 2, 3):
        for r in await db.pool.fetch(QUERIES[stage]):
            if await _send(bot, r["id"], stage):
                sent += 1
            await asyncio.sleep(0.1)
    return sent


async def loop(bot):
    await asyncio.sleep(30)
    while True:
        try:
            n = await run_once(bot)
            if n:
                log.info("winback: %s messages sent", n)
        except Exception:
            log.exception("winback")
        await asyncio.sleep(CHECK_EVERY)


_task = None


async def on_startup(bot):
    """Registered as a dispatcher startup hook in main.py (the database is ready by then)."""
    global _task
    for sql in MIGRATE:
        await db.pool.execute(sql)
    if ENABLED:
        _task = asyncio.create_task(loop(bot))
        log.info("winback enabled: %s%% code %s, window %sh", PCT, CODE, WINDOW_H)
    else:
        log.info("winback disabled (WINBACK=0)")


Q_STATS = f"""
SELECT
  count(*) FILTER (WHERE winback_stage BETWEEN 1 AND 3) reminded,
  count(*) FILTER (WHERE winback_stage BETWEEN 2 AND 3) offered,
  count(*) FILTER (WHERE winback_stage = 3) last_chance,
  count(*) FILTER (WHERE winback_stage BETWEEN 2 AND 3 AND EXISTS (
      SELECT 1 FROM payments p WHERE p.user_id = users.id AND p.status = 'paid' AND p.promo_code = '{CODE}')) bought,
  count(*) FILTER (WHERE winback_stage = {SKIPPED}) skipped
FROM users
"""
Q_REV = f"SELECT coalesce(sum(amount_micro), 0) FROM payments WHERE status = 'paid' AND promo_code = '{CODE}'"


def fmt_stats(s, rev_micro) -> str:
    return (
        "🔁 <b>Trial follow-up (automatic)</b>\n\n"
        f"Status: {'✅ ON' if ENABLED else '⛔️ OFF (WINBACK=0)'}\n"
        f"Offer: {PCT}% with code <code>{html.escape(CODE)}</code>, valid {WINDOW_H}h after the trial ends\n\n"
        f"• Reminded before the end: <b>{s['reminded']}</b>\n"
        f"• Offer sent: <b>{s['offered']}</b>\n"
        f"• Last-chance sent: <b>{s['last_chance']}</b>\n"
        f"• Bought with the offer: <b>{s['bought']}</b>"
        f" ({growth_pct(s['bought'], s['offered'])} of offers) · {rev_micro / 1_000_000:,.2f} USDT\n"
        f"• Skipped (trial had already ended before launch): {s['skipped']}\n\n"
        "<i>Messages go out every 10 minutes, once per user per stage. "
        "Manual sends are still available in /segments.</i>")


def growth_pct(a, b) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


@router.message(Command("winback"))
async def winback_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    s = await db.pool.fetchrow(Q_STATS)
    rev = await db.pool.fetchval(Q_REV)
    await m.answer(fmt_stats(s, float(rev or 0)))
