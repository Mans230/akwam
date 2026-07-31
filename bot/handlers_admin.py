"""لوحة الأدمن: إحصائيات + إذاعة + إدارة مستخدمين + محظورون (حسب SPEC 3.9)."""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import settings
from .db import Database
from .downloader import DownloadManager
from .keyboards import admin_kb, sites_kb, user_manage_kb

log = logging.getLogger(__name__)

_esc = html.escape

router = Router(name="admin")
router.message.filter(F.from_user.id.in_(settings.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(settings.ADMIN_IDS))

BC_DELAY = 0.05


class AdminStates(StatesGroup):
    broadcast = State()
    broadcast_confirm = State()
    user_search = State()
    set_limit = State()


def _user_card(u: dict) -> str:
    username = f"@{u['username']}" if u.get("username") else "—"
    name = u.get("first_name") or "—"
    approval = "✅ موافق عليه" if u.get("is_approved") else "⏳ معلق"
    premium = "⭐ نعم" if u.get("is_premium") else "لا"
    status = "🚫 محظور" if u.get("is_banned") else "✅ نشط"
    limit = u.get("max_concurrent") if u.get("max_concurrent") is not None else "افتراضي"
    return (
        f"👤 <b>{_esc(str(name))}</b> ({_esc(username)})\n"
        f"🆔 <code>{u['id']}</code>\n"
        f"الموافقة: {approval}\n"
        f"بريميوم: {premium}\n"
        f"الحالة: {status}\n"
        f"حد التزامن: {limit}\n"
        f"عدد التحميلات: {u.get('downloads', 0)}"
    )


# ---------- الدخول للوحة ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>لوحة تحكم الأدمن</b>\nاختار من تحت 👇", reply_markup=admin_kb())


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("❌ اتلغت العملية.", reply_markup=admin_kb())


# ---------- الإحصائيات ----------

@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery, db: Database, downloader: DownloadManager) -> None:
    s = await db.stats()
    text = (
        "📊 <b>إحصائيات البوت</b>\n\n"
        f"👥 المستخدمون: <b>{s['users']}</b>\n"
        f"🚫 المحظورون: <b>{s['banned']}</b>\n"
        f"🔍 طلبات البحث: <b>{s['requests']}</b>\n"
        f"✅ تحميلات مكتملة: <b>{s['downloads_done']}</b>\n"
        f"📅 تحميلات النهاردة: <b>{s['downloads_today']}</b>\n"
        f"⭐ بريميوم: <b>{s['premium']}</b>\n"
        f"⏳ معلقون: <b>{s['pending']}</b>\n"
        f"⚡ تحميلات نشطة دلوقتي: <b>{downloader.total_active()}</b>\n\n"
        f"🌐 <b>حسب الموقع:</b>\n"
        f"🔵 أكوام — طلبات: <b>{s['requests_akwam']}</b> | تحميلات: <b>{s['downloads_akwam']}</b>\n"
        f"⭐ ستار سيما — طلبات: <b>{s['requests_starcima']}</b> | تحميلات: <b>{s['downloads_starcima']}</b>"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_kb())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_kb())
    await callback.answer()


# ---------- تفعيل/تعطيل المواقع ----------

async def _site_on(db: Database, site: str) -> bool:
    return (await db.get_setting(f"site_{site}", "1")) == "1"


@router.callback_query(F.data == "adm:sites")
async def adm_sites(callback: CallbackQuery, db: Database) -> None:
    akwam_on = await _site_on(db, "akwam")
    starcima_on = await _site_on(db, "starcima")
    text = (
        "🌐 <b>إدارة المواقع</b>\n\n"
        "الموقع المعطّل بيظهر للمستخدمين «متوقف مؤقتاً» في شاشة اختيار الموقع.\n"
        "اضغط على أي موقع للتبديل 👇"
    )
    try:
        await callback.message.edit_text(text, reply_markup=sites_kb(akwam_on, starcima_on))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=sites_kb(akwam_on, starcima_on))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:site:"))
async def adm_site_toggle(callback: CallbackQuery, db: Database) -> None:
    site = callback.data.split(":")[2]  # akwam | starcima
    if site not in ("akwam", "starcima"):
        await callback.answer("⚠️ موقع غير معروف.", show_alert=True)
        return
    new_on = not await _site_on(db, site)
    await db.set_setting(f"site_{site}", "1" if new_on else "0")
    name = "أكوام" if site == "akwam" else "ستار سيما"
    await callback.answer(f"{'✅ اتفعّل' if new_on else '🔒 اتعطّل'} {name}.")
    akwam_on = await _site_on(db, "akwam")
    starcima_on = await _site_on(db, "starcima")
    try:
        await callback.message.edit_reply_markup(reply_markup=sites_kb(akwam_on, starcima_on))
    except TelegramBadRequest:
        pass


