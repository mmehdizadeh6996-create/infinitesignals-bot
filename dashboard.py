"""Admin dashboard: /dashboard (overview + sections), /users, /user <id>.

Registered on the main dispatcher from main.py. All handlers are admin-only
(ADMIN_IDS). Times are UTC. Revenue periods use payments.created_at because the
payments table has no separate paid_at column (payments confirm within the
60-minute window, so the difference is negligible).
"""
import datetime as dt
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

import db
from config import ADMIN_IDS, PLANS

router = Router()
PAGE = 10

# ---------------------------------------------------------------- SQL
Q_USERS = """
SELECT count(*) total,
  count(*) FILTER (WHERE joined_at > now() - interval '1 day') d1,
  count(*) FILTER (WHERE joined_at > now() - interval '7 days') d7,
  count(*) FILTER (WHERE joined_at > now() - interval '30 days') d30,
  count(*) FILTER (WHERE sub_end > now()) paid,
  count(*) FILTER (WHERE trial_end > now() AND (sub_end IS NULL OR sub_end <= now())) trial,
  count(*) FILTER (WHERE (trial_end IS NULL OR trial_end <= now())
                     AND (sub_end IS NULL OR sub_end <= now())) expired,
  count(*) FILTER (WHERE sub_end > now() AND sub_end < now() + interval '3 days') exp_sub,
  count(*) FILTER (WHERE trial_end > now() AND trial_end < now() + interval '24 hours'
                     AND (sub_end IS NULL OR sub_end <= now())) exp_trial
FROM users
"""

Q_BUYERS = """
SELECT count(*) buyers, count(*) FILTER (WHERE n >= 2) repeat_buyers
FROM (SELECT user_id, count(*) n FROM payments WHERE status = 'paid' GROUP BY user_id) t
"""

Q_DAILY = """
SELECT (joined_at AT TIME ZONE 'UTC')::date AS d, count(*) n
FROM users WHERE joined_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 1
"""

Q_REV = """
SELECT
  coalesce(sum(amount_micro) FILTER (WHERE status = 'paid'), 0) total,
  coalesce(sum(amount_micro) FILTER (WHERE status = 'paid' AND created_at > now() - interval '1 day'), 0) d1,
  coalesce(sum(amount_micro) FILTER (WHERE status = 'paid' AND created_at > now() - interval '7 days'), 0) d7,
  coalesce(sum(amount_micro) FILTER (WHERE status = 'paid' AND created_at > now() - interval '30 days'), 0) d30,
  count(*) FILTER (WHERE status = 'paid') n_paid,
  count(*) FILTER (WHERE status = 'pending') n_pending,
  count(*) FILTER (WHERE status = 'expired') n_expired,
  coalesce(sum(amount_micro) FILTER (WHERE status = 'pending'), 0) pending_amt
FROM payments
"""

Q_REV_PLANS = """
SELECT plan, count(*) n, coalesce(sum(amount_micro), 0) s
FROM payments WHERE status = 'paid' GROUP BY plan ORDER BY s DESC
"""

Q_REV_RECENT = """
SELECT user_id, plan, amount_micro, created_at, txid
FROM payments WHERE status = 'paid' ORDER BY created_at DESC LIMIT 5
"""

Q_SIG = """
SELECT count(*) total,
  count(*) FILTER (WHERE created_at > now() - interval '24 hours') d1,
  count(*) FILTER (WHERE created_at > now() - interval '7 days') d7,
  count(*) FILTER (WHERE status IN ('open', 'tp1')) open,
  count(*) FILTER (WHERE status = 'tp2') tp2,
  count(*) FILTER (WHERE status = 'tp1_be') be,
  count(*) FILTER (WHERE status = 'sl') sl,
  count(*) FILTER (WHERE status = 'expired') expired,
  count(*) FILTER (WHERE side = 'LONG') longs,
  count(*) FILTER (WHERE side = 'SHORT') shorts,
  coalesce(avg(score), 0) avg_score
FROM signals
"""

