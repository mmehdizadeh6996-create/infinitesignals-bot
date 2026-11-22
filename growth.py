"""Growth tools: promo codes (users + admins) and funnel-segment messaging (admins).

Promo codes
  users:  /promo CODE (or the "Apply code" button in a campaign message) -> discount on the next purchase
  admins: /promo_add CODE PERCENT [max_uses] [days_valid] · /promo_list · /promo_off CODE
Segment messaging (funnel stages)
  admins: /segments · /msg <segment> [code=CODE] <text>  (preview + confirm before sending)
"""
import asyncio
import datetime as dt
import html
import logging
import re
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

import db
from config import ADMIN_IDS, PLANS

log = logging.getLogger("growth")
router = Router()

CODE_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
MAX_PERCENT = 90
PENDING_TTL = 600  # seconds an unconfirmed /msg preview stays valid

# automatic trial win-back offer (see winback.py)
WINBACK_CODE = "TRIAL35"
WINBACK_PCT = 35
WINBACK_HOURS = 48  # the offer is valid this long after each user's trial ends

# ---------------------------------------------------------------- segments
NOT_PAID = "(sub_end IS NULL OR sub_end <= now())"
HAS_PAY = "EXISTS (SELECT 1 FROM payments p WHERE p.user_id = users.id)"
HAS_PAID = "EXISTS (SELECT 1 FROM payments p WHERE p.user_id = users.id AND p.status = 'paid')"

SEGMENTS = {
    "trial": ("In free trial", f"trial_end > now() AND {NOT_PAID}"),
    "expiring": ("Trial ends within 24h",
                 f"trial_end > now() AND trial_end < now() + interval '24 hours' AND {NOT_PAID}"),
    "no_checkout": ("Trial ended, never opened checkout",
                    f"trial_end <= now() AND {NOT_PAID} AND NOT {HAS_PAY}"),
    "abandoned": ("Opened checkout but did not pay", f"{NOT_PAID} AND {HAS_PAY} AND NOT {HAS_PAID}"),
    "lapsed": ("Paid before, subscription ended", f"{NOT_PAID} AND {HAS_PAID}"),
    "renew": ("Subscription ends within 3 days",
              "sub_end > now() AND sub_end < now() + interval '3 days'"),
    "paid": ("Active subscribers", "sub_end > now()"),
    "all": ("Everyone", "true"),
}

Q_SEG_COUNTS = "SELECT " + ", ".join(
    f"count(*) FILTER (WHERE {w}) AS {k}" for k, (_, w) in SEGMENTS.items()) + " FROM users"

Q_SOURCES = "SELECT coalesce(source, 'organic') s, count(*) n FROM users GROUP BY 1 ORDER BY n DESC LIMIT 5"


def seg_query(name: str):
    """(where_sql, params) for a segment name, or None if unknown. Names come from the
    SEGMENTS table or `src:<tag>`; user input never reaches SQL text."""
    if name in SEGMENTS:
        return SEGMENTS[name][1], []
    if name.startswith("src:") and CODE_RE.fullmatch(name[4:]):
        return "coalesce(source, 'organic') = $1", [name[4:]]
    return None


async def seg_ids(name: str):
    q = seg_query(name)
    if q is None:
        return None
    where, params = q
    rows = await db.pool.fetch(f"SELECT id FROM users WHERE {where}", *params)
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- promo helpers
def discounted_micro(price_usdt: int, pct: int) -> int:
    return price_usdt * 1_000_000 * (100 - pct) // 100


