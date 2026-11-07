import asyncio
import datetime as dt
import logging

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import analysis
import db
import payments
from config import (ADMIN_IDS, BOT_TOKEN, MAX_DAILY_SIGNALS, MIN_SCORE, PAYMENT_TTL_MIN, PLANS,
                    SCAN_INTERVAL, SIGNAL_MAX_AGE_HOURS, SYMBOL_COOLDOWN_HOURS, SYMBOLS,
                    TRIAL_DAYS, WALLET)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("infinitesignals")
dp = Dispatcher()

BTN_TRACKER = "📊 Signal Tracker"
BTN_BUY = "💳 Subscribe"
BTN_ME = "👤 My Subscription"
BTN_STATS = "📈 Performance"
BTN_HELP = "ℹ️ Help"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_TRACKER), KeyboardButton(text=BTN_STATS)],
              [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_ME)],
              [KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True)

DISCLAIMER = ("⚠️ These signals are not financial advice. Crypto is high risk; "
              "always use proper risk management (risk no more than 1-2% per trade).")


def f(p):
    return analysis.fmt(p)


def signal_text(s) -> str:
    emoji, label = ("🟢", "LONG (Buy)") if s["side"] == "LONG" else ("🔴", "SHORT (Sell)")
    return (
        f"🚨 <b>Signal #{s['id']}</b>\n\n"
        f"🪙 <b>{s['symbol']}/USDT</b> — {emoji} {label}\n"
        f"🎯 Entry: <code>{f(s['entry'])}</code>\n"
        f"🛑 Stop Loss: <code>{f(s['sl'])}</code>\n"
        f"✅ Target 1: <code>{f(s['tp1'])}</code>\n"
        f"✅ Target 2: <code>{f(s['tp2'])}</code>\n\n"
        f"📊 Strength: {s['score']}/8 | ⏱ Timeframe: 1H\n"
        f"💡 After Target 1 is hit, move your stop loss to the entry price.\n\n{DISCLAIMER}"
    )


async def broadcast(bot: Bot, ids, text):
    for uid in ids:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass
        await asyncio.sleep(0.05)


def plans_kb():
    rows = [[InlineKeyboardButton(text=f"{p['title']} — {p['price']} USDT", callback_data=f"buy:{k}")]
            for k, p in PLANS.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def need_access(m: Message) -> bool:
    if await db.has_access(m.from_user.id):
        return True
    await m.answer("⛔️ Your free trial or subscription has ended. Choose a plan to continue:",
                   reply_markup=plans_kb())
    return False


# ---------------- handlers ----------------
@dp.message(CommandStart())
async def start(m: Message):
    _, new = await db.upsert_user(m.from_user.id, m.from_user.username)
    txt = ("👋 Welcome to <b>InfiniteSignals</b>!\n\n"
           "📡 A few high-quality, carefully filtered signals per day across top coins and meme coins "
           "(BTC, ETH, SOL, DOGE, PEPE, SHIB, WIF and more)\n"
           "📊 Live tracker: follow every signal until it hits its target or stop loss\n"
           "💳 Pay with USDT (TRC20 network)\n\n")
    txt += (f"🎁 Your <b>{TRIAL_DAYS}-day free trial</b> is now active!" if new
            else "Tap “👤 My Subscription” to see your status.")
    await m.answer(txt, reply_markup=MENU)


@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def help_(m: Message):
    await m.answer(
        "ℹ️ <b>Help</b>\n\n"
        "• Every signal includes an entry, a stop loss and two targets.\n"
        f"• At most {MAX_DAILY_SIGNALS} signals are sent per 24 hours, so you are never overwhelmed.\n"
        "• Use “📊 Signal Tracker” to see the live status of all open signals.\n"
        "• The result of every signal (target / stop loss) is sent to you automatically.\n\n" + DISCLAIMER)


@dp.message(F.text == BTN_ME)
@dp.message(Command("me"))
async def me(m: Message):
    u = await db.get_user(m.from_user.id)
    if not u:
        u, _ = await db.upsert_user(m.from_user.id, m.from_user.username)
    now = dt.datetime.now(dt.timezone.utc)
    lines = ["👤 <b>Subscription status</b>\n"]
    if u["sub_end"] and u["sub_end"] > now:
        lines.append(f"✅ Active until {u['sub_end']:%Y-%m-%d} (UTC)")
    elif u["trial_end"] and u["trial_end"] > now:
        lines.append(f"🎁 Free trial until {u['trial_end']:%Y-%m-%d %H:%M} (UTC)")
    else:
        lines.append("⛔️ You have no active subscription.")
    await m.answer("\n".join(lines), reply_markup=plans_kb())


@dp.message(F.text == BTN_BUY)
@dp.message(Command("buy"))
async def buy(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await m.answer("💳 Choose a plan:", reply_markup=plans_kb())


@dp.callback_query(F.data.startswith("buy:"))
async def buy_cb(c: CallbackQuery):
    plan = c.data.split(":")[1]
    if plan not in PLANS:
        return await c.answer()
    await db.upsert_user(c.from_user.id, c.from_user.username)
    p = await payments.new_payment(c.from_user.id, plan)
    amount = p["amount_micro"] / 1_000_000
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="✅ I've paid — check now", callback_data=f"chk:{p['id']}")]])
    await c.message.answer(
        f"🧾 <b>{PLANS[plan]['title']} plan</b>\n\n"
        f"Send exactly this amount to the address below:\n\n"
        f"💰 Amount: <code>{amount:.2f}</code> USDT\n"
        f"🌐 Network: <b>TRON (TRC20)</b>\n"
        f"📬 Address:\n<code>{WALLET}</code>\n\n"
        f"⚠️ The amount must match <b>exactly</b> (the extra cents identify your payment).\n"
        f"⏳ This request is valid for {PAYMENT_TTL_MIN} minutes. Your subscription activates "
        f"automatically once the network confirms the transfer.",
        reply_markup=kb)
    await c.answer()


