import logging
import random
import aiohttp
from config import WALLET, TRONGRID_API_KEY, USDT_CONTRACT, PLANS, PAYMENT_TTL_MIN
import db

log = logging.getLogger("payments")


async def new_payment(uid: int, plan: str):
    """Creates a unique amount (price + a few cents) so the payment can be matched automatically."""
    pend = {p["amount_micro"] for p in await db.pending_payments(PAYMENT_TTL_MIN)}
    base = PLANS[plan]["price"] * 1_000_000
    for _ in range(200):
        amount = base + random.randint(1, 99) * 10_000
        if amount not in pend:
            return await db.create_payment(uid, plan, amount)
    raise RuntimeError("no free amount")


async def _transfers(session):
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    async with session.get(
        f"https://api.trongrid.io/v1/accounts/{WALLET}/transactions/trc20",
        params={"only_to": "true", "limit": 100, "contract_address": USDT_CONTRACT},
        headers=headers, timeout=aiohttp.ClientTimeout(total=20),
    ) as r:
        j = await r.json()
    return j.get("data", [])


async def process(bot) -> int:
    """Matches pending payments with incoming wallet transfers. Returns the number confirmed."""
    pend = await db.pending_payments(PAYMENT_TTL_MIN)
    if not pend:
        return 0
    async with aiohttp.ClientSession() as s:
        txs = await _transfers(s)
    done = 0
    for tx in txs:
        if tx.get("to") != WALLET:
            continue
        if tx.get("token_info", {}).get("symbol") != "USDT":
            continue
        value, ts, txid = int(tx["value"]), tx["block_timestamp"] / 1000, tx["transaction_id"]
        for p in pend:
            if p["status"] != "pending" or p["amount_micro"] != value:
                continue
            if ts < p["created_at"].timestamp() - 120:
                continue
            if await db.txid_used(txid):
                break
            await db.mark_paid(p["id"], txid)
            end = await db.activate(p["user_id"], PLANS[p["plan"]]["days"])
            done += 1
            try:
                await bot.send_message(
                    p["user_id"],
                    f"✅ Payment confirmed!\nYour <b>{PLANS[p['plan']]['title']}</b> subscription is now active.\n"
                    f"📅 Valid until: {end:%Y-%m-%d} (UTC)",
                )
            except Exception:
                log.exception("notify failed")
            break
    return done
