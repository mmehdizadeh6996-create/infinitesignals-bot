# InfiniteSignals Bot

Telegram crypto signals bot (major coins + meme coins) with a live tracker, subscriptions and USDT (TRC20) payments.

## Plans
| Plan | Price |
|---|---|
| 1 Month | 99 USDT |
| 6 Months | 499 USDT |
| 1 Year | 999 USDT |

New users get a 3-day free trial.

## Hosting (free tier)
- Runs as a **Render free Web Service** (`python main.py`). A tiny HTTP server answers `/health` so an uptime monitor (e.g. UptimeRobot, every 5 min) can keep the service awake.
- Database: external **Neon Postgres** via `DATABASE_URL` (Render's free Postgres expires after 30 days).

## Environment variables
| Name | Description | Default |
|---|---|---|
| BOT_TOKEN | Telegram bot token (from @BotFather) | - |
| DATABASE_URL | Neon Postgres connection string | - |
| ADMIN_IDS | Comma-separated admin Telegram IDs | - |
| USDT_WALLET | TRC20 receiving address | configured |
| TRONGRID_API_KEY | TronGrid API key (recommended) | - |
| PRICE_1M / PRICE_6M / PRICE_12M | Plan prices (USDT) | 99 / 499 / 999 |
| MAX_DAILY_SIGNALS | Max signals per 24h | 5 |
| MIN_SCORE | Minimum signal score (of 8) | 6 |

## Admin commands
`/admin` stats, `/grant <user_id> <days>` manual activation, `/broadcast <text>` message all users

## How payments work
Each purchase gets a unique amount (e.g. 99.37 USDT). The bot polls incoming TRC20 transfers to the wallet every 45 seconds via TronGrid, matches the amount, and activates the subscription automatically.
