import math
import aiohttp

OKX = "https://www.okx.com"
TIMEOUT = aiohttp.ClientTimeout(total=15)


async def candles(session: aiohttp.ClientSession, inst: str, bar: str, limit: int = 300):
    async with session.get(
        f"{OKX}/api/v5/market/candles",
        params={"instId": inst, "bar": bar, "limit": limit},
        timeout=TIMEOUT,
    ) as r:
        j = await r.json()
    rows = [x for x in j.get("data", []) if x[8] == "1"]  # closed candles only
    rows.reverse()  # oldest -> newest
    return rows


async def ticker(session: aiohttp.ClientSession, inst: str):
    async with session.get(f"{OKX}/api/v5/market/ticker", params={"instId": inst}, timeout=TIMEOUT) as r:
        j = await r.json()
    data = j.get("data") or []
    return float(data[0]["last"]) if data else None


def ema(v, n):
    k = 2 / (n + 1)
    out = [v[0]]
    for x in v[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(c, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g /= n
    l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(h, l, c, n=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def macd_hist(c):
    e12, e26 = ema(c, 12), ema(c, 26)
    m = [a - b for a, b in zip(e12, e26)]
    s = ema(m, 9)
    return [a - b for a, b in zip(m, s)]


def fmt(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    dec = 4 - math.floor(math.log10(p))
    return f"{p:.{dec}f}"


def analyze(sym: str, c1, c4):
    """c1: 1H candles, c4: 4H candles. Returns a signal dict or None."""
    if len(c1) < 210 or len(c4) < 210:
        return None
    cl = [float(x[4]) for x in c1]
    hi = [float(x[2]) for x in c1]
    lo = [float(x[3]) for x in c1]
    vol = [float(x[5]) for x in c1]
    cl4 = [float(x[4]) for x in c4]

    e50, e200 = ema(cl, 50)[-1], ema(cl, 200)[-1]
    e50_4, e200_4 = ema(cl4, 50)[-1], ema(cl4, 200)[-1]
    hist = macd_hist(cl)
    r = rsi(cl)
    a = atr(hi, lo, cl)
    price = cl[-1]
    avg_vol = sum(vol[-21:-1]) / 20
    vol_ok = avg_vol > 0 and vol[-1] >= 1.5 * avg_vol

    long_s = short_s = 0
    if price > e50 > e200:
        long_s += 2
    if hist[-1] > 0 and min(hist[-3:-1]) <= 0:
        long_s += 2
    if 45 <= r <= 65:
        long_s += 1
    if cl4[-1] > e50_4 > e200_4:
        long_s += 2

    if price < e50 < e200:
        short_s += 2
    if hist[-1] < 0 and max(hist[-3:-1]) >= 0:
        short_s += 2
    if 35 <= r <= 55:
        short_s += 1
    if cl4[-1] < e50_4 < e200_4:
        short_s += 2

    if vol_ok:
        long_s += 1
        short_s += 1

    if long_s >= short_s and r < 70:
        side, score = "LONG", long_s
    elif short_s > long_s and r > 30:
        side, score = "SHORT", short_s
    else:
        return None

    risk = 1.5 * a
    if risk <= 0:
        return None
    sign = 1 if side == "LONG" else -1
    return {
        "symbol": sym,
        "side": side,
        "entry": price,
        "sl": price - sign * risk,
        "tp1": price + sign * risk * 1.5,
        "tp2": price + sign * risk * 3.0,
        "score": score,
    }