def money(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


async def get_valid_promo(code: str):
    """Row of an active, unexpired, not-exhausted code, else None."""
    if not CODE_RE.fullmatch(code or ""):
        return None
    r = await db.pool.fetchrow("SELECT * FROM promo_codes WHERE code = $1", code.upper())
    if not r or not r["active"]:
        return None
    if r["expires_at"] and r["expires_at"] <= dt.datetime.now(dt.timezone.utc):
        return None
    if r["max_uses"] is not None and r["used"] >= r["max_uses"]:
        return None
    return r


async def used_by(uid: int, code: str) -> bool:
    return bool(await db.pool.fetchval(
        "SELECT 1 FROM payments WHERE user_id = $1 AND promo_code = $2 AND status = 'paid' LIMIT 1",
        uid, code.upper()))


async def in_window(uid: int, code: str) -> bool:
    """The win-back code only works for WINBACK_HOURS after the user's own trial ended."""
    if (code or "").upper() != WINBACK_CODE:
        return True
    return bool(await db.pool.fetchval(
        "SELECT 1 FROM users WHERE id = $1 AND trial_end + make_interval(hours => $2) > now()",
        uid, WINBACK_HOURS))


_auto_cache = (0.0, None)


def auto_cache_clear():
    global _auto_cache
    _auto_cache = (0.0, None)


async def auto_offer():
    """The running campaign (Black Friday), or None. Returns the promo row plus `start` / `end`.
    It runs while now is inside [start, end) and its code is still valid. While it runs, every other
    discount is paused (see user_discount / apply_code). Cached for a few seconds."""
    global _auto_cache
    ts, val = _auto_cache
    if time.time() - ts < 15:
        return val
    rows = await db.pool.fetch("SELECT key, value FROM settings WHERE key LIKE 'auto_%'")
    cfg = {r["key"]: r["value"] for r in rows}
    out = None
    try:
        if cfg.get("auto_code"):
            start = dt.datetime.fromisoformat(cfg["auto_start"])
            end = dt.datetime.fromisoformat(cfg["auto_end"])
            now = dt.datetime.now(dt.timezone.utc)
            if start <= now < end:
                p = await get_valid_promo(cfg["auto_code"])
                if p:
                    out = {**dict(p), "start": start, "end": end}
    except (KeyError, ValueError):
        log.exception("bad campaign settings")
    _auto_cache = (time.time(), out)
    return out


async def user_discount(uid: int):
    """(code, percent) the user can use right now, or (None, 0)."""
    camp = await auto_offer()
    if camp:  # campaign running: it replaces every other discount, for everyone, automatically
        if await used_by(uid, camp["code"]):
            return None, 0
        return camp["code"], camp["percent"]
    code = await db.pool.fetchval("SELECT promo_code FROM users WHERE id = $1", uid)
    if not code:
        return None, 0
    p = await get_valid_promo(code)
    if not p or await used_by(uid, code) or not await in_window(uid, code):
        return None, 0
    return p["code"], p["percent"]


def plans_markup(pct: int = 0) -> InlineKeyboardMarkup:
    rows = []
    for k, p in PLANS.items():
        price = money(discounted_micro(p["price"], pct) / 1_000_000)
        label = f"{p['title']} — {price} USDT" + (f" (−{pct}%)" if pct else "")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:{k}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def plans_kb_for(uid: int) -> InlineKeyboardMarkup:
    _, pct = await user_discount(uid)
    return plans_markup(pct)


async def apply_code(uid: int, username, raw: str):
    """Validates a code for a user and stores it. Returns (text, markup)."""
    await db.upsert_user(uid, username)
    camp = await auto_offer()
    if camp:
        return (f"🔥 Black Friday pricing is live: <b>{camp['percent']}% off</b> is already applied to every plan. "
                "Other codes can't be combined with it and work again after the sale."), plans_markup(camp["percent"])
    code = (raw or "").strip().upper()
    p = await get_valid_promo(code)
    if not p or await used_by(uid, code) or not await in_window(uid, code):
        return "❌ This code is invalid, expired or already used.", None
    await db.pool.execute("UPDATE users SET promo_code = $2 WHERE id = $1", uid, p["code"])
    return (f"✅ Code <b>{html.escape(p['code'])}</b> applied: <b>{p['percent']}% off</b> your next purchase.\n"
            "Choose a plan:"), plans_markup(p["percent"])


# ---------------------------------------------------------------- user handlers
@router.message(Command("promo"))
async def promo_cmd(m: Message, command: CommandObject):
    code = (command.args or "").strip()
    if not code:
        return await m.answer("🏷 Have a promo code? Send <code>/promo YOURCODE</code>.")
    text, kb = await apply_code(m.from_user.id, m.from_user.username, code)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("promo:"))
async def promo_cb(c: CallbackQuery):
    text, kb = await apply_code(c.from_user.id, c.from_user.username, c.data.split(":", 1)[1])
    await c.message.answer(text, reply_markup=kb)
    await c.answer()


# ---------------------------------------------------------------- admin: promo codes
def _is_admin(uid) -> bool:
    return uid in ADMIN_IDS