@dp.callback_query(F.data.startswith("chk:"))
async def check_cb(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    try:
        await payments.process(c.bot)
    except Exception:
        log.exception("payment check")
    p = await db.get_payment(pid)
    if p and p["status"] == "paid":
        await c.answer("✅ Payment confirmed", show_alert=True)
    elif p and p["status"] == "expired":
        await c.answer("⌛️ This request has expired. Please choose a plan again.", show_alert=True)
    else:
        await c.answer("No transaction found yet. Please check again in a few minutes.", show_alert=True)


@dp.message(F.text == BTN_TRACKER)
@dp.message(Command("tracker"))
async def tracker(m: Message):
    if not await need_access(m):
        return
    sigs = await db.open_signals()
    if not sigs:
        return await m.answer("📭 There are no open signals right now. You'll get a message as soon as a new one is issued.")
    out = ["📊 <b>Open signals</b>\n"]
    async with aiohttp.ClientSession() as s:
        for g in sigs:
            price = await analysis.ticker(s, f"{g['symbol']}-USDT")
            if price is None:
                continue
            sign = 1 if g["side"] == "LONG" else -1
            pnl = (price - g["entry"]) / g["entry"] * 100 * sign
            st = "🎯 Target 1 hit (stop moved to entry)" if g["status"] == "tp1" else "⏳ In progress"
            icon = "🟢" if pnl >= 0 else "🔴"
            out.append(f"<b>#{g['id']} {g['symbol']}</b> {g['side']} — {st}\n"
                       f"Entry <code>{f(g['entry'])}</code> | Now <code>{f(price)}</code> | {icon} {pnl:+.2f}%\n"
                       f"SL <code>{f(g['sl'])}</code> | TP1 <code>{f(g['tp1'])}</code> | TP2 <code>{f(g['tp2'])}</code>\n")
    await m.answer("\n".join(out))


@dp.message(F.text == BTN_STATS)
@dp.message(Command("stats"))
async def stats(m: Message):
    s = await db.stats()
    closed = s["wins"] + s["losses"]
    rate = f"{s['wins'] / closed * 100:.0f}%" if closed else "—"
    await m.answer(
        "📈 <b>Performance (real and transparent)</b>\n\n"
        f"✅ Winners: {s['wins']}\n❌ Stopped out: {s['losses']}\n⌛️ Expired: {s['expired']}\n"
        f"⏳ Open: {s['open']}\n🎯 Win rate: {rate}")


# ---------------- admin ----------------
@dp.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    u = await db.count_users()
    await m.answer(f"👥 Users: {u['total']}\n💎 Subscribed: {u['paid']}\n🎁 On trial: {u['trial']}\n\n"
                   "/grant &lt;user_id&gt; &lt;days&gt;\n/broadcast &lt;text&gt;")


@dp.message(Command("grant"))
async def grant(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS or not command.args:
        return
    uid, days = command.args.split()
    end = await db.activate(int(uid), int(days))
    if end is None:
        return await m.answer("User not found.")
    await m.answer(f"Done. Active until {end:%Y-%m-%d}")


@dp.message(Command("broadcast"))
async def bc(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS or not command.args:
        return
    await broadcast(m.bot, await db.all_user_ids(), command.args)
    await m.answer("Sent.")


# ---------------- background loops ----------------
async def scanner(bot: Bot):
    await asyncio.sleep(10)
    while True:
        try:
            used = await db.signals_last_24h()
            if used < MAX_DAILY_SIGNALS:
                cands = []
                async with aiohttp.ClientSession() as s:
                    for sym in SYMBOLS:
                        if await db.symbol_busy(sym, SYMBOL_COOLDOWN_HOURS):
                            continue
                        try:
                            c1 = await analysis.candles(s, f"{sym}-USDT", "1H")
                            c4 = await analysis.candles(s, f"{sym}-USDT", "4H")
                            sig = analysis.analyze(sym, c1, c4)
                            if sig and sig["score"] >= MIN_SCORE:
                                cands.append(sig)
                        except Exception as e:
                            log.warning("scan %s failed: %s", sym, e)
                        await asyncio.sleep(0.2)
                cands.sort(key=lambda x: x["score"], reverse=True)
                for sig in cands[:MAX_DAILY_SIGNALS - used]:
                    row = await db.add_signal(sig)
                    await broadcast(bot, await db.active_user_ids(), signal_text(row))
        except Exception:
            log.exception("scanner")
        await asyncio.sleep(SCAN_INTERVAL)


async def tracker_loop(bot: Bot):
    await asyncio.sleep(20)
    while True:
        try:
            sigs = await db.open_signals()
            if sigs:
                async with aiohttp.ClientSession() as s:
                    for g in sigs:
                        price = await analysis.ticker(s, f"{g['symbol']}-USDT")
                        if price is None:
                            continue
                        await handle_signal(bot, g, price)
        except Exception:
            log.exception("tracker")
        await asyncio.sleep(60)


async def handle_signal(bot: Bot, g, price):
    long = g["side"] == "LONG"
    sl = g["entry"] if g["status"] == "tp1" else g["sl"]
    hit_sl = price <= sl if long else price >= sl
    hit_tp2 = price >= g["tp2"] if long else price <= g["tp2"]
    hit_tp1 = price >= g["tp1"] if long else price <= g["tp1"]
    age = dt.datetime.now(dt.timezone.utc) - g["created_at"]
    head = f"<b>#{g['id']} {g['symbol']}/USDT {g['side']}</b>\n"
    msg = None
    if hit_tp2:
        await db.set_status(g["id"], "tp2", True)
        msg = head + "🏆 Target 2 reached! Signal closed with full profit."
    elif hit_sl:
        if g["status"] == "tp1":
            await db.set_status(g["id"], "tp1_be", True)
            msg = head + "🟡 Price returned to entry after Target 1; signal closed at break-even."
        else:
            await db.set_status(g["id"], "sl", True)
            msg = head + "❌ Stop loss hit. Signal closed."
    elif hit_tp1 and g["status"] == "open":
        await db.set_status(g["id"], "tp1")
        msg = head + f"🎯 Target 1 reached! Move your stop loss to entry (<code>{f(g['entry'])}</code>)."
    elif age > dt.timedelta(hours=SIGNAL_MAX_AGE_HOURS):
        await db.set_status(g["id"], "expired", True)
        msg = head + "⌛️ Signal closed because it expired."
    if msg:
        await broadcast(bot, await db.active_user_ids(), msg)


async def payment_loop(bot: Bot):
    await asyncio.sleep(15)
    while True:
        try:
            await payments.process(bot)
        except Exception:
            log.exception("payments")
        await asyncio.sleep(45)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")
    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    for loop in (scanner, tracker_loop, payment_loop):
        asyncio.create_task(loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
