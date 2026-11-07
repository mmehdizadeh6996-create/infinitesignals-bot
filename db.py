import asyncpg
from config import DATABASE_URL, TRIAL_DAYS

pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY,
    username TEXT,
    joined_at TIMESTAMPTZ DEFAULT now(),
    trial_end TIMESTAMPTZ,
    sub_end TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS payments(
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    plan TEXT NOT NULL,
    amount_micro BIGINT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT now(),
    txid TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS signals(
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry DOUBLE PRECISION, sl DOUBLE PRECISION, tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION,
    score INT,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMPTZ DEFAULT now(),
    closed_at TIMESTAMPTZ
);
"""


async def init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await pool.execute(SCHEMA)


# ---------- users ----------
async def upsert_user(uid, username):
    row = await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if row:
        return row, False
    row = await pool.fetchrow(
        "INSERT INTO users(id,username,trial_end) VALUES($1,$2, now()+make_interval(days=>$3)) RETURNING *",
        uid, username, TRIAL_DAYS,
    )
    return row, True


async def get_user(uid):
    return await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid)


async def has_access(uid) -> bool:
    return bool(await pool.fetchval(
        "SELECT 1 FROM users WHERE id=$1 AND (trial_end > now() OR sub_end > now())", uid))


async def active_user_ids():
    rows = await pool.fetch("SELECT id FROM users WHERE trial_end > now() OR sub_end > now()")
    return [r["id"] for r in rows]


async def all_user_ids():
    return [r["id"] for r in await pool.fetch("SELECT id FROM users")]


async def activate(uid, days):
    return await pool.fetchval(
        "UPDATE users SET sub_end = GREATEST(now(), COALESCE(sub_end, now())) + make_interval(days=>$2) "
        "WHERE id=$1 RETURNING sub_end", uid, days)


async def count_users():
    return await pool.fetchrow(
        "SELECT count(*) total, count(*) FILTER (WHERE sub_end > now()) paid, "
        "count(*) FILTER (WHERE trial_end > now() AND (sub_end IS NULL OR sub_end <= now())) trial FROM users")


# ---------- payments ----------
async def create_payment(uid, plan, amount_micro):
    return await pool.fetchrow(
        "INSERT INTO payments(user_id,plan,amount_micro) VALUES($1,$2,$3) RETURNING *", uid, plan, amount_micro)


async def pending_payments(ttl_min):
    await pool.execute(
        "UPDATE payments SET status='expired' WHERE status='pending' AND created_at < now() - make_interval(mins=>$1)",
        ttl_min)
    return await pool.fetch("SELECT * FROM payments WHERE status='pending'")


async def txid_used(txid):
    return bool(await pool.fetchval("SELECT 1 FROM payments WHERE txid=$1", txid))


async def mark_paid(pid, txid):
    return await pool.execute("UPDATE payments SET status='paid', txid=$2 WHERE id=$1 AND status='pending'", pid, txid)


async def get_payment(pid):
    return await pool.fetchrow("SELECT * FROM payments WHERE id=$1", pid)


# ---------- signals ----------
async def signals_last_24h():
    return await pool.fetchval("SELECT count(*) FROM signals WHERE created_at > now() - interval '24 hours'")


async def symbol_busy(sym, cooldown_h):
    return bool(await pool.fetchval(
        "SELECT 1 FROM signals WHERE symbol=$1 AND (status IN ('open','tp1') "
        "OR created_at > now() - make_interval(hours=>$2)) LIMIT 1", sym, cooldown_h))


async def add_signal(s):
    return await pool.fetchrow(
        "INSERT INTO signals(symbol,side,entry,sl,tp1,tp2,score) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *",
        s["symbol"], s["side"], s["entry"], s["sl"], s["tp1"], s["tp2"], s["score"])


async def open_signals():
    return await pool.fetch("SELECT * FROM signals WHERE status IN ('open','tp1') ORDER BY created_at DESC")


async def set_status(sid, status, closed=False):
    await pool.execute(
        "UPDATE signals SET status=$2, closed_at = CASE WHEN $3 THEN now() ELSE closed_at END WHERE id=$1",
        sid, status, closed)


async def stats():
    return await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE status IN ('tp2','tp1_be')) wins, "
        "count(*) FILTER (WHERE status='sl') losses, "
        "count(*) FILTER (WHERE status='expired') expired, "
        "count(*) FILTER (WHERE status IN ('open','tp1')) open FROM signals")