@router.message(Command("promo_add"))
async def promo_add(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    usage = ("Usage: /promo_add CODE PERCENT [max_uses] [days_valid]\n"
             "Example: /promo_add WELCOME20 20 100 7  (20% off, 100 uses, valid 7 days)\n"
             "Use 0 or omit max_uses / days for no limit.")
    parts = (command.args or "").split()
    try:
        code, pct = parts[0].upper(), int(parts[1])
        max_uses = int(parts[2]) if len(parts) > 2 else 0
        days = int(parts[3]) if len(parts) > 3 else 0
        if not CODE_RE.fullmatch(code) or not 1 <= pct <= MAX_PERCENT or max_uses < 0 or days < 0:
            raise ValueError
    except (IndexError, ValueError):
        return await m.answer(usage + f"\nPercent must be 1–{MAX_PERCENT}; code 3–32 letters, digits, _ or -.")
    await db.pool.execute(
        "INSERT INTO promo_codes(code, percent, max_uses, expires_at) "
        "VALUES($1, $2, $3, CASE WHEN $4::int > 0 THEN now() + make_interval(days => $4::int) END) "
        "ON CONFLICT (code) DO UPDATE SET percent = EXCLUDED.percent, max_uses = EXCLUDED.max_uses, "
        "expires_at = EXCLUDED.expires_at, active = true",
        code, pct, max_uses or None, days)
    await m.answer(
        f"✅ <code>{code}</code> · {pct}% off · "
        f"{max_uses or 'unlimited'} uses · {f'valid {days}d' if days else 'no expiry'}\n"
        f"Send it to users with: <code>/msg no_checkout code={code} your text</code>")


@router.message(Command("promo_off"))
async def promo_off(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    code = (command.args or "").strip().upper()
    res = await db.pool.execute("UPDATE promo_codes SET active = false WHERE code = $1", code)
    await m.answer("Disabled." if res.endswith("1") else "Code not found.")


async def render_promos():
    rows = await db.pool.fetch("SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 15")
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    for r in rows:
        if not r["active"]:
            state = "⛔️ off"
        elif r["expires_at"] and r["expires_at"] <= now:
            state = "⌛️ expired"
        elif r["max_uses"] is not None and r["used"] >= r["max_uses"]:
            state = "🈵 used up"
        else:
            state = "✅ active"
        exp = f" · until {r['expires_at']:%Y-%m-%d}" if r["expires_at"] else ""
        lines.append(f"• <code>{html.escape(r['code'])}</code> {r['percent']}% · "
                     f"{r['used']}/{r['max_uses'] if r['max_uses'] is not None else '∞'} paid uses{exp} · {state}")
    text = ("🏷 <b>Promo codes</b>\n\n" + ("\n".join(lines) or "No codes yet.") +
            "\n\n<code>/promo_add CODE PERCENT [max_uses] [days]</code>\n"
            "<code>/promo_off CODE</code>\n"
            "<i>Uses are counted when a payment is confirmed.</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Message users", callback_data="adm:seg"),
         InlineKeyboardButton(text="🔻 Funnel", callback_data="adm:funnel:all")]])
    return text, kb


@router.message(Command("promo_list"))
async def promo_list(m: Message):
    if not _is_admin(m.from_user.id):
        return
    text, kb = await render_promos()
    await m.answer(text, reply_markup=kb)


# ---------------------------------------------------------------- admin: segment messaging
async def render_segments():
    c = await db.pool.fetchrow(Q_SEG_COUNTS)
    srcs = await db.pool.fetch(Q_SOURCES)
    seg_lines = "\n".join(f"• <code>{k}</code> — {title}: <b>{c[k]}</b>" for k, (title, _) in SEGMENTS.items())
    src_lines = " · ".join(f"<code>src:{html.escape(r['s'])}</code> ({r['n']})" for r in srcs) or "—"
    text = (
        "📨 <b>Message users by funnel stage</b>\n\n"
        f"{seg_lines}\n\n"
        f"<b>By ad source:</b> {src_lines}\n\n"
        "<b>Tap a stage below</b>, then send your message text. You get a preview and the "
        "recipient count, then confirm. Start the text with <code>code=WELCOME20</code> to add an "
        "“Apply code” button.\n\n"
        "Or type it: <code>/msg no_checkout code=WELCOME20 Your 3 days are over. Get 20% off.</code>")
    pick = [InlineKeyboardButton(text=f"{title} ({c[k]})", callback_data=f"seg:pick:{k}")
            for k, (title, _) in SEGMENTS.items()]
    rows = [pick[i:i + 2] for i in range(0, len(pick), 2)]
    rows += [[InlineKeyboardButton(text="🔻 Funnel", callback_data="adm:funnel:all"),
              InlineKeyboardButton(text="🏷 Promo codes", callback_data="adm:promo")],
             [InlineKeyboardButton(text="🔄 Refresh", callback_data="adm:seg")]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("segments"))
async def segments_cmd(m: Message):
    if not _is_admin(m.from_user.id):
        return
    text, kb = await render_segments()
    await m.answer(text, reply_markup=kb)


_pending: dict[int, dict] = {}
_tasks: set = set()


def _confirm_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Send to {n}", callback_data="seg:send"),
        InlineKeyboardButton(text="✖ Cancel", callback_data="seg:cancel")]])


def _promo_kb(code):
    if not code:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🏷 Apply code {code}", callback_data=f"promo:{code}")]])


