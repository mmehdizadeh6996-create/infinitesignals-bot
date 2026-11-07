import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

# USDT wallet address (TRON / TRC20 network)
WALLET = os.getenv("USDT_WALLET", "TGxbTVemMshonvKXxvKgo5yEkPQJgTjREN")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

TRIAL_DAYS = 3

# Prices in USDT (can be overridden with environment variables)
PLANS = {
    "1m": {"title": "1 Month", "days": 30, "price": int(os.getenv("PRICE_1M", "99"))},
    "6m": {"title": "6 Months", "days": 180, "price": int(os.getenv("PRICE_6M", "499"))},
    "12m": {"title": "1 Year", "days": 365, "price": int(os.getenv("PRICE_12M", "999"))},
}

MAX_DAILY_SIGNALS = int(os.getenv("MAX_DAILY_SIGNALS", "5"))  # max signals per 24h
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))  # minimum score out of 8
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "900"))  # seconds
SYMBOL_COOLDOWN_HOURS = 12
SIGNAL_MAX_AGE_HOURS = 72
PAYMENT_TTL_MIN = 60

MAJORS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOT", "LINK", "LTC",
    "TRX", "TON", "ATOM", "NEAR", "APT", "ARB", "OP", "SUI", "SEI", "INJ",
    "AAVE", "UNI", "FIL", "ICP", "ETC", "XLM", "HBAR", "RENDER", "TIA", "FET",
]
MEMES = ["DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "BOME", "TURBO", "MEME", "ORDI"]
SYMBOLS = MAJORS + MEMES
