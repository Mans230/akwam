"""لوحة الأدمن: إحصائيات + إذاعة + إدارة مستخدمين + محظورون (حسب SPEC 3.9)."""
from __future__ import annotations

import asyncio
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
from .keyboards import (
    admin_kb,
    broadcast_audience_kb,
    members_kb,
    premium_duration_kb,
    sites_kb,
    user_manage_kb,
)
from .textutil import MESSAGE_LIMIT, esc as _esc, truncate_html

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id.in_(settings.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(settings.ADMIN_IDS))

BC_DELAY = 0.05
MEMBERS_PER_PAGE = 8
PENDING_PER_PAGE = 10

# فلاتر شاشة الأعضاء: قصيرة للـ 64 بايت → (فلتر db, الاسم المعروض)
_MEMBERS_FILTERS = {
    "all": ("all", "الكل"),
    "prem": ("premium", "⭐ بريميوم"),
    "ban": ("banned", "🚫 محظور"),
    "pend": ("pending", "⏳ معلق"),
}

_AUDIENCE_NAMES = {"all": "الكل", "premium": "البريميوم", "free": "المجانيين"}


class AdminStates(StatesGroup):
    broadcast = State()
    broadcast_confirm = State()
    user_search = State()
    set_limit = State()
    dm_user = State()


async def _maint_on(db: Database) -> bool:
    return (await db.get_setting("maintenance", "0")) == "1"


def _dur_label(days: int) -> str:
    if days == 0:
        return "♾ دائم"
    if days == 7:
        return "7 أيام"
    return f"{days} يوم"


def _user_card(u: dict) -> str:
    username = f"@{u['username']}" if u.get("username") else "—"
    name = u.get("first_name") or "—"
    approval = "✅ موافق عليه" if u.get("is_approved") else "⏳ معلق"
    if u.get("is_premium"):
        until = u.get("premium_until")
        premium = f"⭐ لحد {_esc(str(until))}" if until else "⭐ دائم"
    else:
        premium = "لا"
    status = "🚫 محظور" if u.get("is_banned") else "✅ نشط"
    limit = u.get("max_concurrent") if u.get("max_concurrent") is not None else "افتراضي"
    return (
        f"👤 <b>{_esc(str(name))}</b> ({_esc(username)})\n"
        f"🆔 <code>{u['id']}</code>\n"
        f"الموافقة: {approval}\n"
        f"بريميوم: {premium}\n"
        f"الحالة: {status}\n"
        f"حد التزامن: {limit}\n"
        f"عدد التحميلات: {u.get('downloads', 0)}\n"
        f"📅 انضم: {_esc(str(u.get('joined_at') or '—'))}"
    )


# ---------- الدخول للوحة ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    await message.answer(
        "🛠 <b>لوحة تحكم الأدمن</b>\nاختار من تحت 👇",
        reply_markup=admin_kb(await _maint_on(db)),
    )


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext, db: Database) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("❌ اتلغت العملية.", reply_markup=admin_kb(await _maint_on(db)))


# ---------- الإحصائيات ----------

@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery, db: Database, downloader: DownloadManager) -> None:
    s = await db.stats()
    requests_mb = s.get("requests_moviebox")
    downloads_mb = s.get("downloads_moviebox")
    if requests_mb is None:  # fallback لو db.stats لسه مفيهاش مفاتيح موفي بوكس
        requests_mb = (
            await db._scalar("SELECT COUNT(*) FROM requests WHERE site = 'moviebox'") or 0
        )
    if downloads_mb is None:
        downloads_mb = (
            await db._scalar("SELECT COUNT(*) FROM downloads WHERE site = 'moviebox'") or 0
        )
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
        f"⭐ ستار سيما — طلبات: <b>{s['requests_starcima']}</b> | تحميلات: <b>{s['downloads_starcima']}</b>\n"
        f"📦 موفي بوكس — طلبات: <b>{requests_mb}</b> | تحميلات: <b>{downloads_mb}</b>"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_kb(await _maint_on(db)))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_kb(await _maint_on(db)))
    await callback.answer()


# ---------- الإحصائيات المتقدمة ----------

_DL_STATUS_ICONS = {"done": "✅", "failed": "❌", "cancelled": "🚫"}


