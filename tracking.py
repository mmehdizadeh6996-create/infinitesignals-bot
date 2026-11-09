"""Price-accuracy helpers: live-entry refresh, entry zone, scan timing, and a
candle-based replay that decides whether a signal hit its targets or stop."""
import datetime as dt
import time

import aiohttp

OKX = "https://www.okx.com"
TIMEOUT = aiohttp.ClientTimeout(total=15)

MAX_PER_SCAN = 2      # new signals per scan, so the daily quota is spread out
MAX_RUN = 0.25        # skip if price already ran this fraction of R toward the targets
MAX_AGAINST = 0.5     # skip if price moved this fraction of R against the setup
ZONE = 0.15           # entry zone half-width, as a fraction of R
CHASE = 0.25          # "do not chase" limit, as a fraction of R


def seconds_to_next_scan(interval: int, offset: int = 60, now: float | None = None) -> float:
    """Seconds until the next wall-clock slot (epoch % interval == offset).
    With interval=900 and offset=60 scans run at :01, :16, :31, :46, i.e. right
    after each hourly candle closes instead of at an arbitrary phase."""
    now = time.time() if now is None else now
    nxt = ((now - offset) // interval + 1) * interval + offset
    return max(5.0, nxt - now)


def refresh_entry(sig: dict, live: float | None):
    """Re-anchor a signal on the live market price.

    analysis.analyze() uses the last *closed* candle (up to ~1h old) as entry.
    Returns a copy with entry/SL/TP rebuilt around the live price (same R
    distances), or None if the move since that candle makes the setup stale."""
    if not live or live <= 0:
        return None
    sign = 1 if sig["side"] == "LONG" else -1
    risk = abs(sig["entry"] - sig["sl"])
    if risk <= 0:
        return None
    move = (live - sig["entry"]) * sign  # > 0: already moved toward the targets
    if move > MAX_RUN * risk or -move > MAX_AGAINST * risk:
        return None
    out = dict(sig)
    out.update(
        entry=live,
        sl=live - sign * risk,
        tp1=live + sign * 1.5 * risk,
        tp2=live + sign * 3.0 * risk,
        ref_entry=sig["entry"],
    )
    if out["sl"] <= 0:
        return None
    return out


def entry_zone(s):
    """(low, high, chase_limit) around the entry, derived from the stored levels."""
    risk = abs(s["entry"] - s["sl"])
    lo, hi = s["entry"] - ZONE * risk, s["entry"] + ZONE * risk
    limit = s["entry"] + CHASE * risk if s["side"] == "LONG" else s["entry"] - CHASE * risk
    return lo, hi, limit


async def candles_since(session: aiohttp.ClientSession, inst: str, created_at, now=None):
    """Candles (oldest -> newest, including the still-forming one) covering the
    signal's lifetime. Finer bars for young signals, coarser for old ones."""
    now = now or dt.datetime.now(dt.timezone.utc)
    age_h = max(0.0, (now - created_at).total_seconds() / 3600)
    bar, bar_ms = ("1m", 60_000) if age_h <= 4.5 else ("5m", 300_000) if age_h <= 22 else ("15m", 900_000)
    limit = max(5, min(300, int(age_h * 3600_000 / bar_ms) + 3))
    async with session.get(
        f"{OKX}/api/v5/market/candles",
        params={"instId": inst, "bar": bar, "limit": limit},
        timeout=TIMEOUT,
    ) as r:
        j = await r.json()
    data = j.get("data")
    if not data:
        return None
    return list(reversed(data))


def simulate(g, rows) -> str:
    """Replay candles that start at/after the signal and return the outcome:
    'open' | 'tp1' | 'tp2' | 'tp1_be' | 'sl'.

    Uses candle highs/lows, so wicks between checks are not missed. When one
    candle touches both a stop and a target the stop is assumed first
    (pessimistic), so results are never flattered."""
    long = g["side"] == "LONG"
    created_ms = g["created_at"].timestamp() * 1000
    status = "open"
    for c in rows:
        if int(c[0]) < created_ms:
            continue  # candle began before the signal existed
        hi, lo = float(c[2]), float(c[3])
        sl = g["entry"] if status == "tp1" else g["sl"]
        if long:
            hit_sl, hit_tp1, hit_tp2 = lo <= sl, hi >= g["tp1"], hi >= g["tp2"]
        else:
            hit_sl, hit_tp1, hit_tp2 = hi >= sl, lo <= g["tp1"], lo <= g["tp2"]
        if status == "open":
            if hit_sl:
                return "sl"
            if hit_tp2:
                return "tp2"
            if hit_tp1:
                status = "tp1"
        else:
            if hit_sl:
                return "tp1_be"
            if hit_tp2:
                return "tp2"
    return status
