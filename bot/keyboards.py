"""بنّايي لوحات المفاتيح Inline (حسب SPEC 3.7) — كل callback_data ≤ 64 بايت."""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from akwam.models import Episode, EpisodeDetails, MovieDetails, SearchResult

if TYPE_CHECKING:
    from starcima import ScEpisode, ScSeason, ServerLink

MAX_CB_BYTES = 64


def _check_cb(data: str) -> str:
    """تأكيد إن الـ callback_data مش بتعدّي 64 بايت."""
    if len(data.encode("utf-8")) > MAX_CB_BYTES:
        raise ValueError(f"callback_data أطول من 64 بايت: {data!r}")
    return data


def _q_token(quality: str) -> str:
    """توكين قصير للجودة جوّه الـ callback (≤ 8 حروف)."""
    return quality[:8]


def search_results_kb(
    results: list[SearchResult], cache_key: str, badge: str = "🔵"
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, r in enumerate(results):
        icon = "🎬" if r.type == "movie" else "📺"
        title = r.title if len(r.title) <= 35 else r.title[:32] + "…"
        suffix = f" ({r.year})" if r.year else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{badge}{icon} {i + 1}. {title}{suffix}",
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
                InlineKeyboardButton(text="🌐 المواقع", callback_data=_check_cb("adm:sites")),
            ],
        ]
    )


def sites_kb(akwam_on: bool, starcima_on: bool) -> InlineKeyboardMarkup:
    """تبديل تفعيل المواقع من لوحة الأدمن (cb='adm:site:{site}')."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🎬 أكوام: {'✅' if akwam_on else '🔒'}",
                    callback_data=_check_cb("adm:site:akwam"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⭐ ستار سيما: {'✅' if starcima_on else '🔒'}",
                    callback_data=_check_cb("adm:site:starcima"),
                )
            ],
            [InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data=_check_cb("adm:home"))],
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


# ---------- ستار سيما (SPEC2 قسم 4) ----------


def site_picker_kb(
    key: str, akwam_on: bool = True, starcima_on: bool = True
) -> InlineKeyboardMarkup:
    """زرا اختيار الموقع قبل البحث (cb='site:a/s:{key}') — الموقع المعطّل يظهر مقفل."""
    builder = InlineKeyboardBuilder()
    if akwam_on:
        builder.row(
            InlineKeyboardButton(text="🎬 أكوام", callback_data=_check_cb(f"site:a:{key}"))
        )
    else:
        builder.row(InlineKeyboardButton(text="🔒 أكوام متوقف مؤقتاً", callback_data=_check_cb("noop")))
    if starcima_on:
        builder.row(
            InlineKeyboardButton(text="⭐ ستار سيما", callback_data=_check_cb(f"site:s:{key}"))
        )
    else:
        builder.row(
            InlineKeyboardButton(text="🔒 ستار سيما متوقف مؤقتاً", callback_data=_check_cb("noop"))
        )
    builder.row(InlineKeyboardButton(text="❌ إلغاء", callback_data=_check_cb("close")))
    return builder.as_markup()


def try_akwam_kb(key: str) -> InlineKeyboardMarkup:
    """زر «جرّب في أكوام» لنفس الاستعلام (cb='site:a:{key}')."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎬 جرّب في أكوام", callback_data=_check_cb(f"site:a:{key}"))
    )
    builder.row(InlineKeyboardButton(text="❌ إلغاء", callback_data=_check_cb("close")))
    return builder.as_markup()


def sc_results_kb(items: list[dict], cache_key: str) -> InlineKeyboardMarkup:
    """نتايج ستار سيما — كل عنصر dict فيه 'r' (SearchResult) و'dubbed' (bool)."""
    builder = InlineKeyboardBuilder()
    for i, item in enumerate(items):
        r: SearchResult = item["r"]
        badge = "🎙" if item.get("dubbed") else "⭐"
        icon = "🎬" if r.type == "movie" else "📺"
        title = r.title if len(r.title) <= 33 else r.title[:30] + "…"
        suffix = f" ({r.year})" if r.year else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{badge}{icon} {i + 1}. {title}{suffix}",
                callback_data=_check_cb(f"r:{cache_key}:{i}"),
            )
        )
    builder.row(InlineKeyboardButton(text="❌ إلغاء البحث", callback_data=_check_cb("close")))
    return builder.as_markup()


