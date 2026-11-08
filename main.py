import asyncio
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _clean_dsn(url: str) -> str:
    """Neon URLs include channel_binding=..., which asyncpg does not understand."""
    if not url:
        return url
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(q)))


os.environ["DATABASE_URL"] = _clean_dsn(os.environ.get("DATABASE_URL", ""))

from aiohttp import web  # noqa: E402

import bot  # noqa: E402
import dashboard  # noqa: E402

bot.dp.include_router(dashboard.router)  # admin-only /dashboard, /users, /user


async def health(_request):
    return web.Response(text="ok")


async def run():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    await bot.main()


if __name__ == "__main__":
    asyncio.run(run())
