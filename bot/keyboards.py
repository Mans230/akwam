"""بنّايي لوحات المفاتيح Inline (حسب SPEC 3.7) — كل callback_data ≤ 64 بايت."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from akwam.models import Episode, EpisodeDetails, MovieDetails, SearchResult

MAX_CB_BYTES = 64


def _check_cb(data: str) -> str:
    """تأكيد إن الـ callback_data مش بتعدّي 64 بايت."""
    if len(data.encode("utf-8")) > MAX_CB_BYTES:
        raise ValueError(f"callback_data أطول من 64 بايت: {data!r}")
    return data


def _q_token(quality: str) -> str:
    """توكين قصير للجودة جوّه الـ callback (≤ 8 حروف)."""
    return quality[:8]


def search_results_kb(results: list[SearchResult], cache_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, r in enumerate(results):
        icon = "🎬" if r.type == "movie" else "📺"
        title = r.title if len(r.title) <= 35 else r.title[:32] + "…"
        suffix = f" ({r.year})" if r.year else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {i + 1}. {title}{suffix}",
                callback_data=_check_cb(f"r:{cache_key}:{i}"),
            )
        )
    builder.row(InlineKeyboardButton(text="❌ إلغاء البحث", callback_data=_check_cb("close")))
    return builder.as_markup()


def _quality_rows(builder: InlineKeyboardBuilder, file_id: int, content_id: int, quality: str) -> None:
    q = _q_token(quality)
    builder.row(
        InlineKeyboardButton(
            text=f"⬇️ إرسال {quality} ⭐",
            callback_data=_check_cb(f"send:{file_id}:{content_id}:{q}"),
        ),
        InlineKeyboardButton(
            text=f"🔗 رابط {quality}",
            callback_data=_check_cb(f"link:{file_id}:{content_id}:{q}"),
        ),
    )


def movie_kb(movie: MovieDetails) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ql in movie.qualities:
        _quality_rows(builder, ql.file_id, ql.content_id, ql.quality)
    if movie.qualities:
        first = movie.qualities[0]
        builder.row(
            InlineKeyboardButton(
                text="👁 مشاهدة أونلاين",
                callback_data=_check_cb(f"watch:{first.file_id}:{first.content_id}"),
            )
        )
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def seasons_kb(seasons: list[SearchResult]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in seasons:
        title = s.title if len(s.title) <= 40 else s.title[:37] + "…"
        builder.row(
            InlineKeyboardButton(text=f"📺 {title}", callback_data=_check_cb(f"season:{s.id}"))
        )
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def series_kb(
    series_id: int,
    episodes: list[Episode],
    page: int,
    per_page: int,
    total: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ep in episodes:
        label = f"الحلقة {ep.number}" if ep.number else f"#{ep.id}"
        builder.add(InlineKeyboardButton(text=label, callback_data=_check_cb(f"ep:{ep.id}")))
    builder.adjust(4)

    pages = max(1, (total + per_page - 1) // per_page)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(text="◀️ السابق", callback_data=_check_cb(f"eps:{series_id}:{page - 1}"))
        )
    nav.append(InlineKeyboardButton(text=f"📄 {page}/{pages}", callback_data=_check_cb("noop")))
    if page < pages:
        nav.append(
            InlineKeyboardButton(text="التالي ▶️", callback_data=_check_cb(f"eps:{series_id}:{page + 1}"))
        )
    builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text="⬇️ تحميل الموسم كامل ⭐", callback_data=_check_cb(f"sall:{series_id}")
        )
    )
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def episode_kb(ep: EpisodeDetails) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ql in ep.qualities:
        _quality_rows(builder, ql.file_id, ql.content_id, ql.quality)
    if ep.qualities:
        first = ep.qualities[0]
        builder.row(
            InlineKeyboardButton(
                text="👁 مشاهدة أونلاين",
                callback_data=_check_cb(f"watch:{first.file_id}:{first.content_id}"),
            )
        )
    nav: list[InlineKeyboardButton] = []
    if ep.prev_episode_id:
        nav.append(
            InlineKeyboardButton(text="⏮ السابقة", callback_data=_check_cb(f"ep:{ep.prev_episode_id}"))
        )
    if ep.next_episode_id:
        nav.append(
            InlineKeyboardButton(text="⏭ الحلقة التالية", callback_data=_check_cb(f"ep:{ep.next_episode_id}"))
        )
    if nav:
        builder.row(*nav)
    if ep.series_id:
        builder.row(
            InlineKeyboardButton(text="🔙 الحلقات", callback_data=_check_cb(f"eps:{ep.series_id}:1"))
        )
    else:
        builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def season_all_kb(series_id: int, qualities: list[str]) -> InlineKeyboardMarkup:
    """أزرار اختيار جودة تحميل الموسم كامل cb='sallq:{series_id}:{q}'."""
    builder = InlineKeyboardBuilder()
    for q in qualities:
        builder.row(
            InlineKeyboardButton(
                text=f"⬇️ الموسم كامل {q} ⭐",
                callback_data=_check_cb(f"sallq:{series_id}:{_q_token(q)}"),
            )
        )
    return builder.as_markup()


def links_kb(urls: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, url in urls:
        builder.row(InlineKeyboardButton(text=label, url=url))
    return builder.as_markup()


def cancel_kb(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء التحميل", callback_data=_check_cb(f"cancel:{task_id}"))]
        ]
    )


def force_sub_kb(channel: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if channel.startswith("@"):
        builder.row(InlineKeyboardButton(text="📢 انضم للقناة", url=f"https://t.me/{channel[1:]}"))
    builder.row(InlineKeyboardButton(text="✅ تحققت من الاشتراك", callback_data=_check_cb("checksub")))
    return builder.as_markup()


def approval_kb(user_id: int) -> InlineKeyboardMarkup:
    """أزرار قرار الأدمن على طلب انضمام (cb='acc:ok/prem/no:{uid}')."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ موافقة", callback_data=_check_cb(f"acc:ok:{user_id}"))],
            [
                InlineKeyboardButton(
                    text="⭐ موافقة + بريميوم", callback_data=_check_cb(f"acc:prem:{user_id}")
                )
            ],
            [InlineKeyboardButton(text="❌ رفض", callback_data=_check_cb(f"acc:no:{user_id}"))],
        ]
    )


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 إحصائيات", callback_data=_check_cb("adm:stats")),
                InlineKeyboardButton(text="📢 إذاعة", callback_data=_check_cb("adm:bc")),
            ],
            [
                InlineKeyboardButton(text="👤 إدارة مستخدم", callback_data=_check_cb("adm:user")),
                InlineKeyboardButton(text="🚫 محظورون", callback_data=_check_cb("adm:bans")),
            ],
            [
                InlineKeyboardButton(text="⏳ طلبات معلقة", callback_data=_check_cb("adm:pending")),
            ],
        ]
    )


def user_manage_kb(
    user_id: int, is_banned: bool, is_premium: bool = False
) -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton(text="✅ فك الحظر", callback_data=_check_cb(f"adm:unban:{user_id}"))
        if is_banned
        else InlineKeyboardButton(text="🚫 حظر", callback_data=_check_cb(f"adm:ban:{user_id}"))
    )
    prem_btn = (
        InlineKeyboardButton(text="⭐ إلغاء بريميوم", callback_data=_check_cb(f"adm:unprem:{user_id}"))
        if is_premium
        else InlineKeyboardButton(text="⭐ تفعيل بريميوم", callback_data=_check_cb(f"adm:prem:{user_id}"))
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ban_btn],
            [prem_btn],
            [InlineKeyboardButton(text="🔢 تعيين حد التزامن", callback_data=_check_cb(f"adm:lim:{user_id}"))],
        ]
    )