def sc_movie_kb(tmdb_id: int, ckey: str, watch_url: str) -> InlineKeyboardMarkup:
    """أزرار فيلم ستار سيما: سيرفرات + ترجمة + صفحة المشاهدة."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📡 السيرفرات", callback_data=_check_cb(f"srv:{ckey}:1"))
    )
    builder.row(
        InlineKeyboardButton(
            text="📝 ترجمة عربية", callback_data=_check_cb(f"sub:{tmdb_id}:0:0")
        )
    )
    builder.row(InlineKeyboardButton(text="🔗 صفحة المشاهدة", url=watch_url))
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def sc_seasons_kb(tmdb_id: int, seasons: list[ScSeason]) -> InlineKeyboardMarkup:
    """أزرار مواسم مسلسل ستار سيما (cb='scseason:{tmdb}:{n}')."""
    builder = InlineKeyboardBuilder()
    for s in seasons:
        label = s.name or f"الموسم {s.number}"
        builder.row(
            InlineKeyboardButton(
                text=f"📺 {label} ({s.episode_count} حلقة)",
                callback_data=_check_cb(f"scseason:{tmdb_id}:{s.number}"),
            )
        )
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def sc_episodes_kb(
    tmdb_id: int,
    season: int,
    episodes: list[ScEpisode],
    page: int,
    per_page: int,
    total: int,
) -> InlineKeyboardMarkup:
    """حلقات موسم ستار سيما (cb='scep:{tmdb}:{s}:{e}') + تنقل (cb='sceps:{tmdb}:{s}:{page}')."""
    builder = InlineKeyboardBuilder()
    for ep in episodes:
        builder.add(
            InlineKeyboardButton(
                text=f"الحلقة {ep.number}",
                callback_data=_check_cb(f"scep:{tmdb_id}:{season}:{ep.number}"),
            )
        )
    builder.adjust(4)
    pages = max(1, (total + per_page - 1) // per_page)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="◀️ السابق", callback_data=_check_cb(f"sceps:{tmdb_id}:{season}:{page - 1}")
            )
        )
    nav.append(InlineKeyboardButton(text=f"📄 {page}/{pages}", callback_data=_check_cb("noop")))
    if page < pages:
        nav.append(
            InlineKeyboardButton(
                text="التالي ▶️", callback_data=_check_cb(f"sceps:{tmdb_id}:{season}:{page + 1}")
            )
        )
    builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()


def sc_episode_kb(
    tmdb_id: int,
    season: int,
    ep_number: int,
    ep_count: int,
    ckey: str,
    watch_url: str,
    per_page: int = 20,
) -> InlineKeyboardMarkup:
    """أزرار حلقة ستار سيما: سيرفرات + ترجمة + مشاهدة + تنقل + تحميل الموسم."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📡 السيرفرات", callback_data=_check_cb(f"srv:{ckey}:1"))
    )
    builder.row(
        InlineKeyboardButton(
            text="📝 ترجمة عربية",
            callback_data=_check_cb(f"sub:{tmdb_id}:{season}:{ep_number}"),
        )
    )
    builder.row(InlineKeyboardButton(text="🔗 صفحة المشاهدة", url=watch_url))
    nav: list[InlineKeyboardButton] = []
    if ep_number > 1:
        nav.append(
            InlineKeyboardButton(
                text="⏮ السابقة",
                callback_data=_check_cb(f"scep:{tmdb_id}:{season}:{ep_number - 1}"),
            )
        )
    if ep_number < ep_count:
        nav.append(
            InlineKeyboardButton(
                text="⏭ التالية",
                callback_data=_check_cb(f"scep:{tmdb_id}:{season}:{ep_number + 1}"),
            )
        )
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text="⬇️ تحميل الموسم كامل ⭐",
            callback_data=_check_cb(f"salls:{tmdb_id}:{season}"),
        )
    )
    back_page = (ep_number - 1) // per_page + 1
    builder.row(
        InlineKeyboardButton(
            text="🔙 الحلقات", callback_data=_check_cb(f"sceps:{tmdb_id}:{season}:{back_page}")
        )
    )
    return builder.as_markup()