# ---------- الإذاعة ----------

@router.callback_query(F.data == "adm:bc")
async def adm_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast)
    await callback.message.answer(
        "📢 ابعت الرسالة اللي عايز توصلها لكل المستخدمين.\n(أو /cancel للإلغاء)"
    )
    await callback.answer()


@router.message(AdminStates.broadcast)
async def adm_broadcast_preview(message: Message, state: FSMContext) -> None:
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(AdminStates.broadcast_confirm)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ إرسال للكل", callback_data="adm:bcgo"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="adm:bcstop"),
            ]
        ]
    )
    await message.answer("👆 دي الرسالة اللي هتتبعت. متأكد؟", reply_markup=kb)


@router.callback_query(F.data == "adm:bcstop", StateFilter("*"))
async def adm_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ اتلغت الإذاعة.", reply_markup=admin_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:bcgo")
async def adm_broadcast_send(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    await state.clear()
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if not chat_id or not message_id:
        await callback.answer("⚠️ الرسالة ضاعت، ابعتها تاني.", show_alert=True)
        return
    users = await db.all_user_ids()
    status = await callback.message.edit_text(f"📢 بيبعت لـ {len(users)} مستخدم…")
    ok = failed = 0
    for uid in users:
        try:
            await callback.bot.copy_message(uid, chat_id, message_id)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await callback.bot.copy_message(uid, chat_id, message_id)
                ok += 1
            except Exception:  # noqa: BLE001
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception:  # noqa: BLE001
            log.exception("broadcast failed for %s", uid)
            failed += 1
        await asyncio.sleep(BC_DELAY)
    await status.edit_text(
        f"📢 <b>الإذاعة خلصت</b>\n\n✅ نجح: <b>{ok}</b>\n❌ فشل: <b>{failed}</b>",
        reply_markup=admin_kb(),
    )
    await callback.answer()


# ---------- إدارة مستخدم ----------

@router.callback_query(F.data == "adm:user")
async def adm_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.user_search)
    await callback.message.answer(
        "👤 ابعت الـ <b>ID</b> أو الـ <b>@username</b> بتاع المستخدم.\n(أو /cancel للإلغاء)"
    )
    await callback.answer()


@router.message(AdminStates.user_search)
async def adm_user_search(message: Message, state: FSMContext, db: Database) -> None:
    users = await db.search_users(message.text or "")
    if not users:
        await message.answer("🙁 مفيش مستخدم مطابق. جرب تاني أو /cancel.")
        return
    await state.clear()
    if len(users) > 1:
        listing = "\n".join(
            f"• <code>{u['id']}</code> — {_esc(u.get('first_name') or '')} "
            f"({_esc('@' + u['username']) if u.get('username') else 'بدون يوزر'})"
            for u in users
        )
        await message.answer(f"لقيت {len(users)} مستخدمين:\n{listing}\n\nدي بيانات أول واحد 👇")
    u = users[0]
    await message.answer(
        _user_card(u),
        reply_markup=user_manage_kb(u["id"], u["is_banned"], u.get("is_premium", False)),
    )


@router.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_ban(uid, True)
    await callback.answer("🚫 اتحظر.")
    await _refresh_card(callback, db, uid)


