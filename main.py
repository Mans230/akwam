"""نقطة تشغيل البوت: تهيئة المكونات + بدء polling."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from akwam import AkwamClient
from moviebox import MovieboxClient
from starcima import StarcimaClient

from bot.cache import TTLCache
from bot.config import settings
from bot.db import Database
from bot.downloader import DownloadManager
from bot.handlers_admin import router as admin_router
from bot.handlers_user import router as user_router
from bot.middlewares import ForceSubscribeMiddleware

log = logging.getLogger("akwam-bot")


def _build_bot() -> Bot:
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    if settings.BOT_API_SERVER:
        # سيرفر Bot API محلي (رفع حد حجم الملفات للإرسال)
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.BOT_API_SERVER))
        log.info("BOT_API_SERVER مفعّل: %s", settings.BOT_API_SERVER)
        return Bot(settings.BOT_TOKEN, session=session, default=default)
    return Bot(settings.BOT_TOKEN, default=default)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx بيلوّث اللوج برسالة INFO لكل طلب (آلاف السجمنتات في تحميل DASH)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = Database(settings.DB_PATH)
    await db.init()
    log.info("قاعدة البيانات جاهزة: %s", settings.DB_PATH)

    akwam = AkwamClient(base_url=settings.AKWAM_DOMAIN)
    starcima = StarcimaClient(base_url=settings.STARCIMA_DOMAIN)
    moviebox = MovieboxClient(base_url=settings.MOVIEBOX_DOMAIN)
    cache = TTLCache(ttl_seconds=settings.CACHE_TTL_HOURS * 3600)
    bot = _build_bot()
    downloader = DownloadManager(bot, db, settings.DOWNLOAD_DIR, settings.DEFAULT_MAX_CONCURRENT)
    await downloader.start()

    dp = Dispatcher(
        akwam=akwam,
        starcima=starcima,
        moviebox=moviebox,
        cache=cache,
        downloader=downloader,
        db=db,
    )
    dp.message.middleware(ForceSubscribeMiddleware())
    dp.callback_query.middleware(ForceSubscribeMiddleware())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("البوت بدأ — 3 مواقع: أكوام + ستار سيما + موفي بوكس")
    try:
        await dp.start_polling(bot)
    finally:
        await downloader.stop()
        await akwam.close()
        await starcima.close()
        await moviebox.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("البوت اتقفل.")