def sc_servers_kb(
    ckey: str,
    servers: list[ServerLink],
    page: int,
    per_page: int,
    watch_url: str,
    qkey: str | None = None,
) -> InlineKeyboardMarkup:
    """سيرفرات ستار سيما 10/صفحة — ⚡ قابل أولاً (مرتب من الكلاينت)، akwam بجودات، الباقي روابط."""
    builder = InlineKeyboardBuilder()
    start = (page - 1) * per_page
    for i in range(start, min(start + per_page, len(servers))):
        s = servers[i]
        if s.is_akwam:
            builder.row(
                InlineKeyboardButton(
                    text=f"🎬 {s.name} — جودات أكوام",
                    callback_data=_check_cb(f"sakw:{ckey}:{i}"),
                )
            )
        elif s.downloadable:
            builder.row(
                InlineKeyboardButton(
                    text=f"⚡ {s.name} — تحميل/إرسال",
                    callback_data=_check_cb(f"sget:{ckey}:{i}"),
                )
            )
        else:
            builder.row(InlineKeyboardButton(text=f"👁 {s.name}", url=s.embed_url))
    pages = max(1, (len(servers) + per_page - 1) // per_page)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="◀️ السابق", callback_data=_check_cb(f"srv:{ckey}:{page - 1}")
            )
        )
    nav.append(InlineKeyboardButton(text=f"📄 {page}/{pages}", callback_data=_check_cb("noop")))
    if page < pages:
        nav.append(
            InlineKeyboardButton(
                text="التالي ▶️", callback_data=_check_cb(f"srv:{ckey}:{page + 1}")
            )
        )
    if pages > 1:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔗 صفحة المشاهدة الأصلية", url=watch_url))
    if qkey:
        builder.row(
            InlineKeyboardButton(
                text="🎬 جرّب في أكوام", callback_data=_check_cb(f"site:a:{qkey}")
            )
        )
    return builder.as_markup()


def sc_no_servers_kb(watch_url: str, vidking_url: str, qkey: str | None) -> InlineKeyboardMarkup:
    """لا سيرفرات شغالة — سيرفر احتياطي + جرّب في أكوام."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👁 VidKing (احتياطي)", url=vidking_url))
    builder.row(InlineKeyboardButton(text="🔗 صفحة المشاهدة الأصلية", url=watch_url))
    if qkey:
        builder.row(
            InlineKeyboardButton(
                text="🎬 جرّب في أكوام", callback_data=_check_cb(f"site:a:{qkey}")
            )
        )
    return builder.as_markup()


def sc_mp4_kb(ckey: str, index: int, direct_url: str) -> InlineKeyboardMarkup:
    """بعد استخراج mp4: إرسال (بريميوم) + رابط تحميل."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⬇️ إرسال الفيديو ⭐", callback_data=_check_cb(f"ssend:{ckey}:{index}")
        )
    )
    builder.row(InlineKeyboardButton(text="🔗 رابط التحميل", url=direct_url))
    return builder.as_markup()


def sc_hls_kb(watch_url: str) -> InlineKeyboardMarkup:
    """بعد استخراج hls: مشاهدة مباشرة فقط."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👁 مشاهدة مباشرة", url=watch_url))
    return builder.as_markup()


def sc_fail_kb(embed_url: str) -> InlineKeyboardMarkup:
    """فشل الاستخراج — متاح للمشاهدة فقط."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👁 مشاهدة على السيرفر", url=embed_url))
    return builder.as_markup()


def sc_akwam_kb(file_id: int, content_id: int, qualities: list[str]) -> InlineKeyboardMarkup:
    """جودات أكوام الحقيقية لسيرفر akwam داخل ستار سيما."""
    builder = InlineKeyboardBuilder()
    for q in qualities:
        _quality_rows(builder, file_id, content_id, q)
    builder.row(
        InlineKeyboardButton(
            text="👁 مشاهدة أونلاين", callback_data=_check_cb(f"watch:{file_id}:{content_id}")
        )
    )
    return builder.as_markup()


def sc_subs_kb(urls: list[str]) -> InlineKeyboardMarkup:
    """أزرار ملفات الترجمة SRT."""
    builder = InlineKeyboardBuilder()
    for i, url in enumerate(urls):
        builder.row(InlineKeyboardButton(text=f"📝 ترجمة {i + 1} (SRT)", url=url))
    return builder.as_markup()


def sc_dubbed_kb(
    ckey: str,
    episodes: list[ScEpisode],
    page: int,
    per_page: int,
    total: int,
    watch_url: str,
) -> InlineKeyboardMarkup:
    """حلقات المحتوى المدبلج — كل حلقة زر URL لصفحة المشاهدة الأصلية فقط."""
    builder = InlineKeyboardBuilder()
    for ep in episodes:
        builder.add(InlineKeyboardButton(text=f"🎙 الحلقة {ep.number}", url=watch_url))
    builder.adjust(4)
    pages = max(1, (total + per_page - 1) // per_page)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="◀️ السابق", callback_data=_check_cb(f"scdeps:{ckey}:{page - 1}")
            )
        )
    nav.append(InlineKeyboardButton(text=f"📄 {page}/{pages}", callback_data=_check_cb("noop")))
    if page < pages:
        nav.append(
            InlineKeyboardButton(
                text="التالي ▶️", callback_data=_check_cb(f"scdeps:{ckey}:{page + 1}")
            )
        )
    if pages > 1:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔗 صفحة المشاهدة", url=watch_url))
    builder.row(InlineKeyboardButton(text="🔙 رجوع للنتايج", callback_data=_check_cb("back")))
    return builder.as_markup()