Q_SYM = """
SELECT symbol, count(*) n,
  count(*) FILTER (WHERE status = 'tp2') w,
  count(*) FILTER (WHERE status = 'tp1_be') b,
  count(*) FILTER (WHERE status = 'sl') l
FROM signals GROUP BY symbol ORDER BY n DESC, symbol LIMIT 6
"""

Q_SIG_RECENT = """
SELECT id, symbol, side, status, score, created_at
FROM signals ORDER BY created_at DESC LIMIT 5
"""


# ---------------------------------------------------------------- helpers
def usdt(micro) -> str:
    return f"{float(micro or 0) / 1_000_000:,.2f}"


def pct(a, b) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


def bar(n, mx, width=10) -> str:
    if not n or not mx:
        return ""
    return "█" * max(1, round(n / mx * width))


def when(d) -> str:
    return f"{d:%Y-%m-%d %H:%M}" if d else "—"


def user_status(r, now) -> str:
    if r["sub_end"] and r["sub_end"] > now:
        return f"💎 paid until {r['sub_end']:%Y-%m-%d}"
    if r["trial_end"] and r["trial_end"] > now:
        return f"🎁 trial until {r['trial_end']:%Y-%m-%d %H:%M}"
    return "⛔️ expired"


def plan_title(key) -> str:
    return PLANS.get(key, {}).get("title", key)


STATUS_ICON = {"open": "⏳", "tp1": "🎯", "tp2": "🏆", "tp1_be": "🟡", "sl": "❌", "expired": "⌛️"}


