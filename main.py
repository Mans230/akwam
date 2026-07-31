"""نقطة تشغيل بوت أكوام (حسب SPEC 3.11)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from akwam import AkwamClient

from bot.cache import TTLCache
from bot.config import settings
from bot.db import Database
from bot.downloader import DownloadManager
from bot.handlers_admin import router as admin_router
from bot.handlers_user import router as user_router
from bot.middlewares import BanMiddleware, ForceSubMiddleware, UserTrackMiddleware

log = logging.getLogger("akwam-bot")


def _build_bot() -> Bot:
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    if settings.BOT_API_SERVER:
        # Bot API Local Server — رفع ملفات لحد 2GB
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.BOT_API_SERVER))
        log.info("BOT_API_SERVER مفعّل: %s", settings.BOT_API_SERVER)
        return Bot(settings.BOT_TOKEN, session=session, default=default)
    return Bot(settings.BOT_TOKEN, default=default)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db = Database(settings.DB_PATH)
    await db.init()
    log.info("قاعدة البيانات جاهزة: %s", settings.DB_PATH)

    akwam = AkwamClient(base_url=settings.AKWAM_DOMAIN)
    cache = TTLCache(ttl_seconds=settings.CACHE_TTL_HOURS * 3600)
    bot = _build_bot()
    downloader = DownloadManager(bot, db, settings.DOWNLOAD_DIR, settings.DEFAULT_MAX_CONCURRENT)

    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["akwam"] = akwam
    dp["cache"] = cache
    dp["downloader"] = downloader

    # ترتيب الميدل وير: تسجيل → حظر → اشتراك إجباري
    dp.message.outer_middleware(UserTrackMiddleware(db))
    dp.callback_query.outer_middleware(UserTrackMiddleware(db))
    dp.message.outer_middleware(BanMiddleware(db))
    dp.callback_query.outer_middleware(BanMiddleware(db))
    dp.message.outer_middleware(ForceSubMiddleware(settings.FORCE_CHANNEL))
    dp.callback_query.outer_middleware(ForceSubMiddleware(settings.FORCE_CHANNEL))

    dp.include_router(admin_router)  # الأول عشان حالات FSM بتاعته تلتقط رسايله
    dp.include_router(user_router)

    me = await bot.get_me()
    log.info("البوت اشتغل ✅ @%s (id=%s)", me.username, me.id)
    log.info("دومين أكوام: %s — أدمن: %s", settings.AKWAM_DOMAIN, settings.ADMIN_IDS)
    if settings.FORCE_CHANNEL:
        log.info("اشتراك إجباري على: %s", settings.FORCE_CHANNEL)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("بيقفل… إلغاء التحميلات وقفل الاتصالات")
        await downloader.shutdown()
        await akwam.close()
        await db.close()
        await bot.session.close()
        log.info("اتقفل نضيف 👋")


if __name__ == "__main__":
    asyncio.run(main())
