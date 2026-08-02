"""ميدل ويرز: تسجيل المستخدم + الحظر + موافقة الإدارة + الاشتراك الإجباري (حسب SPEC 3.10)."""
from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from .config import settings
from .db import Database
from .keyboards import approval_kb, force_sub_kb

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


async def is_subscribed(bot, channel: str, user_id: int) -> bool:
    """يفحص الاشتراك — عند أي خطأ في الفحص نسمح بالمرور (fail-open)."""
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in _MEMBER_STATUSES
    except TelegramBadRequest as e:
        log.warning("force-sub check failed (%s) — السماح بالمرور", e.message)
        return True
    except Exception:  # noqa: BLE001
        log.exception("force-sub check error — السماح بالمرور")
        return True


class UserTrackMiddleware(BaseMiddleware):
    """upsert_user لكل update."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                await self.db.upsert_user(user.id, user.username, user.first_name)
            except Exception:  # noqa: BLE001
                log.exception("upsert_user failed for %s", user.id)
        return await handler(event, data)


class BanMiddleware(BaseMiddleware):
    """المحظور → رسالة حظر ووقف المعالجة. الأدمن مستثنى."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id in settings.ADMIN_IDS:
            return await handler(event, data)
        try:
            banned = await self.db.is_banned(user.id)
        except Exception:  # noqa: BLE001
            log.exception("is_banned check failed for %s", user.id)
            banned = False
        if not banned:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("🚫 تم حظرك من استخدام البوت.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("🚫 تم حظرك من استخدام البوت.")
        return None


class MaintenanceMiddleware(BaseMiddleware):
    """وضع الصيانة — لما مفتاح maintenance=1 في الإعدادات، أي حد غير الأدمن يتصدى له.

    الأدمن يعدّي دايمًا، وعند أي استثناء في فحص الإعداد → fail-open (نسمح بالمرور).
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id in settings.ADMIN_IDS:
            return await handler(event, data)
        try:
            maintenance = (await self.db.get_setting("maintenance", "0")) == "1"
        except Exception:  # noqa: BLE001
            log.exception("maintenance check failed — السماح بالمرور")
            maintenance = False
        if not maintenance:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("🛠 البوت تحت الصيانة حاليًا.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("🛠 البوت تحت الصيانة حاليًا — جرّب تاني بعد شوية 🙏")
        return None


class ApprovalMiddleware(BaseMiddleware):
    """موافقة الإدارة قبل الاستخدام — شغالة بس لو REQUIRE_APPROVAL مفعّل.

    - الأدمن يمر دائماً، والمحظور (مرفوض) يتصدى له BanMiddleware قبلنا.
    - غير الموافق عليه: أول مرة → يتسجل pending + إبلاغ الأدمنز بأزرار القرار،
      وبعدها → رسالة "لسه مستني موافقة".
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._notified: set[int] = set()  # يوزرات اتبعت طلباتها للأدمن في الجلسة دي

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not settings.REQUIRE_APPROVAL:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None or user.id in settings.ADMIN_IDS:
            return await handler(event, data)
        try:
            approved = await self.db.is_approved(user.id)
        except Exception:  # noqa: BLE001
            log.exception("is_approved check failed for %s", user.id)
            approved = True  # fail-open زي باقي الميدل ويرز
        if approved:
            return await handler(event, data)
        if user.id in self._notified:
            reply = "⏳ طلبك لسه مستني موافقة الإدارة"
        else:
            self._notified.add(user.id)
            reply = "⏳ طلبك اتبعت للإدارة، هيوصلك رد قريب"
            await self._notify_admins(data["bot"], user)
        if isinstance(event, CallbackQuery):
            await event.answer(reply, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(reply)
        return None

    async def _notify_admins(self, bot, user: User) -> None:
        username = html.escape(f"@{user.username}") if user.username else "—"
        name = html.escape(user.full_name or "—")
        text = (
            "🆕 <b>طلب انضمام جديد</b>\n\n"
            f"👤 الاسم: {name}\n"
            f"🔗 اليوزرنيم: {username}\n"
            f"🆔 الآيدي: <code>{user.id}</code>"
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, reply_markup=approval_kb(user.id))
            except Exception:  # noqa: BLE001
                log.exception("approval notify failed for admin %s", admin_id)


class ForceSubMiddleware(BaseMiddleware):
    """اشتراك إجباري بالقناة (لو FORCE_CHANNEL مضبوط). الأدمن مستثنى."""

    def __init__(self, channel: str = "") -> None:
        self.channel = channel

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not self.channel:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None or user.id in settings.ADMIN_IDS:
            return await handler(event, data)
        # زر "تحققت" لازم يعدّي عشان الهاندلر يعيد الفحص
        if isinstance(event, CallbackQuery) and event.data == "checksub":
            return await handler(event, data)
        bot = data["bot"]
        if await is_subscribed(bot, self.channel, user.id):
            return await handler(event, data)
        text = (
            f"🔒 عشان تستخدم البوت لازم تشترك الأول في القناة {html.escape(self.channel)}\n\n"
            "اشترك واضغط «✅ تحققت من الاشتراك»."
        )
        if isinstance(event, CallbackQuery):
            await event.answer("🔒 اشترك في القناة الأول وبعدين جرب تاني.", show_alert=True)
            if event.message:
                try:
                    await event.message.answer(text, reply_markup=force_sub_kb(self.channel))
                except TelegramBadRequest:
                    pass
        elif isinstance(event, Message):
            await event.answer(text, reply_markup=force_sub_kb(self.channel))
        return None