@router.callback_query(F.data.startswith("adm:unban:"))
async def adm_unban(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_ban(uid, False)
    await callback.answer("✅ اتفك الحظر.")
    await _refresh_card(callback, db, uid)


@router.callback_query(F.data.startswith("adm:prem:"))
async def adm_prem(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_premium(uid, True)
    await callback.answer("⭐ اتفعّل البريميوم.")
    await _refresh_card(callback, db, uid)


@router.callback_query(F.data.startswith("adm:unprem:"))
async def adm_unprem(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_premium(uid, False)
    await callback.answer("✅ اتلغى البريميوم.")
    await _refresh_card(callback, db, uid)


async def _refresh_card(callback: CallbackQuery, db: Database, uid: int) -> None:
    u = await db.get_user(uid)
    text = _user_card(u) if u else f"🆔 <code>{uid}</code>"
    kb = user_manage_kb(
        uid,
        u["is_banned"] if u else False,
        u.get("is_premium", False) if u else False,
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("adm:lim:"))
async def adm_limit_start(callback: CallbackQuery, state: FSMContext) -> None:
    uid = int(callback.data.split(":")[2])
    await state.set_state(AdminStates.set_limit)
    await state.update_data(target=uid)
    await callback.message.answer(
        f"🔢 ابعت حد التحميلات المتزامنة للمستخدم <code>{uid}</code>\n"
        "(رقم صحيح — 0 = يرجع للافتراضي)\n(أو /cancel للإلغاء)"
    )
    await callback.answer()


@router.message(AdminStates.set_limit)
async def adm_limit_set(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    target = data.get("target")
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("⚠️ ابعت رقم صحيح بس (0 أو أكتر). أو /cancel.")
        return
    n = int(raw)
    await db.set_user_limit(target, None if n == 0 else n)
    await state.clear()
    if n == 0:
        txt = f"✅ المستخدم <code>{target}</code> رجع للحد الافتراضي."
    else:
        txt = f"✅ حد التزامن للمستخدم <code>{target}</code> بقى <b>{n}</b>."
    await message.answer(txt, reply_markup=admin_kb())


# ---------- المحظورون ----------

@router.callback_query(F.data == "adm:bans")
async def adm_bans(callback: CallbackQuery, db: Database) -> None:
    banned = await db.list_banned()
    if not banned:
        text = "🚫 مفيش مستخدمين محظورين 🎉"
    else:
        lines = [f"🚫 <b>المحظورون ({len(banned)})</b>\n"]
        for u in banned:
            username = f"@{u['username']}" if u.get("username") else "—"
            lines.append(f"• <code>{u['id']}</code> — {_esc(u.get('first_name') or '')} ({_esc(username)})")
        text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=admin_kb())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_kb())
    await callback.answer()


# ---------- طلبات الانضمام المعلقة ----------

@router.callback_query(F.data == "adm:home")
async def adm_home(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_text(
            "🛠 <b>لوحة تحكم الأدمن</b>\nاختار من تحت 👇", reply_markup=admin_kb()
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "adm:pending")
async def adm_pending(callback: CallbackQuery, db: Database) -> None:
    pending = await db.list_pending()
    if not pending:
        text = "⏳ مفيش طلبات معلقة 🎉"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="adm:home")]
            ]
        )
    else:
        lines = [f"⏳ <b>طلبات معلقة ({len(pending)})</b>\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        for u in pending[:10]:
            name = _esc(str(u.get("first_name") or "—"))
            username = f"@{u['username']}" if u.get("username") else "—"
            lines.append(f"• <code>{u['id']}</code> — {name} ({_esc(username)})")
            label = name if len(name) <= 12 else name[:11] + "…"
            buttons.append(
                [
                    InlineKeyboardButton(text=f"✅ {label}", callback_data=f"acc:ok:{u['id']}"),
                    InlineKeyboardButton(text="⭐", callback_data=f"acc:prem:{u['id']}"),
                    InlineKeyboardButton(text="❌", callback_data=f"acc:no:{u['id']}"),
                ]
            )
        buttons.append([InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="adm:home")])
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ---------- قرارات الموافقة (acc:*) ----------

async def _notify_user(callback: CallbackQuery, uid: int, text: str) -> None:
    try:
        await callback.bot.send_message(uid, text)
    except Exception:  # noqa: BLE001
        log.exception("notify user %s failed", uid)


async def _edit_decision(callback: CallbackQuery, uid: int, decision: str) -> None:
    base = callback.message.text or callback.message.caption or ""
    try:
        await callback.message.edit_text(
            f"{_esc(base)}\n\n<b>القرار:</b> {decision} — <code>{uid}</code>"
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("acc:ok:"))
async def acc_ok(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_approved(uid, True)
    await _notify_user(callback, uid, "✅ تمت الموافقة! ابعت اسم أي فيلم أو مسلسل 🎬")
    await _edit_decision(callback, uid, "✅ موافقة")
    await callback.answer("✅ تمت الموافقة.")


@router.callback_query(F.data.startswith("acc:prem:"))
async def acc_prem(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_approved(uid, True)
    await db.set_premium(uid, True)
    await _notify_user(
        callback,
        uid,
        "🎉 تمت الموافقة وحسابك بقى <b>بريميوم ⭐</b>\n\n"
        "مميزاتك:\n"
        "• ⬇️ إرسال الأفلام والحلقات مباشرة هنا على تليجرام\n"
        f"• ⚡ تحميل أسرع ({settings.PREMIUM_SEGMENTS} قطعة متوازية)\n\n"
        "ابعت اسم أي فيلم أو مسلسل 👇",
    )
    await _edit_decision(callback, uid, "⭐ موافقة + بريميوم")
    await callback.answer("⭐ موافقة + بريميوم.")


@router.callback_query(F.data.startswith("acc:no:"))
async def acc_no(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_ban(uid, True)
    await _notify_user(callback, uid, "❌ تم رفض طلبك من الإدارة.")
    await _edit_decision(callback, uid, "❌ رفض")
    await callback.answer("❌ اترفض واتحظر.")