def keyboard(current, extra_row=None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Overview", callback_data="adm:overview"),
         InlineKeyboardButton(text="👥 Users", callback_data="adm:users:1")],
        [InlineKeyboardButton(text="💰 Revenue", callback_data="adm:revenue"),
         InlineKeyboardButton(text="📡 Signals", callback_data="adm:signals")],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"adm:{current}")],
    ]
    if extra_row:
        rows.insert(0, extra_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signal_perf(s):
    closed = s["tp2"] + s["be"] + s["sl"]
    net_r = 3 * s["tp2"] - s["sl"]  # TP2 = +3R, break-even after TP1 = 0R, SL = -1R
    return closed, net_r


# ---------------------------------------------------------------- formatters (pure)
def fmt_overview(u, b, rev, s, daily, now) -> str:
    closed, net_r = signal_perf(s)
    days = [(now - dt.timedelta(days=i)).date() for i in range(6, -1, -1)]
    counts = {r["d"]: r["n"] for r in daily}
    mx = max([counts.get(d, 0) for d in days] + [0])
    spark = "\n".join(
        f"<code>{d:%m-%d}</code> {bar(counts.get(d, 0), mx):<10} {counts.get(d, 0)}" for d in days)
    return (
        "📊 <b>Dashboard — Overview</b>\n"
        f"<i>Updated {now:%Y-%m-%d %H:%M} UTC</i>\n\n"
        f"👥 <b>Users: {u['total']}</b>\n"
        f"• New: 24h <b>{u['d1']}</b> · 7d <b>{u['d7']}</b> · 30d <b>{u['d30']}</b>\n"
        f"• 💎 Paid: <b>{u['paid']}</b> · 🎁 Trial: <b>{u['trial']}</b> · ⛔️ Expired: <b>{u['expired']}</b>\n"
        f"• ⏳ Expiring: {u['exp_sub']} subscriptions (3d), {u['exp_trial']} trials (24h)\n"
        f"• Ever paid: {b['buyers']} ({pct(b['buyers'], u['total'])} of all users)\n"
        f"• Repeat buyers: {b['repeat_buyers']}\n\n"
        f"💰 <b>Revenue (USDT)</b>\n"
        f"• Total <b>{usdt(rev['total'])}</b>\n"
        f"• 24h {usdt(rev['d1'])} · 7d {usdt(rev['d7'])} · 30d {usdt(rev['d30'])}\n"
        f"• Pending: {rev['n_pending']} ({usdt(rev['pending_amt'])})\n\n"
        f"📡 <b>Signals: {s['total']}</b> (24h {s['d1']} · open {s['open']})\n"
        f"• 🏆 TP2 {s['tp2']} · 🟡 BE {s['be']} · ❌ SL {s['sl']} · ⌛️ expired {s['expired']}\n"
        f"• Win rate (TP2 only): {pct(s['tp2'], closed)} · Net result: {net_r:+d}R\n\n"
        f"📈 <b>Signups, last 7 days</b>\n{spark}"
    )


def fmt_revenue(rev, plans, b, recent, users_total) -> str:
    started = rev["n_paid"] + rev["n_pending"] + rev["n_expired"]
    plan_lines = "\n".join(
        f"• {plan_title(p['plan'])}: {p['n']} sold · {usdt(p['s'])} USDT" for p in plans
    ) or "• No paid orders yet"
    recent_lines = "\n".join(
        f"• <code>{r['user_id']}</code> {plan_title(r['plan'])} · {usdt(r['amount_micro'])} · {when(r['created_at'])}"
        for r in recent
    ) or "• —"
    arppu = float(rev["total"] or 0) / 1_000_000 / b["buyers"] if b["buyers"] else 0
    arpu = float(rev["total"] or 0) / 1_000_000 / users_total if users_total else 0
    return (
        "💰 <b>Dashboard — Revenue</b>\n\n"
        f"<b>Total: {usdt(rev['total'])} USDT</b>\n"
        f"• 24h {usdt(rev['d1'])} · 7d {usdt(rev['d7'])} · 30d {usdt(rev['d30'])}\n"
        f"• Avg per paying user: {arppu:,.2f} · avg per user: {arpu:,.2f}\n\n"
        f"<b>Orders</b>\n"
        f"• Started {started} · paid {rev['n_paid']} · pending {rev['n_pending']} · expired {rev['n_expired']}\n"
        f"• Checkout completion: {pct(rev['n_paid'], started)}\n"
        f"• Pending amount: {usdt(rev['pending_amt'])} USDT\n\n"
        f"<b>By plan</b>\n{plan_lines}\n\n"
        f"<b>Latest paid orders</b>\n{recent_lines}\n\n"
        "<i>Amounts include the extra cents used to identify each payment.</i>"
    )


def fmt_signals(s, syms, recent) -> str:
    closed, net_r = signal_perf(s)
    sym_lines = "\n".join(
        f"• {r['symbol']}: {r['n']} signals · 🏆{r['w']} 🟡{r['b']} ❌{r['l']}" for r in syms) or "• —"
    recent_lines = "\n".join(
        f"• #{r['id']} {r['symbol']} {r['side']} {STATUS_ICON.get(r['status'], '')} {r['status']} "
        f"(score {r['score']}) · {when(r['created_at'])}" for r in recent) or "• —"
    return (
        "📡 <b>Dashboard — Signals</b>\n\n"
        f"Total <b>{s['total']}</b> · 24h {s['d1']} · 7d {s['d7']} · open {s['open']}\n"
        f"Long {s['longs']} · Short {s['shorts']} · avg score {float(s['avg_score']):.1f}/8\n\n"
        f"<b>Results ({closed} closed)</b>\n"
        f"• 🏆 TP2 (+3R): {s['tp2']}\n"
        f"• 🟡 Break-even after TP1 (0R): {s['be']}\n"
        f"• ❌ Stop loss (−1R): {s['sl']}\n"
        f"• ⌛️ Expired (not counted): {s['expired']}\n"
        f"• Win rate (TP2 only): {pct(s['tp2'], closed)}\n"
        f"• Not-a-loss rate (TP2 + BE): {pct(s['tp2'] + s['be'], closed)}\n"
        f"• Net result: <b>{net_r:+d}R</b>\n\n"
        f"<b>Most active symbols</b>\n{sym_lines}\n\n"
        f"<b>Latest signals</b>\n{recent_lines}"
    )


def fmt_users(rows, page, pages, total, now) -> str:
    lines = []
    for r in rows:
        name = f"@{html.escape(r['username'])}" if r["username"] else "—"
        lines.append(
            f"<code>{r['id']}</code> {name}\n   {user_status(r, now)} · joined {r['joined_at']:%Y-%m-%d}")
    body = "\n".join(lines) or "No users yet."
    return f"👥 <b>Users</b> — page {page}/{pages} ({total} total)\n\n{body}\n\n<i>Details: /user &lt;id&gt;</i>"


def fmt_user(u, pays, now) -> str:
    name = f"@{html.escape(u['username'])}" if u["username"] else "—"
    pay_lines = "\n".join(
        f"• #{p['id']} {plan_title(p['plan'])} · {usdt(p['amount_micro'])} · {p['status']} · {when(p['created_at'])}"
        for p in pays) or "• No payments"
    return (
        f"👤 <b>User <code>{u['id']}</code></b> {name}\n\n"
        f"Status: {user_status(u, now)}\n"
        f"Joined: {when(u['joined_at'])}\n"
        f"Trial end: {when(u['trial_end'])}\n"
        f"Subscription end: {when(u['sub_end'])}\n\n"
        f"<b>Payments</b>\n{pay_lines}\n\n"
        f"<i>Grant days: /grant {u['id']} &lt;days&gt;</i>"
    )


# ---------------------------------------------------------------- data + render
def _now():
    return dt.datetime.now(dt.timezone.utc)


async def render(section: str, page: int = 1):
    pool = db.pool
    now = _now()
    if section == "revenue":
        rev = await pool.fetchrow(Q_REV)
        plans = await pool.fetch(Q_REV_PLANS)
        b = await pool.fetchrow(Q_BUYERS)
        recent = await pool.fetch(Q_REV_RECENT)
        total = await pool.fetchval("SELECT count(*) FROM users")
        return fmt_revenue(rev, plans, b, recent, total), keyboard("revenue")
    if section == "signals":
        s = await pool.fetchrow(Q_SIG)
        syms = await pool.fetch(Q_SYM)
        recent = await pool.fetch(Q_SIG_RECENT)
        return fmt_signals(s, syms, recent), keyboard("signals")
    if section == "users":
        total = await pool.fetchval("SELECT count(*) FROM users")
        pages = max(1, -(-total // PAGE))
        page = min(max(1, page), pages)
        rows = await pool.fetch(
            "SELECT id, username, joined_at, trial_end, sub_end FROM users "
            "ORDER BY joined_at DESC LIMIT $1 OFFSET $2", PAGE, (page - 1) * PAGE)
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"adm:users:{page - 1}"))
        if page < pages:
            nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"adm:users:{page + 1}"))
        kb = keyboard(f"users:{page}", nav or None)
        return fmt_users(rows, page, pages, total, now), kb
    # overview (default)
    u = await pool.fetchrow(Q_USERS)
    b = await pool.fetchrow(Q_BUYERS)
    rev = await pool.fetchrow(Q_REV)
    s = await pool.fetchrow(Q_SIG)
    daily = await pool.fetch(Q_DAILY)
    return fmt_overview(u, b, rev, s, daily, now), keyboard("overview")


# ---------------------------------------------------------------- handlers
@router.message(Command("dashboard"))
async def dashboard_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    text, kb = await render("overview")
    await m.answer(text, reply_markup=kb)


@router.message(Command("users"))
async def users_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return
    page = int(command.args) if command.args and command.args.strip().isdigit() else 1
    text, kb = await render("users", page)
    await m.answer(text, reply_markup=kb)


@router.message(Command("user"))
async def user_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await m.answer("Usage: /user &lt;telegram_id&gt;")
    u = await db.pool.fetchrow("SELECT * FROM users WHERE id=$1", int(arg))
    if not u:
        return await m.answer("User not found.")
    pays = await db.pool.fetch(
        "SELECT id, plan, amount_micro, status, created_at FROM payments "
        "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10", int(arg))
    await m.answer(fmt_user(u, pays, _now()))


@router.callback_query(F.data.startswith("adm:"))
async def dashboard_cb(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Admins only", show_alert=True)
    parts = c.data.split(":")
    section = parts[1] if len(parts) > 1 else "overview"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    text, kb = await render(section, page)
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # "message is not modified" when nothing changed
    await c.answer()