@router.callback_query(F.data == "adm:adv")
async def adm_adv(callback: CallbackQuery, db: Database) -> None:
    new_today = await db.count_new_users(1)
    new_week = await db.count_new_users(7)
    top = await db.top_users(10)
    titles = await db.top_titles(10)
    recent = await db.recent_downloads(20)

    lines = [
        "📈 <b>إحصائيات متقدمة</b>",
        "",
        f"🆕 <b>أعضاء جدد:</b> النهاردة: <b>{new_today}</b> — آخر 7 أيام: <b>{new_week}</b>",
        "",
        "🏆 <b>توب 10 مستخدمين تحميلًا:</b>",
    ]
    if top:
        for i, u in enumerate(top, 1):
            name = _esc(str(u.get("first_name") or "—"))
            username = f"@{u['username']}" if u.get("username") else "—"
            lines.append(f"  {i}. {name} ({_esc(username)}) — {u.get('downloads', 0)} تحميل")
    else:
        lines.append("  — لسه مفيش تحميلات")
    lines += ["", "🎬 <b>أكثر العناوين تحميلًا:</b>"]
    if titles:
        for i, (title, count) in enumerate(titles, 1):
            lines.append(f"  {i}. {_esc(str(title))} — {count}")
    else:
        lines.append("  — لسه مفيش")
    lines += ["", "🕒 <b>آخر 20 تحميل:</b>"]
    if recent:
        for d in recent:
            icon = _DL_STATUS_ICONS.get(d.get("status") or "", "❔")
            title = _esc(str(d.get("title") or "—"))
            quality = _esc(str(d.get("quality") or "—"))
            uname = _esc(str(d.get("first_name") or d.get("username") or d.get("user_id") or "—"))
            site = _SITE_NAMES.get(d.get("site") or "", d.get("site") or "—")
            lines.append(
                f"  • {icon} {title} ({quality}) — {uname} — {site} — "
                f"{_esc(str(d.get('created_at') or '—'))}"
            )
    else:
        lines.append("  — لسه مفيش")

    text = truncate_html("\n".join(lines), MESSAGE_LIMIT)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="adm:home")]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ---------- شاشة الأعضاء ----------

def _member_icon(u: dict) -> str:
    if u.get("is_banned"):
        return "🚫"
    if u.get("is_premium"):
        return "⭐"
    if not u.get("is_approved"):
        return "⏳"
    return ""


async def _render_members(callback: CallbackQuery, db: Database, flt: str, offset: int) -> None:
    db_filter, filter_name = _MEMBERS_FILTERS[flt]
    total = await db.count_users(db_filter)
    users = await db.list_users(db_filter, offset, MEMBERS_PER_PAGE)
    if not users:
        text = f"👥 <b>الأعضاء — {filter_name} ({total})</b>\n\nمفيش أعضاء هنا."
    else:
        lines = [f"👥 <b>الأعضاء — {filter_name} ({total})</b>\n"]
        for u in users:
            name = _esc(str(u.get("first_name") or "—"))
            username = f"@{u['username']}" if u.get("username") else "—"
            icon = _member_icon(u)
            suffix = f" {icon}" if icon else ""
            lines.append(f"• {name} ({_esc(username)}) — 🆔 <code>{u['id']}</code>{suffix}")
        text = truncate_html("\n".join(lines), MESSAGE_LIMIT)
    kb = members_kb(users, flt, offset, total, MEMBERS_PER_PAGE)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:members:"))
async def adm_members(callback: CallbackQuery, db: Database) -> None:
    parts = callback.data.split(":")
    flt = parts[2] if len(parts) > 2 and parts[2] in _MEMBERS_FILTERS else "all"
    try:
        offset = max(0, int(parts[3]))
    except (IndexError, ValueError):
        offset = 0
    await _render_members(callback, db, flt, offset)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:muser:"))