_awaiting: dict[int, dict] = {}  # admin id -> {"seg", "ts"}: tapped a stage, next text message is the body


async def _preview(m: Message, seg: str, rest: str) -> bool:
    """Validates `[code=CODE] <text>` for a segment, shows the preview and a confirm button.
    Returns True when a preview was created."""
    usage = "Send the message text (optionally starting with code=CODE)."
    code = None
    rest = rest.strip()
    if rest.lower().startswith("code="):
        tok, _, rest = rest.partition(" ")
        code = tok[5:].upper()
        if not await get_valid_promo(code):
            await m.answer(f"Code <code>{html.escape(code)}</code> is not active. Create it with /promo_add.")
            return False
    text = rest.strip()
    if not text:
        await m.answer(usage)
        return False
    ids = await seg_ids(seg)
    if ids is None:
        await m.answer("Unknown segment. See /segments.")
        return False
    if not ids:
        await m.answer("That segment has no users right now.")
        return False
    try:  # the preview is rendered exactly like the real message, so bad HTML fails here, not in the mass send
        await m.answer(text, reply_markup=_promo_kb(code))
    except TelegramBadRequest as e:
        await m.answer(f"Message could not be rendered: {html.escape(str(e))}\nFix the text and send it again.")
        return False
    _pending[m.from_user.id] = {"seg": seg, "text": text, "code": code, "ids": ids, "ts": time.time()}
    await m.answer(f"👆 Preview. Send to <b>{len(ids)}</b> users in <code>{html.escape(seg)}</code>?",
                   reply_markup=_confirm_kb(len(ids)))
    return True


@router.message(Command("msg"))
async def msg_cmd(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    parts = (command.args or "").split(None, 1)
    if len(parts) < 2:
        return await m.answer("Usage: /msg &lt;segment&gt; [code=CODE] &lt;text&gt;\nSee /segments for segment names.")
    await _preview(m, parts[0].lower(), parts[1])


def _waiting_for_text(m: Message) -> bool:
    a = _awaiting.get(m.from_user.id)
    return bool(a and time.time() - a["ts"] <= PENDING_TTL and m.text and not m.text.startswith("/"))


@router.message(F.text, _waiting_for_text)
async def msg_text(m: Message):
    if not _is_admin(m.from_user.id):
        return
    a = _awaiting[m.from_user.id]
    if await _preview(m, a["seg"], m.text):
        _awaiting.pop(m.from_user.id, None)


@router.callback_query(F.data.startswith("seg:pick:"))
async def seg_pick(c: CallbackQuery):
    if not _is_admin(c.from_user.id):
        return await c.answer("Admins only", show_alert=True)
    seg = c.data.split(":", 2)[2]
    ids = await seg_ids(seg)
    if ids is None:
        return await c.answer("Unknown segment", show_alert=True)
    if not ids:
        return await c.answer("No users in this segment right now.", show_alert=True)
    _awaiting[c.from_user.id] = {"seg": seg, "ts": time.time()}
    title = SEGMENTS[seg][0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Cancel", callback_data="seg:cancel")]])
    await c.message.answer(
        f"✍️ <b>{html.escape(title)}</b> — {len(ids)} users.\n"
        "Send the message text now. Start with <code>code=WELCOME20</code> to attach a promo code button.",
        reply_markup=kb)
    await c.answer()


async def _send_campaign(bot, admin_id: int, job: dict):
    ok = fail = 0
    kb = _promo_kb(job["code"])
    for uid in job["ids"]:
        for attempt in (1, 2):
            try:
                await bot.send_message(uid, job["text"], reply_markup=kb)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                fail += 1
                break
        else:
            fail += 1
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(admin_id, f"📨 Done: <b>{ok}</b> delivered, {fail} failed "
                                         f"(users who blocked the bot fail).")
    except Exception:
        log.exception("campaign report")


@router.callback_query(F.data.in_({"seg:send", "seg:cancel"}))
async def seg_cb(c: CallbackQuery):
    if not _is_admin(c.from_user.id):
        return await c.answer("Admins only", show_alert=True)
    job = _pending.pop(c.from_user.id, None)
    _awaiting.pop(c.from_user.id, None)
    if c.data == "seg:cancel":
        await c.message.edit_text("Cancelled.")
        return await c.answer()
    if not job or time.time() - job["ts"] > PENDING_TTL:
        await c.message.edit_text("This preview expired. Run /msg again.")
        return await c.answer()
    await c.message.edit_text(f"⏳ Sending to {len(job['ids'])} users…")
    task = asyncio.create_task(_send_campaign(c.bot, c.from_user.id, job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    await c.answer()
