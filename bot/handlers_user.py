"""تدفقات المستخدم: بحث → اختيار موقع → نتايج → فيلم/مسلسل → جودات/سيرفرات/تحميل."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from akwam import AkwamClient
from akwam.client import NotFoundError
from akwam.models import DirectLink, QualityLink, SearchResult
from starcima import ScMediaDetails, ServerLink, StarcimaClient

from .cache import TTLCache
from .config import settings
from .db import Database
from .downloader import DownloadJob, DownloadManager
from .keyboards import (
    episode_kb,
    links_kb,
    movie_kb,
    sc_akwam_kb,
    sc_dubbed_kb,
    sc_episode_kb,
    sc_episodes_kb,
    sc_fail_kb,
    sc_hls_kb,
    sc_movie_kb,
    sc_mp4_kb,
    sc_no_servers_kb,
    sc_results_kb,
    sc_seasons_kb,
    sc_servers_kb,
    sc_subs_kb,
    search_results_kb,
    season_all_kb,
    seasons_kb,
    series_kb,
    site_picker_kb,
    try_akwam_kb,
)
from .middlewares import is_subscribed
from .textutil import CAPTION_LIMIT, MESSAGE_LIMIT, esc as _esc, truncate_html

log = logging.getLogger(__name__)

router = Router()

WELCOME = (
    "أهلاً بيك في <b>بوت أكوام</b> 🍿\n\n"
    "ابعتلي <b>اسم الفيلم أو المسلسل</b> اللي بتدور عليه وهجيبلك:\n"
    "🎬 الأفلام بكل الجودات (إرسال هنا أو رابط مباشر)\n"
    "📺 المسلسلات بالمواسم والحلقات + تحميل موسم كامل\n"
    "👁 روابط مشاهدة أونلاين\n\n"
    "يلا… ابعت الاسم 👇"
)


# ---------- أدوات مساعدة ----------

def _type_ar(t: str) -> str:
    if t == "movie":
        return "🎬 فيلم"
    if t == "dubbed":
        return "🎙 مدبلج"
    return "📺 مسلسل"


def _results_text(query: str, results: list[SearchResult], badge: str = "🔵") -> str:
    lines = [f"🔍 نتايج البحث عن <b>«{_esc(query)}»</b>:\n"]
    for i, r in enumerate(results):
        year = f" ({r.year})" if r.year else ""
        rating = f" ⭐ {r.rating}" if r.rating else ""
        lines.append(f"{badge}{i + 1}. {_type_ar(r.type)} <b>{_esc(r.title)}</b>{year}{rating}")
    lines.append("\nاختار من الأزرار تحت 👇")
    text = "\n".join(lines)
    return truncate_html(text, CAPTION_LIMIT)  # حد كابشن الصور بأمان


def _sc_results_text(query: str, items: list[dict]) -> str:
    """نص نتايج ستار سيما — ⭐ عادي / 🎙 مدبلج."""
    lines = [f"🔍 نتايج البحث عن <b>«{_esc(query)}»</b> في ⭐ ستار سيما:\n"]
    for i, item in enumerate(items):
        r: SearchResult = item["r"]
        badge = "🎙" if item.get("dubbed") else "⭐"
        year = f" ({r.year})" if r.year else ""
        rating = f" ⭐ {r.rating}" if r.rating else ""
        lines.append(f"{badge}{i + 1}. {_type_ar(r.type)} <b>{_esc(r.title)}</b>{year}{rating}")
    lines.append("\nاختار من الأزرار تحت 👇")
    return truncate_html("\n".join(lines), CAPTION_LIMIT)


def _first_poster(results: list[SearchResult]) -> str | None:
    for r in results:
        if r.poster:
            return r.poster
    return None


async def _respond(
    callback: CallbackQuery,
    text: str,
    kb=None,
    photo: str | None = None,
) -> None:
    """يعدّل الرسالة الحالية لو أمكن، وإلا يبعت رسالة جديدة."""
    msg = callback.message
    if msg is None:
        return
    # شبكة أمان: حد الكابشن 1024 / الرسالة 4096 — قص واعي بالوسوم بدون كسر HTML
    text = truncate_html(text, CAPTION_LIMIT if photo else MESSAGE_LIMIT)
    try:
        if photo:
            await msg.edit_media(
                InputMediaPhoto(media=photo, caption=text), reply_markup=kb
            )
        elif msg.photo:
            await msg.delete()
            await msg.answer(text, reply_markup=kb)
        else:
            await msg.edit_text(text, reply_markup=kb)
        return
    except TelegramBadRequest:
        pass
    try:
        if photo:
            await msg.answer_photo(photo, caption=text, reply_markup=kb)
        else:
            await msg.answer(text, reply_markup=kb)
    except TelegramBadRequest:
        await msg.answer(text, reply_markup=kb)


PREMIUM_ONLY_MSG = (
    "🔒 الإرسال المباشر على تليجرام للمشتركين البريميوم بس — "
    "استخدم روابط التحميل أو المشاهدة 👇"
)


async def _send_allowed(db: Database, user_id: int) -> bool:
    """الإرسال المباشر لتليجرام: بريميوم أو أدمن بس."""
    if user_id in settings.ADMIN_IDS:
        return True
    return await db.is_premium(user_id)


def _pick_quality(items, token: str):
    """يختار العنصر المطابق للجودة، وإلا أقرب جودة."""
    if not items:
        return None
    for it in items:
        if it.quality == token or it.quality.startswith(token) or token.startswith(it.quality[:8]):
            return it
    num = "".join(ch for ch in token if ch.isdigit())
    if num:
        target = int(num)

        def key(it):
            n = "".join(ch for ch in it.quality if ch.isdigit())
            return abs(int(n) - target) if n else 99999

        return min(items, key=key)
    return items[0]


async def _get_links(akwam: AkwamClient, file_id: int, content_id: int) -> list[DirectLink]:
    """روابط مباشرة من صفحة /watch مع fallback لصفحة /download."""
    try:
        links = await akwam.get_direct_links(file_id, content_id)
    except Exception:  # noqa: BLE001
        log.exception("get_direct_links failed %s/%s", file_id, content_id)
        links = []
    if links:
        return links
    try:
        link = await akwam.resolve_download(file_id, content_id)
    except Exception:  # noqa: BLE001
        log.exception("resolve_download failed %s/%s", file_id, content_id)
        link = None
    return [link] if link else []


# ---------- أدوات ستار سيما ----------

_AKWAM_WATCH_RE = re.compile(r"/watch/(\d+)/(\d+)")


async def _site_enabled(db: Database, site: str) -> bool:
    """تفعيل الموقع من إعدادات الأدمن (افتراضي مفعّل)."""
    return (await db.get_setting(f"site_{site}", "1")) == "1"


@dataclass
class _SrvCtx:
    """سياق سيرفرات ستار سيما — يتخزن في الكاش تحت srv:{ckey}."""

    tmdb: int
    type: str  # 'movie' | 'series'
    title_ar: str
    title_en: str | None
    year: int | None
    season: int | None = None
    episode: int | None = None
    abs_episode: int | None = None
    season_ep_count: int | None = None
    display: str = ""  # عنوان العرض (فيلم أو مسلسل + رقم الحلقة)
    poster: str | None = None
    qkey: str | None = None  # مفتاح الاستعلام لزر «جرّب في أكوام»


def _sc_watch_url(
    tmdb: int,
    type_: str,
    title_ar: str,
    title_en: str | None,
    season: int | None = None,
    episode: int | None = None,
) -> str:
    """رابط صفحة المشاهدة الأصلية على ستار سيما."""
    params: dict = {"type": "movie" if type_ == "movie" else "tv"}
    if title_ar:
        params["title"] = title_ar
    if title_en:
        params["en"] = title_en
    if season is not None:
        params["season"] = season
    if episode is not None:
        params["ep"] = episode
    return f"{settings.STARCIMA_DOMAIN}/watch/{tmdb}?{urlencode(params)}"


def _vidking_url(tmdb: int, type_: str, season: int | None, episode: int | None) -> str:
    """سيرفر vidking الاحتياطي الثابت (يُضاف من طبقة البوت)."""
    if type_ == "movie":
        return f"https://www.vidking.net/embed/movie/{tmdb}"
    return f"https://www.vidking.net/embed/tv/{tmdb}/{season or 1}/{episode or 1}"


def _new_srv_ctx(cache: TTLCache, ctx: _SrvCtx) -> str:
    """يخزن ServerContext بمفتاح قصير ويرجعه."""
    ckey = uuid4().hex[:8]
    cache.set(f"srv:{ckey}", ctx)
    return ckey


async def _get_sc_media(
    starcima: StarcimaClient, cache: TTLCache, tmdb: int, type_: str
) -> ScMediaDetails:
    """تفاصيل ستار سيما مع كاش (المواسم/العناوين لا تتغير)."""
    cached: ScMediaDetails | None = cache.get(f"scmedia:{type_}:{tmdb}")
    if cached is not None:
        return cached
    media = await starcima.get_media(tmdb, type_)
    cache.set(f"scmedia:{type_}:{tmdb}", media)
    return media


async def _get_sc_episodes(
    starcima: StarcimaClient, cache: TTLCache, tmdb: int, season: int
):
    cached = cache.get(f"sceps:{tmdb}:{season}")
    if cached is not None:
        return cached
    eps = await starcima.get_episodes(tmdb, season)
    cache.set(f"sceps:{tmdb}:{season}", eps)
    return eps


def _abs_episode(media: ScMediaDetails, season: int, episode: int) -> int | None:
    """رقم الحلقة المطلق (للأنمي المتواصل): مجموع حلقات المواسم السابقة + رقم الحلقة."""
    try:
        prior = sum(s.episode_count for s in media.seasons if s.number < season)
        return prior + episode if prior or season == 1 else None
    except Exception:  # noqa: BLE001
        return None


async def _get_server_list(
    starcima: StarcimaClient, cache: TTLCache, ckey: str, ctx: _SrvCtx
) -> list[ServerLink]:
    """قائمة السيرفرات مع كاش + سيرفر vidking الاحتياطي في النهاية دائماً."""
    servers: list[ServerLink] | None = cache.get(f"srvlist:{ckey}")
    if servers is None:
        try:
            servers = await starcima.get_servers(
                ctx.title_ar,
                ctx.title_en,
                ctx.year,
                ctx.type,
                season=ctx.season,
                episode=ctx.episode,
                abs_episode=ctx.abs_episode,
                season_ep_count=ctx.season_ep_count,
            )
        except Exception:  # noqa: BLE001
            log.exception("get_servers failed: %s", ctx.tmdb)
            servers = []
        servers.append(
            ServerLink(
                name="VidKing (احتياطي)",
                embed_url=_vidking_url(ctx.tmdb, ctx.type, ctx.season, ctx.episode),
                provider="other",
                downloadable=False,
                is_akwam=False,
            )
        )
        cache.set(f"srvlist:{ckey}", servers)
    return servers


# ---------- الأوامر والبحث ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@router.message(F.text, ~F.text.startswith("/"))
async def on_search(message: Message, cache: TTLCache, db: Database) -> None:
    query = (message.text or "").strip()
    if not query:
        return
    user_id = message.from_user.id
    key = uuid4().hex[:6]
    cache.set(f"q:{user_id}:{key}", query)
    akwam_on = await _site_enabled(db, "akwam")
    starcima_on = await _site_enabled(db, "starcima")
    await message.answer(
        truncate_html(f"🔍 «{_esc(query)}»\nاختار الموقع اللي أدور فيه 👇", MESSAGE_LIMIT),
        reply_markup=site_picker_kb(key, akwam_on, starcima_on),
    )


# ---------- اختيار الموقع ----------

def _picker_query(callback: CallbackQuery, cache: TTLCache) -> tuple[str, str | None]:
    key = callback.data.split(":")[2]
    return key, cache.get(f"q:{callback.from_user.id}:{key}")


@router.callback_query(F.data.startswith("site:a:"))
async def on_site_akwam(callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache, db: Database) -> None:
    key, query = _picker_query(callback, cache)
    if not query:
        await callback.answer("⌛ انتهت صلاحية البحث ده — ابعت الاسم تاني من فضلك.", show_alert=True)
        return
    if not await _site_enabled(db, "akwam"):
        await callback.answer("🔒 أكوام متوقف مؤقتاً من الإدارة.", show_alert=True)
        return
    await callback.answer("⏳ بدور في أكوام…")
    user_id = callback.from_user.id
    await db.log_request(user_id, query, site="akwam")
    try:
        results = await akwam.search(query)
    except NotFoundError:
        results = []
    except Exception:  # noqa: BLE001
        log.exception("search failed: %s", query)
        await _respond(callback, "❌ حصلت مشكلة في البحث دلوقتي، جرب تاني بعد شوية 🙏")
        return
    if not results:
        await _respond(callback, f"🙁 مفيش نتايج لـ «{_esc(query)}» في أكوام، جرب اسم تاني.")
        return
    cache.set(f"search:{user_id}:{key}", results)
    cache.set(f"lastsearch:{user_id}", key)
    await _respond(
        callback,
        _results_text(query, results, badge="🔵"),
        search_results_kb(results, key, badge="🔵"),
        photo=_first_poster(results),
    )


@router.callback_query(F.data.startswith("site:s:"))
async def on_site_starcima(
    callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache, db: Database
) -> None:
    key, query = _picker_query(callback, cache)
    if not query:
        await callback.answer("⌛ انتهت صلاحية البحث ده — ابعت الاسم تاني من فضلك.", show_alert=True)
        return
    if not await _site_enabled(db, "starcima"):
        await callback.answer("🔒 ستار سيما متوقف مؤقتاً من الإدارة.", show_alert=True)
        return
    await callback.answer("⏳ بدور في ستار سيما…")
    user_id = callback.from_user.id
    await db.log_request(user_id, query, site="starcima")
    try:
        normal, dubbed = await asyncio.gather(
            starcima.search(query), starcima.search_dubbed(query)
        )
    except NotFoundError:
        normal, dubbed = [], []
    except Exception:  # noqa: BLE001
        log.exception("starcima search failed: %s", query)
        await _respond(callback, "❌ حصلت مشكلة في البحث دلوقتي، جرب تاني بعد شوية 🙏")
        return
    items: list[dict] = [{"r": r, "site": "s", "dubbed": False} for r in normal]
    items += [{"r": r, "site": "s", "dubbed": True} for r in dubbed]
    if not items:
        await _respond(
            callback,
            f"🙁 مفيش نتايج لـ «{_esc(query)}» في ستار سيما — جرّب أكوام 👇",
            try_akwam_kb(key),
        )
        return
    cache.set(f"search:{user_id}:{key}", items)
    cache.set(f"lastsearch:{user_id}", key)
    poster = _first_poster([it["r"] for it in items])
    await _respond(callback, _sc_results_text(query, items), sc_results_kb(items, key), photo=poster)


# ---------- اختيار نتيجة ----------

@router.callback_query(F.data.startswith("r:"))
async def on_result(
    callback: CallbackQuery, akwam: AkwamClient, starcima: StarcimaClient, cache: TTLCache
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, key, idx = parts
    results: list | None = cache.get(f"search:{callback.from_user.id}:{key}")
    if not results:
        await callback.answer(
            "⌛ انتهت صلاحية النتايج دي — ابعت البحث تاني من فضلك.", show_alert=True
        )
        return
    try:
        item = results[int(idx)]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    await callback.answer("⏳ بجيب التفاصيل…")
    if isinstance(item, dict):  # نتيجة ستار سيما
        try:
            await _show_sc_result(callback, starcima, cache, item, qkey=key)
        except NotFoundError:
            await callback.answer("❌ الصفحة دي مش موجودة على ستار سيما.", show_alert=True)
        except Exception:  # noqa: BLE001
            log.exception("starcima result flow failed")
            await callback.answer("❌ حصل خطأ وأنا بجيب التفاصيل، جرب تاني.", show_alert=True)
        return
    result: SearchResult = item
    try:
        if result.type == "movie":
            await _show_movie(callback, akwam, cache, result.id)
        else:
            await _show_seasons(callback, akwam, cache, result)
    except NotFoundError:
        await callback.answer("❌ الصفحة دي مش موجودة على الموقع.", show_alert=True)
    except Exception:  # noqa: BLE001
        log.exception("result flow failed: %s", result.id)
        await callback.answer("❌ حصل خطأ وأنا بجيب التفاصيل، جرب تاني.", show_alert=True)


async def _show_movie(
    callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache, movie_id: int
) -> None:
    movie = await akwam.get_movie(movie_id)
    cache.set(f"title:{movie.id}", movie.title)
    year = f" ({movie.year})" if movie.year else ""
    rating = f"⭐ {movie.rating}\n" if movie.rating else ""
    desc = (movie.description or "").strip()
    if len(desc) > 350:
        desc = desc[:347] + "…"
    quals = ", ".join(q.quality for q in movie.qualities) or "غير متاحة"
    caption = truncate_html(
        f"🎬 <b>{_esc(movie.title)}</b>{year}\n"
        f"{rating}\n{_esc(desc)}\n\n"
        f"💾 الجودات المتاحة: <b>{_esc(quals)}</b>",
        CAPTION_LIMIT,
    )
    await _respond(callback, caption, movie_kb(movie), photo=movie.poster)


async def _show_seasons(
    callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache, result: SearchResult
) -> None:
    seasons = await akwam.search_seasons(result.title)
    seasons = [s for s in seasons if s.type == "series"] or seasons
    if len(seasons) <= 1:
        sid = seasons[0].id if seasons else result.id
        await _show_series(callback, akwam, cache, sid, page=1)
        return
    await _respond(
        callback,
        f"📺 <b>{_esc(result.title)}</b>\n\nاختار الموسم 👇",
        seasons_kb(seasons),
        photo=result.poster,
    )


# ---------- المسلسلات والحلقات ----------

async def _show_series(
    callback: CallbackQuery,
    akwam: AkwamClient,
    cache: TTLCache,
    series_id: int,
    page: int,
) -> None:
    series = await akwam.get_series(series_id)
    cache.set(f"title:{series.id}", series.title)
    cache.set(f"series:{series.id}", series.title)
    total = len(series.episodes)
    per_page = settings.EPISODES_PER_PAGE
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    chunk = series.episodes[(page - 1) * per_page : page * per_page]
    rating = f" ⭐ {series.rating}" if series.rating else ""
    text = (
        f"📺 <b>{_esc(series.title)}</b>{rating}\n"
        f"عدد الحلقات: {total} — صفحة {page}/{pages}\n\n"
        "اختار الحلقة 👇"
    )
    await _respond(
        callback,
        text,
        series_kb(series_id, chunk, page, per_page, total),
        photo=series.poster,
    )


@router.callback_query(F.data.startswith("season:"))
async def on_season(callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache) -> None:
    series_id = int(callback.data.split(":")[1])
    await callback.answer("⏳ بجيب الحلقات…")
    try:
        await _show_series(callback, akwam, cache, series_id, page=1)
    except Exception:  # noqa: BLE001
        log.exception("season flow failed: %s", series_id)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)


@router.callback_query(F.data.startswith("eps:"))
async def on_episodes_page(callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache) -> None:
    _, sid, page = callback.data.split(":")
    await callback.answer()
    try:
        await _show_series(callback, akwam, cache, int(sid), int(page))
    except Exception:  # noqa: BLE001
        log.exception("episodes page failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)


@router.callback_query(F.data.startswith("ep:"))
async def on_episode(callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache) -> None:
    ep_id = int(callback.data.split(":")[1])
    await callback.answer("⏳ بجيب الحلقة…")
    try:
        ep = await akwam.get_episode(ep_id)
    except NotFoundError:
        await callback.answer("❌ الحلقة دي مش موجودة.", show_alert=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("episode failed: %s", ep_id)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)
        return
    cache.set(f"title:{ep.id}", ep.title)
    quals = ", ".join(q.quality for q in ep.qualities) or "غير متاحة"
    number = f" — الحلقة {ep.number}" if ep.number else ""
    text = f"📺 <b>{_esc(ep.title)}</b>{number}\n\n💾 الجودات المتاحة: <b>{_esc(quals)}</b>"
    await _respond(callback, text, episode_kb(ep))


# ---------- تفاصيل ستار سيما ----------

async def _show_sc_result(
    callback: CallbackQuery,
    starcima: StarcimaClient,
    cache: TTLCache,
    item: dict,
    qkey: str,
) -> None:
    r: SearchResult = item["r"]
    if item.get("dubbed") or r.type == "dubbed":
        await _show_sc_dubbed(callback, starcima, cache, r)
    elif r.type == "movie":
        await _show_sc_movie(callback, starcima, cache, r.id, qkey)
    else:
        await _show_sc_series(callback, starcima, cache, r.id, qkey)


async def _show_sc_movie(
    callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache, tmdb: int, qkey: str
) -> None:
    media = await _get_sc_media(starcima, cache, tmdb, "movie")
    ctx = _SrvCtx(
        tmdb=tmdb,
        type="movie",
        title_ar=media.title_ar,
        title_en=media.title_en,
        year=media.year,
        display=media.title_ar,
        poster=media.poster,
        qkey=qkey,
    )
    ckey = _new_srv_ctx(cache, ctx)
    year = f" ({media.year})" if media.year else ""
    rating = f"⭐ {media.rating}\n" if media.rating else ""
    desc = (media.description or "").strip()
    if len(desc) > 350:
        desc = desc[:347] + "…"
    caption = truncate_html(
        f"🎬 <b>{_esc(media.title_ar)}</b>{year} — ⭐ ستار سيما\n"
        f"{rating}\n{_esc(desc)}\n\n"
        "اختار من تحت 👇",
        CAPTION_LIMIT,
    )
    watch_url = _sc_watch_url(tmdb, "movie", media.title_ar, media.title_en)
    await _respond(callback, caption, sc_movie_kb(tmdb, ckey, watch_url), photo=media.poster)


async def _show_sc_series(
    callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache, tmdb: int, qkey: str
) -> None:
    media = await _get_sc_media(starcima, cache, tmdb, "series")
    cache.set(f"scqkey:{tmdb}", qkey)
    if len(media.seasons) == 1:
        await _show_sc_episodes_page(callback, starcima, cache, tmdb, media.seasons[0].number, 1)
        return
    year = f" ({media.year})" if media.year else ""
    rating = f" ⭐ {media.rating}" if media.rating else ""
    text = (
        f"📺 <b>{_esc(media.title_ar)}</b>{year}{rating} — ⭐ ستار سيما\n\n"
        "اختار الموسم 👇"
    )
    await _respond(callback, text, sc_seasons_kb(tmdb, media.seasons), photo=media.poster)


@router.callback_query(F.data.startswith("scseason:"))
async def on_sc_season(callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache) -> None:
    _, tmdb, season = callback.data.split(":")
    await callback.answer("⏳ بجيب الحلقات…")
    try:
        await _show_sc_episodes_page(callback, starcima, cache, int(tmdb), int(season), 1)
    except Exception:  # noqa: BLE001
        log.exception("scseason failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)


@router.callback_query(F.data.startswith("sceps:"))
async def on_sc_episodes_page(
    callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache
) -> None:
    _, tmdb, season, page = callback.data.split(":")
    await callback.answer()
    try:
        await _show_sc_episodes_page(callback, starcima, cache, int(tmdb), int(season), int(page))
    except Exception:  # noqa: BLE001
        log.exception("sceps failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)


async def _show_sc_episodes_page(
    callback: CallbackQuery,
    starcima: StarcimaClient,
    cache: TTLCache,
    tmdb: int,
    season: int,
    page: int,
) -> None:
    media = await _get_sc_media(starcima, cache, tmdb, "series")
    eps = await _get_sc_episodes(starcima, cache, tmdb, season)
    total = len(eps)
    per_page = settings.EPISODES_PER_PAGE
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    chunk = eps[(page - 1) * per_page : page * per_page]
    text = (
        f"📺 <b>{_esc(media.title_ar)}</b> — الموسم {season} — ⭐ ستار سيما\n"
        f"عدد الحلقات: {total} — صفحة {page}/{pages}\n\n"
        "اختار الحلقة 👇"
    )
    await _respond(
        callback,
        text,
        sc_episodes_kb(tmdb, season, chunk, page, per_page, total),
        photo=media.poster,
    )


@router.callback_query(F.data.startswith("scep:"))
async def on_sc_episode(callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache) -> None:
    _, tmdb, season, ep_num = callback.data.split(":")
    tmdb_i, season_i, ep_i = int(tmdb), int(season), int(ep_num)
    await callback.answer("⏳ بجيب الحلقة…")
    try:
        media = await _get_sc_media(starcima, cache, tmdb_i, "series")
        eps = await _get_sc_episodes(starcima, cache, tmdb_i, season_i)
    except Exception:  # noqa: BLE001
        log.exception("scep failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)
        return
    ep = next((e for e in eps if e.number == ep_i), None)
    if ep is None:
        await callback.answer("❌ الحلقة دي مش موجودة.", show_alert=True)
        return
    display = f"{media.title_ar} — الموسم {season_i} الحلقة {ep_i}"
    season_ep_count = next(
        (s.episode_count for s in media.seasons if s.number == season_i), len(eps)
    )
    ctx = _SrvCtx(
        tmdb=tmdb_i,
        type="series",
        title_ar=media.title_ar,
        title_en=media.title_en,
        year=media.year,
        season=season_i,
        episode=ep_i,
        abs_episode=_abs_episode(media, season_i, ep_i),
        season_ep_count=season_ep_count,
        display=display,
        poster=media.poster,
        qkey=cache.get(f"scqkey:{tmdb_i}"),
    )
    ckey = _new_srv_ctx(cache, ctx)
    overview = (ep.overview or "").strip()
    if len(overview) > 300:
        overview = overview[:297] + "…"
    text = truncate_html(
        f"📺 <b>{_esc(display)}</b> — ⭐ ستار سيما\n"
        + (f"\n{_esc(overview)}\n" if overview else "")
        + "\nاختار من تحت 👇",
        CAPTION_LIMIT,
    )
    watch_url = _sc_watch_url(
        tmdb_i, "series", media.title_ar, media.title_en, season_i, ep_i
    )
    await _respond(
        callback,
        text,
        sc_episode_kb(
            tmdb_i, season_i, ep_i, len(eps), ckey, watch_url,
            per_page=settings.EPISODES_PER_PAGE,
        ),
        photo=ep.thumb or media.poster,
    )


# ---------- المدبلج (ستار سيما) ----------

async def _show_sc_dubbed(
    callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache, r: SearchResult
) -> None:
    series_key = str(r.id)  # المدبلج: id = arabicToonsId (حسب عقد سكرابر ستار سيما)
    ckey = uuid4().hex[:8]
    try:
        eps = await starcima.get_dubbed_episodes(series_key)
    except Exception:  # noqa: BLE001
        log.exception("get_dubbed_episodes failed: %s", series_key)
        eps = []
    cache.set(f"scdep:{ckey}", {"title": r.title, "url": r.url, "eps": eps})
    await _show_sc_dubbed_page(callback, cache, ckey, 1, poster=r.poster)


@router.callback_query(F.data.startswith("scdeps:"))
async def on_sc_dubbed_page(callback: CallbackQuery, cache: TTLCache) -> None:
    _, ckey, page = callback.data.split(":")
    await callback.answer()
    await _show_sc_dubbed_page(callback, cache, ckey, int(page))


async def _show_sc_dubbed_page(
    callback: CallbackQuery,
    cache: TTLCache,
    ckey: str,
    page: int,
    poster: str | None = None,
) -> None:
    data = cache.get(f"scdep:{ckey}")
    if not data:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    eps = data["eps"]
    total = len(eps)
    per_page = settings.EPISODES_PER_PAGE
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    chunk = eps[(page - 1) * per_page : page * per_page]
    text = f"🎙 <b>{_esc(data['title'])}</b> (مدبلج) — ⭐ ستار سيما\n"
    if total:
        text += f"عدد الحلقات: {total} — صفحة {page}/{pages}\n\n"
    else:
        text += "\n"
    text += "المشاهدة من صفحة المحتوى الأصلية 👇"
    await _respond(
        callback,
        text,
        sc_dubbed_kb(ckey, chunk, page, per_page, total, data["url"]),
        photo=poster,
    )


# ---------- سيرفرات ستار سيما ----------

@router.callback_query(F.data.startswith("srv:"))
async def on_sc_servers(callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache) -> None:
    _, ckey, page = callback.data.split(":")
    ctx: _SrvCtx | None = cache.get(f"srv:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بجيب السيرفرات…")
    servers = await _get_server_list(starcima, cache, ckey, ctx)
    watch_url = _sc_watch_url(
        ctx.tmdb, ctx.type, ctx.title_ar, ctx.title_en, ctx.season, ctx.episode
    )
    vidking = servers[-1]  # vidking دايماً الأخير
    real = servers[:-1]
    if not real:
        await _respond(
            callback,
            f"😕 مفيش سيرفرات شغالة من ستار سيما لـ <b>{_esc(ctx.display)}</b> دلوقتي.\n"
            "تقدر تجرب السيرفر الاحتياطي أو تدور في أكوام 👇",
            sc_no_servers_kb(watch_url, vidking.embed_url, ctx.qkey),
        )
        return
    per_page = settings.SERVERS_PER_PAGE
    pages = max(1, (len(servers) + per_page - 1) // per_page)
    page_i = max(1, min(int(page), pages))
    text = (
        f"📡 سيرفرات <b>{_esc(ctx.display)}</b> — صفحة {page_i}/{pages}\n\n"
        "⚡ قابل للتحميل/الإرسال — 🎬 جودات أكوام — 👁 مشاهدة فقط"
    )
    await _respond(
        callback,
        text,
        sc_servers_kb(ckey, servers, page_i, per_page, watch_url, qkey=ctx.qkey),
    )


@router.callback_query(F.data.startswith("sget:"))
async def on_sc_extract(callback: CallbackQuery, starcima: StarcimaClient, cache: TTLCache) -> None:
    _, ckey, idx = callback.data.split(":")
    ctx: _SrvCtx | None = cache.get(f"srv:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    servers = await _get_server_list(starcima, cache, ckey, ctx)
    try:
        server = servers[int(idx)]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    await callback.answer("⏳ بستخرج الرابط…")
    try:
        stream = await starcima.extract(server.embed_url)
    except Exception:  # noqa: BLE001
        log.exception("extract failed: %s", server.embed_url)
        stream = None
    if stream is None:
        await callback.message.answer(
            f"🙁 تعذر استخراج رابط مباشر من <b>{_esc(server.name)}</b> — متاح للمشاهدة فقط 👇",
            reply_markup=sc_fail_kb(server.embed_url),
        )
        return
    if stream.kind == "mp4":
        await callback.message.answer(
            f"✅ <b>{_esc(server.name)}</b> جاهز بصيغة MP4\n"
            "⏳ تنبيه: رابط التحميل صالح حوالي 24 ساعة وبعدين بيبوظ.",
            reply_markup=sc_mp4_kb(ckey, int(idx), stream.direct_url),
        )
    else:
        await callback.message.answer(
            f"👁 <b>{_esc(server.name)}</b> — السيرفر ده مشاهدة فقط (مش قابل للإرسال):",
            reply_markup=sc_hls_kb(stream.proxy_url or stream.direct_url),
        )


@router.callback_query(F.data.startswith("ssend:"))
async def on_sc_send(
    callback: CallbackQuery,
    starcima: StarcimaClient,
    cache: TTLCache,
    downloader: DownloadManager,
    db: Database,
) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    _, ckey, idx = callback.data.split(":")
    ctx: _SrvCtx | None = cache.get(f"srv:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    servers = await _get_server_list(starcima, cache, ckey, ctx)
    try:
        server = servers[int(idx)]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    await callback.answer("⏳ بجهز التحميل…")
    try:
        stream = await starcima.extract(server.embed_url)
    except Exception:  # noqa: BLE001
        log.exception("extract failed: %s", server.embed_url)
        stream = None
    if stream is None or stream.kind != "mp4":
        await callback.answer(
            "🙁 السيرفر ده مش بيدعم الإرسال المباشر — استخدم رابط المشاهدة.", show_alert=True
        )
        return
    title = f"{ctx.display} ({server.provider} ⭐ ستار سيما)"
    job = DownloadJob(
        task_id=uuid4().hex[:12],
        title=title,
        url=stream.direct_url,
        caption=(
            f"🎬 <b>{_esc(ctx.display)}</b>\n"
            f"⭐ ستار سيما — سيرفر {_esc(server.provider)}"
        ),
        thumb_url=ctx.poster,
    )
    await downloader.enqueue(callback.from_user.id, callback.message.chat.id, job)
    await db.log_download(
        callback.from_user.id, title, server.provider, "queued", site="starcima"
    )
    await callback.answer(f"✅ «{ctx.display}» اتضاف للتحميل", show_alert=True)


@router.callback_query(F.data.startswith("sakw:"))
async def on_sc_akwam(
    callback: CallbackQuery, akwam: AkwamClient, starcima: StarcimaClient, cache: TTLCache
) -> None:
    _, ckey, idx = callback.data.split(":")
    ctx: _SrvCtx | None = cache.get(f"srv:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    servers = await _get_server_list(starcima, cache, ckey, ctx)
    try:
        server = servers[int(idx)]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    m = _AKWAM_WATCH_RE.search(server.embed_url)
    if not m:
        await callback.answer("🙁 مقدرتش أقرا رابط أكوام ده.", show_alert=True)
        return
    fid, cid = int(m.group(1)), int(m.group(2))
    await callback.answer("⏳ بجيب جودات أكوام…")
    links = await _get_links(akwam, fid, cid)
    if not links:
        await callback.answer("🙁 مفيش روابط أكوام متاحة دلوقتي.", show_alert=True)
        return
    cache.set(f"title:{cid}", ctx.display)
    qualities = list(dict.fromkeys(l.quality for l in links))
    await callback.message.answer(
        f"🎬 جودات أكوام لـ <b>{_esc(ctx.display)}</b> 👇",
        reply_markup=sc_akwam_kb(fid, cid, qualities),
    )


# ---------- تحميل موسم ستار سيما ----------

def _best_direct_link(links: list[DirectLink]) -> DirectLink | None:
    """أعلى جودة رقمية من روابط أكوام المباشرة."""

    def q(link: DirectLink) -> int:
        num = "".join(ch for ch in link.quality if ch.isdigit())
        return int(num) if num else 0

    return max(links, key=q) if links else None


@router.callback_query(F.data.startswith("salls:"))
async def on_sc_season_all(
    callback: CallbackQuery,
    akwam: AkwamClient,
    starcima: StarcimaClient,
    cache: TTLCache,
    downloader: DownloadManager,
    db: Database,
) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    _, tmdb, season = callback.data.split(":")
    tmdb_i, season_i = int(tmdb), int(season)
    await callback.answer("⏳ بجهز حلقات الموسم…")
    progress = await callback.message.answer("⏳ بشوف سيرفرات الحلقات وأضيف اللي ينفع للطابور…")
    try:
        media = await _get_sc_media(starcima, cache, tmdb_i, "series")
        eps = await _get_sc_episodes(starcima, cache, tmdb_i, season_i)
    except Exception:  # noqa: BLE001
        log.exception("salls failed: %s", callback.data)
        await progress.edit_text("❌ حصل خطأ وأنا بجهز الموسم، جرب تاني.")
        return
    if not eps:
        await progress.edit_text("🙁 الموسم ده مفيهوش حلقات.")
        return
    total = len(eps)
    added = 0
    skipped: list[int] = []
    for ep in eps:
        mp4_url: str | None = None
        provider = ""
        try:
            servers = await starcima.get_servers(
                media.title_ar,
                media.title_en,
                media.year,
                "series",
                season=season_i,
                episode=ep.number,
                abs_episode=_abs_episode(media, season_i, ep.number),
                season_ep_count=total,
            )
            candidates = [s for s in servers if s.downloadable and not s.is_akwam]
            candidates += [s for s in servers if s.is_akwam]
            for cand in candidates[:3]:
                if cand.is_akwam:
                    m = _AKWAM_WATCH_RE.search(cand.embed_url)
                    if not m:
                        continue
                    links = await _get_links(akwam, int(m.group(1)), int(m.group(2)))
                    best = _best_direct_link(links)
                    if best is not None:
                        mp4_url, provider = best.url, f"akwam {best.quality}"
                        break
                else:
                    stream = await starcima.extract(cand.embed_url)
                    if stream is not None and stream.kind == "mp4":
                        mp4_url, provider = stream.direct_url, cand.provider
                        break
        except Exception:  # noqa: BLE001
            log.exception("salls episode failed: %s/%s", season_i, ep.number)
        if mp4_url is None:
            skipped.append(ep.number)
            continue
        display = f"{media.title_ar} — الموسم {season_i} الحلقة {ep.number}"
        title = f"{display} ({provider} ⭐ ستار سيما)"
        job = DownloadJob(
            task_id=uuid4().hex[:12],
            title=title,
            url=mp4_url,
            caption=(
                f"📺 <b>{_esc(display)}</b>\n"
                f"⭐ ستار سيما — سيرفر {_esc(provider)}"
            ),
            thumb_url=ep.thumb or media.poster,
        )
        await downloader.enqueue(callback.from_user.id, callback.message.chat.id, job)
        await db.log_download(
            callback.from_user.id, title, provider, "queued", site="starcima"
        )
        added += 1
    text = (
        f"✅ اتضاف <b>{added}</b> من أصل <b>{total}</b> حلقة لطابور التحميل\n"
        "الحلقات هتتبعتلك ورا بعض واحدة واحدة 📥"
    )
    if skipped:
        nums = "، ".join(str(n) for n in skipped)
        text += f"\n\n⚠️ الحلقات دي ماتضافتش (مفيش سيرفر MP4 شغال): {nums}"
    await progress.edit_text(text)


# ---------- ترجمة عربية (ستار سيما) ----------

@router.callback_query(F.data.startswith("sub:"))
async def on_sc_subs(callback: CallbackQuery, starcima: StarcimaClient) -> None:
    _, tmdb, season, episode = callback.data.split(":")
    await callback.answer("⏳ بجيب الترجمة…")
    try:
        subs = await starcima.get_subtitles(
            int(tmdb),
            season=int(season) or None,
            episode=int(episode) or None,
        )
    except Exception:  # noqa: BLE001
        log.exception("get_subtitles failed: %s", callback.data)
        subs = []
    if not subs:
        await callback.answer("🙁 لا توجد ترجمة متاحة دلوقتي.", show_alert=True)
        return
    await callback.message.answer(
        "📝 الترجمة العربية (SRT) 👇", reply_markup=sc_subs_kb(subs)
    )


# ---------- روابط التحميل والمشاهدة ----------

@router.callback_query(F.data.startswith("link:"))
async def on_link(callback: CallbackQuery, akwam: AkwamClient) -> None:
    _, fid, cid, q = callback.data.split(":", 3)
    await callback.answer("⏳ بجهز الرابط…")
    links = await _get_links(akwam, int(fid), int(cid))
    if not links:
        await callback.answer("🙁 مفيش روابط متاحة دلوقتي، جرب بعد شوية.", show_alert=True)
        return
    link = _pick_quality(links, q)
    kb = links_kb([(f"⬇️ تحميل {link.quality}", link.url)])
    await callback.message.answer(
        f"🔗 رابط التحميل المباشر جاهز بجودة <b>{_esc(link.quality)}</b>\n"
        "⏳ تنبيه: الرابط صالح حوالي 24 ساعة وبعدين بيبوظ.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("watch:"))
async def on_watch(callback: CallbackQuery, akwam: AkwamClient) -> None:
    _, fid, cid = callback.data.split(":")
    await callback.answer("⏳ بجهز روابط المشاهدة…")
    watch_page = f"{settings.AKWAM_DOMAIN}/watch/{fid}/{cid}"
    links = await _get_links(akwam, int(fid), int(cid))
    urls: list[tuple[str, str]] = [("👁 صفحة المشاهدة على أكوام", watch_page)]
    urls += [(f"▶️ مشاهدة {l.quality}", l.url) for l in links]
    note = "\n⏳ الروابط المباشرة صالحة حوالي 24 ساعة." if links else ""
    await callback.message.answer(
        f"👁 روابط المشاهدة جاهزة:{note}",
        reply_markup=links_kb(urls),
    )


# ---------- إرسال الفيديو ----------

@router.callback_query(F.data.startswith("send:"))
async def on_send(
    callback: CallbackQuery,
    akwam: AkwamClient,
    cache: TTLCache,
    downloader: DownloadManager,
    db: Database,
) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    _, fid, cid, q = callback.data.split(":", 3)
    await callback.answer("⏳ بجهز التحميل…")
    links = await _get_links(akwam, int(fid), int(cid))
    if not links:
        await callback.answer("🙁 مفيش روابط تحميل متاحة دلوقتي.", show_alert=True)
        return
    link = _pick_quality(links, q)
    base = cache.get(f"title:{cid}") or f"محتوى {cid}"
    title = f"{base} ({link.quality})"
    job = DownloadJob(
        task_id=uuid4().hex[:12],
        title=title,
        url=link.url,
        caption=f"🎬 <b>{_esc(base)}</b>\n💾 الجودة: {_esc(link.quality)}",
    )
    await downloader.enqueue(callback.from_user.id, callback.message.chat.id, job)
    await callback.answer(f"✅ «{title}» اتضاف للتحميل", show_alert=True)


# ---------- تحميل الموسم كامل ----------

@router.callback_query(F.data.startswith("sall:"))
async def on_season_all(callback: CallbackQuery, akwam: AkwamClient, db: Database) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    series_id = int(callback.data.split(":")[1])
    await callback.answer("⏳ بشوف الجودات المتاحة…")
    try:
        series = await akwam.get_series(series_id)
        if not series.episodes:
            await callback.answer("🙁 المسلسل ده مفيهوش حلقات.", show_alert=True)
            return
        first = await akwam.get_episode(series.episodes[0].id)
    except Exception:  # noqa: BLE001
        log.exception("sall failed: %s", series_id)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)
        return
    if not first.qualities:
        await callback.answer("🙁 مفيش جودات متاحة للحلقات.", show_alert=True)
        return
    kb = season_all_kb(series_id, [ql.quality for ql in first.qualities])
    await callback.message.answer(
        f"📦 <b>{_esc(series.title)}</b> — {len(series.episodes)} حلقة\n"
        "اختار الجودة اللي عايز تحمّل بيها الموسم كله 👇",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("sallq:"))
async def on_season_all_quality(
    callback: CallbackQuery,
    akwam: AkwamClient,
    downloader: DownloadManager,
    db: Database,
) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    _, sid, q = callback.data.split(":", 2)
    series_id = int(sid)
    await callback.answer("⏳ بجهز حلقات الموسم…")
    progress = await callback.message.answer("⏳ بجهز الحلقات وأضيفها للطابور…")

    async def _enqueue_all() -> tuple[int, int]:
        series = await akwam.get_series(series_id)
        total = len(series.episodes)
        added = 0
        for ep in series.episodes:
            try:
                epd = await akwam.get_episode(ep.id)
                ql: QualityLink | None = _pick_quality(epd.qualities, q)
                if ql is None:
                    continue
                links = await _get_links(akwam, ql.file_id, ql.content_id)
                link = _pick_quality(links, q)
                if link is None:
                    continue
                title = f"{series.title} - الحلقة {ep.number} ({link.quality})"
                job = DownloadJob(
                    task_id=uuid4().hex[:12],
                    title=title,
                    url=link.url,
                    caption=(
                        f"📺 <b>{_esc(series.title)}</b> — الحلقة {ep.number}\n"
                        f"💾 الجودة: {_esc(link.quality)}"
                    ),
                    thumb_url=ep.thumb,
                )
                await downloader.enqueue(callback.from_user.id, callback.message.chat.id, job)
                added += 1
            except Exception:  # noqa: BLE001
                log.exception("sallq episode failed: %s", ep.id)
        return added, total

    try:
        added, total = await _enqueue_all()
        await progress.edit_text(
            f"✅ اتضاف <b>{added}</b> من أصل <b>{total}</b> حلقة لطابور التحميل بجودة <b>{_esc(q)}</b>\n"
            "الحلقات هتتبعتلك ورا بعض واحدة واحدة 📥"
        )
    except Exception:  # noqa: BLE001
        log.exception("sallq failed: %s", callback.data)
        await progress.edit_text("❌ حصل خطأ وأنا بجهز الموسم، جرب تاني.")


# ---------- إلغاء / رجوع / متفرقات ----------

@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: CallbackQuery, downloader: DownloadManager) -> None:
    task_id = callback.data.split(":", 1)[1]
    ok = await downloader.cancel(task_id, callback.from_user.id)
    if ok:
        await callback.answer("✅ اتلغى التحميل.")
    else:
        await callback.answer("⚠️ المهمة دي خلصت أو مش موجودة.", show_alert=True)


@router.callback_query(F.data == "back")
async def on_back(callback: CallbackQuery, cache: TTLCache) -> None:
    user_id = callback.from_user.id
    key = cache.get(f"lastsearch:{user_id}")
    results = cache.get(f"search:{user_id}:{key}") if key else None
    if not results:
        await callback.answer("⌛ مفيش نتايج محفوظة — ابعت بحث جديد.", show_alert=True)
        return
    await callback.answer()
    if isinstance(results[0], dict):  # نتايج ستار سيما
        await _respond(
            callback,
            _sc_results_text("آخر بحث", results),
            sc_results_kb(results, key),
            photo=_first_poster([it["r"] for it in results]),
        )
        return
    await _respond(
        callback,
        _results_text("آخر بحث", results),
        search_results_kb(results, key),
        photo=_first_poster(results),
    )


@router.callback_query(F.data == "close")
async def on_close(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "checksub")
async def on_checksub(callback: CallbackQuery) -> None:
    channel = settings.FORCE_CHANNEL
    if not channel:
        await callback.answer("✅ تمام!")
        return
    if await is_subscribed(callback.bot, channel, callback.from_user.id):
        await callback.answer("✅ تم التحقق!")
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        await callback.message.answer("✅ جميل! دلوقتي ابعت اسم الفيلم أو المسلسل 👇")
    else:
        await callback.answer(
            f"❌ لسه مشتركتش في القناة {_esc(channel)} — اشترك الأول وبعدين اضغط تحققت.",
            show_alert=True,
        )