async def adm_member_card(callback: CallbackQuery, db: Database) -> None:
    parts = callback.data.split(":")
    uid = int(parts[2])
    flt = parts[3] if len(parts) > 3 and parts[3] in _MEMBERS_FILTERS else "all"
    try:
        offset = max(0, int(parts[4]))
    except (IndexError, ValueError):
        offset = 0
    u = await db.get_user(uid)
    text = _user_card(u) if u else f"🆔 <code>{uid}</code>\n(المستخدم مش متسجل)"
    kb = user_manage_kb(
        uid,
        u["is_banned"] if u else False,
        u.get("is_premium", False) if u else False,
        back=f"adm:members:{flt}:{offset}",
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:card:"))
async def adm_card(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    u = await db.get_user(uid)
    text = _user_card(u) if u else f"🆔 <code>{uid}</code>\n(المستخدم مش متسجل)"
    kb = user_manage_kb(
        uid,
        u["is_banned"] if u else False,
        u.get("is_premium", False) if u else False,
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ---------- تفعيل/تعطيل المواقع ----------

async def _site_on(db: Database, site: str) -> bool:
    return (await db.get_setting(f"site_{site}", "1")) == "1"


_SITE_NAMES = {"akwam": "أكوام", "starcima": "ستار سيما", "moviebox": "موفي بوكس"}


@router.callback_query(F.data == "adm:sites")
async def adm_sites(callback: CallbackQuery, db: Database) -> None:
    akwam_on = await _site_on(db, "akwam")
    starcima_on = await _site_on(db, "starcima")
    moviebox_on = await _site_on(db, "moviebox")
    text = (
        "🌐 <b>إدارة المواقع</b>\n\n"
        "الموقع المعطّل بيظهر للمستخدمين «متوقف مؤقتاً» في شاشة اختيار الموقع.\n"
        "اضغط على أي موقع للتبديل 👇"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=sites_kb(akwam_on, starcima_on, moviebox_on)
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text, reply_markup=sites_kb(akwam_on, starcima_on, moviebox_on)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:site:"))
async def adm_site_toggle(callback: CallbackQuery, db: Database) -> None:
    site = callback.data.split(":")[2]  # akwam | starcima | moviebox
    if site not in _SITE_NAMES:
        await callback.answer("⚠️ موقع غير معروف.", show_alert=True)
        return
    new_on = not await _site_on(db, site)
    await db.set_setting(f"site_{site}", "1" if new_on else "0")
    await callback.answer(f"{'✅ اتفعّل' if new_on else '🔒 اتعطّل'} {_SITE_NAMES[site]}.")
    akwam_on = await _site_on(db, "akwam")
    starcima_on = await _site_on(db, "starcima")
    moviebox_on = await _site_on(db, "moviebox")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=sites_kb(akwam_on, starcima_on, moviebox_on)
        )
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
    await message.answer(
        "👆 دي الرسالة اللي هتتبعت. اختار الجمهور 👇",
        reply_markup=broadcast_audience_kb(),
    )


@router.callback_query(F.data == "adm:bcstop", StateFilter("*"))
async def adm_broadcast_cancel(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await callback.message.edit_text(
        "❌ اتلغت الإذاعة.", reply_markup=admin_kb(await _maint_on(db))
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:bcgo"))
async def adm_broadcast_send(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    parts = callback.data.split(":")
    aud = parts[2] if len(parts) > 2 and parts[2] in _AUDIENCE_NAMES else "all"
    data = await state.get_data()
    await state.clear()
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if not chat_id or not message_id:
        await callback.answer("⚠️ الرسالة ضاعت، ابعتها تاني.", show_alert=True)
        return
    users = await db.all_user_ids(aud)
    status = await callback.message.edit_text(
        f"📢 بيبعت لـ {len(users)} مستخدم ({_AUDIENCE_NAMES[aud]})…"
    )
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
        reply_markup=admin_kb(await _maint_on(db)),
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
        await message.answer(
            truncate_html(
                f"لقيت {len(users)} مستخدمين:\n{listing}\n\nدي بيانات أول واحد 👇",
                MESSAGE_LIMIT,
            )
        )
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
async def adm_prem(callback: CallbackQuery) -> None:
    uid = int(callback.data.split(":")[2])
    try:
        await callback.message.edit_text(
            f"⭐ اختار مدة البريميوم للمستخدم <code>{uid}</code>:",
            reply_markup=premium_duration_kb(uid, "adm"),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm:premd:"))
async def adm_premd(callback: CallbackQuery, db: Database) -> None:
    parts = callback.data.split(":")
    uid, days = int(parts[2]), int(parts[3])
    await db.set_premium(uid, True, days or None)
    await callback.answer(f"⭐ اتفعّل البريميوم ({_dur_label(days)}).")
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
    await message.answer(txt, reply_markup=admin_kb(await _maint_on(db)))


# ---------- مراسلة مستخدم ✉️ ----------

@router.callback_query(F.data.startswith("adm:msg:"))
async def adm_msg_start(callback: CallbackQuery, state: FSMContext) -> None:
    uid = int(callback.data.split(":")[2])
    await state.set_state(AdminStates.dm_user)
    await state.update_data(target=uid)
    await callback.message.answer(
        f"✉️ ابعت الرسالة اللي عايز توصلها للمستخدم <code>{uid}</code>.\n(أو /cancel للإلغاء)"
    )
    await callback.answer()


@router.message(AdminStates.dm_user)
async def adm_msg_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target = data.get("target")
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ الرسالة فاضية — اتلغت.")
        return
    try:
        await message.bot.send_message(
            target, f"📩 <b>رسالة من الإدارة:</b>\n\n{_esc(text)}"
        )
    except TelegramForbiddenError:
        await message.answer("❌ مقدرتش أوصل — المستخدم حظر البوت")
        return
    except Exception:  # noqa: BLE001
        log.exception("dm to user %s failed", target)
        await message.answer("❌ حصل خطأ أثناء الإرسال.")
        return
    await message.answer(f"✅ اتبعتت الرسالة للمستخدم <code>{target}</code>.")


# ---------- وضع الصيانة ----------

@router.callback_query(F.data == "adm:maint")
async def adm_maint(callback: CallbackQuery, db: Database) -> None:
    new_on = not await _maint_on(db)
    await db.set_setting("maintenance", "1" if new_on else "0")
    await callback.answer("🔴 وضع الصيانة اتفعّل" if new_on else "🟢 وضع الصيانة اتقفل")
    try:
        await callback.message.edit_text(
            "🛠 <b>لوحة تحكم الأدمن</b>\nاختار من تحت 👇", reply_markup=admin_kb(new_on)
        )
    except TelegramBadRequest:
        pass


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
        text = truncate_html("\n".join(lines), MESSAGE_LIMIT)
    try:
        await callback.message.edit_text(text, reply_markup=admin_kb(await _maint_on(db)))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_kb(await _maint_on(db)))
    await callback.answer()


# ---------- طلبات الانضمام المعلقة ----------

@router.callback_query(F.data == "adm:home")
async def adm_home(callback: CallbackQuery, db: Database) -> None:
    try:
        await callback.message.edit_text(
            "🛠 <b>لوحة تحكم الأدمن</b>\nاختار من تحت 👇",
            reply_markup=admin_kb(await _maint_on(db)),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


async def _render_pending(callback: CallbackQuery, db: Database, offset: int) -> None:
    total = await db.count_pending()
    pending = await db.list_pending(offset, PENDING_PER_PAGE)
    if not pending:
        text = f"⏳ <b>طلبات معلقة ({total})</b>\n\nمفيش طلبات هنا 🎉" if total else "⏳ مفيش طلبات معلقة 🎉"
        buttons: list[list[InlineKeyboardButton]] = []
    else:
        lines = [f"⏳ <b>طلبات معلقة ({total})</b>\n"]
        buttons = []
        for u in pending:
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
        text = truncate_html("\n".join(lines), MESSAGE_LIMIT)
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ السابق",
                callback_data=f"adm:pend:{max(0, offset - PENDING_PER_PAGE)}",
            )
        )
    if offset + PENDING_PER_PAGE < total:
        nav.append(
            InlineKeyboardButton(text="▶️ التالي", callback_data=f"adm:pend:{offset + PENDING_PER_PAGE}")
        )
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="adm:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "adm:pending")
async def adm_pending_legacy(callback: CallbackQuery, db: Database) -> None:
    """اسم بديل قديم — يرد بنفس شاشة adm:pend:0 عشان مفيش زر قديم يكسر."""
    await _render_pending(callback, db, 0)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pend:"))
async def adm_pending(callback: CallbackQuery, db: Database) -> None:
    try:
        offset = max(0, int(callback.data.split(":")[2]))
    except (IndexError, ValueError):
        offset = 0
    await _render_pending(callback, db, offset)
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
async def acc_prem(callback: CallbackQuery) -> None:
    uid = int(callback.data.split(":")[2])
    try:
        await callback.message.edit_text(
            f"⭐ اختار مدة البريميوم للمستخدم <code>{uid}</code>:",
            reply_markup=premium_duration_kb(uid, "acc"),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("acc:premd:"))
async def acc_premd(callback: CallbackQuery, db: Database) -> None:
    parts = callback.data.split(":")
    uid, days = int(parts[2]), int(parts[3])
    dur = _dur_label(days)
    await db.set_approved(uid, True)
    await db.set_premium(uid, True, days or None)
    await _notify_user(
        callback,
        uid,
        "🎉 تمت الموافقة وحسابك بقى <b>بريميوم ⭐</b>\n\n"
        "مميزاتك:\n"
        "• ⬇️ إرسال الأفلام والحلقات مباشرة هنا على تليجرام\n"
        f"• ⚡ تحميل أسرع ({settings.PREMIUM_SEGMENTS} قطعة متوازية)\n"
        f"• ⌛ مدة الصلاحية: {dur}\n\n"
        "ابعت اسم أي فيلم أو مسلسل 👇",
    )
    await _edit_decision(callback, uid, f"⭐ موافقة + بريميوم ({dur})")
    await callback.answer("⭐ موافقة + بريميوم.")


@router.callback_query(F.data.startswith("acc:no:"))
async def acc_no(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    await db.set_ban(uid, True)
    await _notify_user(callback, uid, "❌ تم رفض طلبك من الإدارة.")
    await _edit_decision(callback, uid, "❌ رفض")
    await callback.answer("❌ اترفض واتحظر.")
