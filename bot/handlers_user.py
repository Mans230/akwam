"""تدفقات المستخدم: بحث → اختيار موقع → نتايج → فيلم/مسلسل → جودات/سيرفرات/تحميل."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaPhoto,
    Message,
)

from akwam import AkwamClient
from akwam.client import NotFoundError
from akwam.models import DirectLink, QualityLink, SearchResult
from moviebox import VIDEO_REFERER, MbDetails, MovieboxClient
from starcima import ScMediaDetails, ServerLink, StarcimaClient

from .cache import TTLCache
from .config import settings
from .db import Database
from .downloader import DownloadJob, DownloadManager
from .keyboards import (
    episode_kb,
    links_kb,
    mb_details_kb,
    mb_dubs_kb,
    mb_episodes_kb,
    mb_langs_kb,
    mb_link_kb,
    mb_results_kb,
    mb_seasons_kb,
    mb_streams_kb,
    mb_trending_kb,
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
    moviebox_on = await _site_enabled(db, "moviebox")
    await message.answer(
        truncate_html(f"🔍 «{_esc(query)}»\nاختار الموقع اللي أدور فيه 👇", MESSAGE_LIMIT),
        reply_markup=site_picker_kb(key, akwam_on, starcima_on, moviebox_on),
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


# ---------- موفي بوكس: بحث + رائج ----------

def _mb_results_text(query: str, items: list[dict]) -> str:
    """نص نتايج موفي بوكس — 📦 بجانب كل نتيجة."""
    lines = [f"🔍 نتايج البحث عن <b>«{_esc(query)}»</b> في 📦 موفي بوكس:\n"]
    for i, item in enumerate(items):
        r: SearchResult = item["r"]
        year = f" ({r.year})" if r.year else ""
        rating = f" ⭐ {r.rating}" if r.rating else ""
        lines.append(f"📦{i + 1}. {_type_ar(r.type)} <b>{_esc(r.title)}</b>{year}{rating}")
    lines.append("\nاختار من الأزرار تحت 👇")
    return truncate_html("\n".join(lines), CAPTION_LIMIT)


def _mb_trending_text(items: list[dict], page: int) -> str:
    lines = [f"🔥 الرائج دلوقتي في 📦 موفي بوكس — صفحة {page}:\n"]
    for i, item in enumerate(items):
        r: SearchResult = item["r"]
        year = f" ({r.year})" if r.year else ""
        rating = f" ⭐ {r.rating}" if r.rating else ""
        lines.append(f"📦{i + 1}. {_type_ar(r.type)} <b>{_esc(r.title)}</b>{year}{rating}")
    lines.append("\nاختار من الأزرار تحت 👇")
    return truncate_html("\n".join(lines), CAPTION_LIMIT)


@router.callback_query(F.data.startswith("site:m:"))
async def on_site_moviebox(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache, db: Database
) -> None:
    key, query = _picker_query(callback, cache)
    if not query:
        await callback.answer("⌛ انتهت صلاحية البحث ده — ابعت الاسم تاني من فضلك.", show_alert=True)
        return
    if not await _site_enabled(db, "moviebox"):
        await callback.answer("🔒 موفي بوكس متوقف مؤقتاً من الإدارة.", show_alert=True)
        return
    await callback.answer("⏳ بدور في موفي بوكس…")
    user_id = callback.from_user.id
    await db.log_request(user_id, query, site="moviebox")
    try:
        results = await moviebox.search(query)
    except NotFoundError:
        results = []
    except Exception:  # noqa: BLE001
        log.exception("moviebox search failed: %s", query)
        await _respond(callback, "❌ حصلت مشكلة في البحث دلوقتي، جرب تاني بعد شوية 🙏")
        return
    items: list[dict] = [{"r": r, "site": "m"} for r in results]
    if not items:
        await _respond(
            callback, f"🙁 مفيش نتايج لـ «{_esc(query)}» في موفي بوكس، جرب اسم تاني."
        )
        return
    cache.set(f"search:{user_id}:{key}", items)
    cache.set(f"lastsearch:{user_id}", key)
    poster = _first_poster([it["r"] for it in items])
    await _respond(callback, _mb_results_text(query, items), mb_results_kb(items, key), photo=poster)


@router.callback_query(F.data.startswith("mbtrend:"))
async def on_mb_trending(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache, db: Database
) -> None:
    page = int(callback.data.split(":")[1])
    if not await _site_enabled(db, "moviebox"):
        await callback.answer("🔒 موفي بوكس متوقف مؤقتاً من الإدارة.", show_alert=True)
        return
    await callback.answer("⏳ بجيب الرائج…")
    user_id = callback.from_user.id
    await db.log_request(user_id, f"🔥 الرائج (صفحة {page})", site="moviebox")
    try:
        results = await moviebox.trending(page)
    except Exception:  # noqa: BLE001
        log.exception("moviebox trending failed: page %s", page)
        await _respond(callback, "❌ حصلت مشكلة وأنا بجيب الرائج، جرب تاني بعد شوية 🙏")
        return
    items: list[dict] = [{"r": r, "site": "m"} for r in results]
    if not items:
        await _respond(callback, "🙁 مفيش محتوى رائج في الصفحة دي — ارجع للصفحة اللي قبلها.")
        return
    key = f"mbtr{page}"
    cache.set(f"search:{user_id}:{key}", items)
    cache.set(f"lastsearch:{user_id}", key)
    poster = _first_poster([it["r"] for it in items])
    await _respond(
        callback, _mb_trending_text(items, page), mb_trending_kb(items, key, page), photo=poster
    )


# ---------- اختيار نتيجة ----------

@router.callback_query(F.data.startswith("r:"))
async def on_result(
    callback: CallbackQuery,
    akwam: AkwamClient,
    starcima: StarcimaClient,
    moviebox: MovieboxClient,
    cache: TTLCache,
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
    if isinstance(item, dict) and item.get("site") == "m":  # نتيجة موفي بوكس
        try:
            await _show_mb_result(callback, moviebox, cache, item, qkey=key)
        except NotFoundError:
            await callback.answer("❌ الصفحة دي مش موجودة على موفي بوكس.", show_alert=True)
        except Exception:  # noqa: BLE001
            log.exception("moviebox result flow failed")
            await callback.answer("❌ حصل خطأ وأنا بجيب التفاصيل، جرب تاني.", show_alert=True)
        return
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


# ---------- تدفق موفي بوكس (SPEC3 قسم 4.4) ----------

@dataclass
class _MbCtx:
    """سياق موفي بوكس — يتخزن في الكاش تحت mb:{ckey}."""

    subject_id: str
    detail_path: str
    title: str
    poster: str | None
    qkey: str | None = None  # مفتاح الاستعلام لزر «جرّب موقع آخر» (فارغ من الرائج)


def _new_mb_ctx(cache: TTLCache, ctx: _MbCtx) -> str:
    ckey = uuid4().hex[:8]
    cache.set(f"mb:{ckey}", ctx)
    return ckey


async def _get_mb_details(
    moviebox: MovieboxClient, cache: TTLCache, ckey: str, ctx: _MbCtx
) -> MbDetails:
    """تفاصيل موفي بوكس مع كاش لكل ckey (المواسم/النسخ لا تتغير أثناء الجلسة)."""
    cached: MbDetails | None = cache.get(f"mbdet:{ckey}")
    if cached is not None:
        return cached
    details = await moviebox.get_details(ctx.detail_path)
    cache.set(f"mbdet:{ckey}", details)
    return details


def _mb_display(ctx: _MbCtx, se: int, ep: int) -> str:
    if se > 0:
        return f"{ctx.title} — الموسم {se} الحلقة {ep}"
    return ctx.title


def _mb_site_url(ctx: _MbCtx) -> str:
    return f"{settings.MOVIEBOX_DOMAIN}/detail/{ctx.detail_path}"


def _mb_size(size: int | None) -> str:
    if not size or size <= 0:
        return "حجم غير معروف"
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024:.0f} KB"


async def _show_mb_result(
    callback: CallbackQuery,
    moviebox: MovieboxClient,
    cache: TTLCache,
    item: dict,
    qkey: str | None,
) -> None:
    r: SearchResult = item["r"]
    detail_path = r.url.split("/detail/", 1)[-1] if "/detail/" in r.url else r.url
    ctx = _MbCtx(
        subject_id=str(r.id),
        detail_path=detail_path,
        title=r.title,
        poster=r.poster,
        qkey=qkey,
    )
    ckey = _new_mb_ctx(cache, ctx)
    details = await moviebox.get_details(detail_path)
    cache.set(f"mbdet:{ckey}", details)
    await _render_mb_details(callback, cache, ckey, ctx, details)


async def _render_mb_details(
    callback: CallbackQuery,
    cache: TTLCache,
    ckey: str,
    ctx: _MbCtx,
    details: MbDetails,
) -> None:
    year = f" ({details.year})" if details.year else ""
    rating = f"⭐ {details.rating}\n" if details.rating else ""
    desc = (details.description or "").strip()
    if len(desc) > 350:
        desc = desc[:347] + "…"
    site_url = _mb_site_url(ctx)
    has_dubs = bool(details.dubs)
    if details.type == "movie":
        caption = truncate_html(
            f"🎬 <b>{_esc(details.title)}</b>{year} — 📦 موفي بوكس\n"
            f"{rating}\n{_esc(desc)}\n\n"
            "اختار من تحت 👇",
            CAPTION_LIMIT,
        )
        kb = mb_details_kb(ckey, site_url, has_dubs)
    else:
        caption = truncate_html(
            f"📺 <b>{_esc(details.title)}</b>{year} — 📦 موفي بوكس\n"
            f"{rating}\n{_esc(desc)}\n\n"
            "اختار الموسم 👇",
            CAPTION_LIMIT,
        )
        kb = mb_seasons_kb(ckey, details.seasons, site_url, has_dubs)
    await _respond(callback, caption, kb, photo=details.poster or ctx.poster)


@router.callback_query(F.data.startswith("mbi:"))
async def on_mb_info(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    ckey = callback.data.split(":")[1]
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer()
    try:
        details = await _get_mb_details(moviebox, cache, ckey, ctx)
    except Exception:  # noqa: BLE001
        log.exception("mb details failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ وأنا بجيب التفاصيل، جرب تاني.", show_alert=True)
        return
    await _render_mb_details(callback, cache, ckey, ctx, details)


# ---------- مواسم وحلقات موفي بوكس ----------

async def _show_mb_episodes(
    callback: CallbackQuery,
    moviebox: MovieboxClient,
    cache: TTLCache,
    ckey: str,
    se: int,
    page: int,
) -> None:
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    try:
        details = await _get_mb_details(moviebox, cache, ckey, ctx)
    except Exception:  # noqa: BLE001
        log.exception("mb episodes failed: %s/%s", ckey, se)
        await callback.answer("❌ حصل خطأ وأنا بجيب الحلقات، جرب تاني.", show_alert=True)
        return
    season = next((s for s in details.seasons if s.se == se), None)
    if season is None:
        await callback.answer("❌ الموسم ده مش موجود.", show_alert=True)
        return
    per_page = settings.EPISODES_PER_PAGE
    pages = max(1, (season.max_ep + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    text = (
        f"📺 <b>{_esc(details.title)}</b> — الموسم {se} — 📦 موفي بوكس\n"
        f"عدد الحلقات: {season.max_ep} — صفحة {page}/{pages}\n\n"
        "اختار الحلقة 👇"
    )
    await _respond(
        callback,
        text,
        mb_episodes_kb(ckey, se, season.max_ep, season.all_ep, page, per_page),
        photo=details.poster or ctx.poster,
    )


@router.callback_query(F.data.startswith("mbs:"))
async def on_mb_season(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se = callback.data.split(":")
    await callback.answer("⏳ بجيب الحلقات…")
    await _show_mb_episodes(callback, moviebox, cache, ckey, int(se), 1)


@router.callback_query(F.data.startswith("mbeps:"))
async def on_mb_episodes_page(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se, page = callback.data.split(":")
    await callback.answer()
    await _show_mb_episodes(callback, moviebox, cache, ckey, int(se), int(page))


# ---------- شاشة الجودات (طازجة دائماً — الروابط بتنتهي) ----------

def _mb_season_of(details: MbDetails, se: int):
    return next((s for s in details.seasons if s.se == se), None)


async def _show_mb_streams(
    callback: CallbackQuery,
    moviebox: MovieboxClient,
    cache: TTLCache,
    ckey: str,
    se: int,
    ep: int,
) -> None:
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    display = _mb_display(ctx, se, ep)
    try:
        streams = await moviebox.get_streams(ctx.subject_id, ctx.detail_path, se=se, ep=ep)
    except Exception:  # noqa: BLE001
        log.exception("mb get_streams failed: %s %s/%s", ctx.subject_id, se, ep)
        streams = None
    if streams is None or not streams.qualities:
        text = (
            f"😕 مفيش جودات متاحة لـ <b>{_esc(display)}</b> دلوقتي.\n"
            "جرّب تاني بعد شوية أو دور في موقع تاني 👇"
        )
        kb = None
        if ctx.qkey:
            kb = site_picker_kb(ctx.qkey)  # «جرّب موقع آخر» بنفس مفتاح البحث
        await _respond(callback, text, kb, photo=ctx.poster)
        return
    prev_ep = next_ep = None
    eps_back_page = 1
    if se > 0:
        try:
            details = await _get_mb_details(moviebox, cache, ckey, ctx)
        except Exception:  # noqa: BLE001
            log.exception("mb details for nav failed: %s", ckey)
            details = None
        season = _mb_season_of(details, se) if details is not None else None
        if season is not None:
            available = (
                sorted(season.all_ep)
                if season.all_ep is not None
                else list(range(1, season.max_ep + 1))
            )
            prev_ep = next((n for n in reversed(available) if n < ep), None)
            next_ep = next((n for n in available if n > ep), None)
            eps_back_page = (ep - 1) // settings.EPISODES_PER_PAGE + 1
    quals = [(q.resolution, _mb_size(q.size)) for q in streams.qualities]
    has_ar = any(c.lan == "ar" for c in streams.captions)
    has_other = any(c.lan != "ar" for c in streams.captions)
    icon = "📺" if se > 0 else "🎬"
    text = truncate_html(
        f"{icon} <b>{_esc(display)}</b> — 📦 موفي بوكس\n\n"
        "اختار الجودة 👇\n"
        "⬇️ إرسال مباشر هنا ⭐ بريميوم — 🔗 رابط تحميل مباشر",
        CAPTION_LIMIT,
    )
    kb = mb_streams_kb(
        ckey,
        se,
        ep,
        quals,
        has_ar,
        has_other,
        prev_ep=prev_ep,
        next_ep=next_ep,
        eps_back_page=eps_back_page,
        site_url=_mb_site_url(ctx),
    )
    await _respond(callback, text, kb, photo=ctx.poster)


@router.callback_query(F.data.startswith("mbq:"))
async def on_mb_qualities(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se, ep = callback.data.split(":")
    await callback.answer("⏳ بجيب الجودات…")
    await _show_mb_streams(callback, moviebox, cache, ckey, int(se), int(ep))


@router.callback_query(F.data.startswith("mbep:"))
async def on_mb_episode(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se, ep = callback.data.split(":")
    await callback.answer("⏳ بجيب الجودات…")
    await _show_mb_streams(callback, moviebox, cache, ckey, int(se), int(ep))


# ---------- إرسال / رابط ----------

@router.callback_query(F.data.startswith("mbsend:"))
async def on_mb_send(
    callback: CallbackQuery,
    moviebox: MovieboxClient,
    cache: TTLCache,
    downloader: DownloadManager,
    db: Database,
) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    _, ckey, se, ep, res = callback.data.split(":")
    se_i, ep_i, res_i = int(se), int(ep), int(res)
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بجهز التحميل…")
    try:
        streams = await moviebox.get_streams(ctx.subject_id, ctx.detail_path, se=se_i, ep=ep_i)
    except Exception:  # noqa: BLE001
        log.exception("mb send get_streams failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ وأنا بجيب الرابط، جرب تاني.", show_alert=True)
        return
    quality = next((q for q in streams.qualities if q.resolution == res_i), None)
    if quality is None:
        await callback.answer(
            "🙁 الجودة دي مش متاحة دلوقتي — افتح شاشة الجودات تاني.", show_alert=True
        )
        return
    display = _mb_display(ctx, se_i, ep_i)
    title = f"{display} ({res_i}p 📦)"
    job = DownloadJob(
        task_id=uuid4().hex[:12],
        title=title,
        url=quality.url,
        caption=(
            f"🎬 <b>{_esc(display)}</b>\n"
            f"📦 موفي بوكس — 💾 الجودة: {res_i}p"
        ),
        thumb_url=ctx.poster,
        referer=VIDEO_REFERER,
        dash_url=streams.dash_url,
        dash_res=res_i,
    )
    await downloader.enqueue(callback.from_user.id, callback.message.chat.id, job)
    await db.log_download(
        callback.from_user.id, title, f"{res_i}p", "queued", site="moviebox"
    )
    await callback.answer(f"✅ «{display}» اتضاف للتحميل", show_alert=True)


@router.callback_query(F.data.startswith("mblink:"))
async def on_mb_link(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se, ep, res = callback.data.split(":")
    se_i, ep_i, res_i = int(se), int(ep), int(res)
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بجهز الرابط…")
    try:
        streams = await moviebox.get_streams(ctx.subject_id, ctx.detail_path, se=se_i, ep=ep_i)
    except Exception:  # noqa: BLE001
        log.exception("mb link get_streams failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ وأنا بجيب الرابط، جرب تاني.", show_alert=True)
        return
    quality = next((q for q in streams.qualities if q.resolution == res_i), None)
    if quality is None:
        await callback.answer(
            "🙁 الجودة دي مش متاحة دلوقتي — افتح شاشة الجودات تاني.", show_alert=True
        )
        return
    await callback.message.answer(
        f"🔗 رابط التحميل المباشر جاهز بجودة <b>{res_i}p</b>\n"
        "⚠️ لو الرابط مافتحش في المتصفح (خطأ 429) استخدم ⬇️ الإرسال المباشر — "
        "الرابط بيحتاج Referer خاص.",
        reply_markup=mb_link_kb(quality.url),
    )


# ---------- الترجمة ----------

@router.callback_query(F.data.startswith("mbsub:"))
async def on_mb_sub(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se, ep, lan = callback.data.split(":", 4)
    se_i, ep_i = int(se), int(ep)
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بجيب الترجمة…")
    try:
        streams = await moviebox.get_streams(ctx.subject_id, ctx.detail_path, se=se_i, ep=ep_i)
    except Exception:  # noqa: BLE001
        log.exception("mb sub get_streams failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ وأنا بجيب الترجمة، جرب تاني.", show_alert=True)
        return
    caption_info = next((c for c in streams.captions if c.lan == lan), None)
    if caption_info is None:
        await callback.answer("🙁 مفيش ترجمة باللغة دي للنسخة دي.", show_alert=True)
        return
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(caption_info.url)  # بدون referer — روابط الترجمة موقّعة
            resp.raise_for_status()
            srt_bytes = resp.content
    except Exception:  # noqa: BLE001
        log.exception("mb sub download failed: %s", caption_info.url)
        await callback.answer("❌ مقدرتش أنزّل ملف الترجمة، جرب تاني.", show_alert=True)
        return
    display = _mb_display(ctx, se_i, ep_i)
    safe_title = re.sub(r"[^\w\s\-]", "", ctx.title).strip() or "subtitle"
    filename = f"{safe_title}.{lan}.srt"
    lang_name = caption_info.lan_name or lan
    await callback.bot.send_document(
        callback.message.chat.id,
        BufferedInputFile(srt_bytes, filename=filename),
        caption=(
            f"📝 ترجمة <b>{_esc(display)}</b>\n"
            f"🌐 اللغة: {_esc(lang_name)} — 📦 موفي بوكس"
        ),
    )


@router.callback_query(F.data.startswith("mblangs:"))
async def on_mb_langs(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, se, ep, page = callback.data.split(":")
    se_i, ep_i, page_i = int(se), int(ep), int(page)
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بجيب اللغات…")
    try:
        streams = await moviebox.get_streams(ctx.subject_id, ctx.detail_path, se=se_i, ep=ep_i)
    except Exception:  # noqa: BLE001
        log.exception("mb langs get_streams failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ وأنا بجيب اللغات، جرب تاني.", show_alert=True)
        return
    if not streams.captions:
        await callback.answer("🙁 مفيش ترجمات متاحة للنسخة دي.", show_alert=True)
        return
    display = _mb_display(ctx, se_i, ep_i)
    await callback.message.answer(
        f"🌐 لغات الترجمة المتاحة لـ <b>{_esc(display)}</b> 👇",
        reply_markup=mb_langs_kb(ckey, se_i, ep_i, streams.captions, page_i),
    )


# ---------- النسخ والدبلجات ----------

@router.callback_query(F.data.startswith("mbdubs:"))
async def on_mb_dubs(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    ckey = callback.data.split(":")[1]
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer()
    try:
        details = await _get_mb_details(moviebox, cache, ckey, ctx)
    except Exception:  # noqa: BLE001
        log.exception("mb dubs failed: %s", ckey)
        await callback.answer("❌ حصل خطأ وأنا بجيب النسخ، جرب تاني.", show_alert=True)
        return
    if not details.dubs:
        await callback.answer("🙁 مفيش نسخ أو دبلجات تانية للمحتوى ده.", show_alert=True)
        return
    await _respond(
        callback,
        f"🌐 النسخ واللغات المتاحة لـ <b>{_esc(details.title)}</b>:\nاختار النسخة 👇",
        mb_dubs_kb(ckey, details.dubs),
    )


@router.callback_query(F.data.startswith("mbd:"))
async def on_mb_dub(
    callback: CallbackQuery, moviebox: MovieboxClient, cache: TTLCache
) -> None:
    _, ckey, idx = callback.data.split(":")
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بفتح النسخة…")
    try:
        details = await _get_mb_details(moviebox, cache, ckey, ctx)
        dub = details.dubs[int(idx)]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("mb dub failed: %s", callback.data)
        await callback.answer("❌ حصل خطأ، جرب تاني.", show_alert=True)
        return
    ctx.subject_id = dub.subject_id
    ctx.detail_path = dub.detail_path
    cache.set(f"mb:{ckey}", ctx)
    try:
        new_details = await moviebox.get_details(dub.detail_path)
    except NotFoundError:
        await callback.answer("❌ النسخة دي مش موجودة على موفي بوكس.", show_alert=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("mb dub details failed: %s", dub.detail_path)
        await callback.answer("❌ حصل خطأ وأنا بجيب التفاصيل، جرب تاني.", show_alert=True)
        return
    cache.set(f"mbdet:{ckey}", new_details)
    await _render_mb_details(callback, cache, ckey, ctx, new_details)


# ---------- تحميل موسم موفي بوكس كامل ----------

def _best_mb_quality(qualities):
    """أفضل جودة ≤720 متاحة، وإلا الأعلى."""
    if not qualities:
        return None
    le720 = [q for q in qualities if q.resolution <= 720]
    return max(le720 or qualities, key=lambda q: q.resolution)


@router.callback_query(F.data.startswith("mbsall:"))
async def on_mb_season_all(
    callback: CallbackQuery,
    moviebox: MovieboxClient,
    cache: TTLCache,
    downloader: DownloadManager,
    db: Database,
) -> None:
    if not await _send_allowed(db, callback.from_user.id):
        await callback.answer(PREMIUM_ONLY_MSG, show_alert=True)
        return
    _, ckey, se = callback.data.split(":")
    se_i = int(se)
    ctx: _MbCtx | None = cache.get(f"mb:{ckey}")
    if ctx is None:
        await callback.answer("⌛ انتهت صلاحية الصفحة دي — ابعت البحث تاني.", show_alert=True)
        return
    await callback.answer("⏳ بجهز حلقات الموسم…")
    progress = await callback.message.answer("⏳ بجيب روابط الحلقات وأضيف اللي ينفع للطابور…")
    try:
        details = await _get_mb_details(moviebox, cache, ckey, ctx)
    except Exception:  # noqa: BLE001
        log.exception("mbsall details failed: %s", callback.data)
        await progress.edit_text("❌ حصل خطأ وأنا بجهز الموسم، جرب تاني.")
        return
    season = _mb_season_of(details, se_i)
    if season is None:
        await progress.edit_text("🙁 الموسم ده مش موجود.")
        return
    episodes = (
        sorted(season.all_ep)
        if season.all_ep is not None
        else list(range(1, season.max_ep + 1))
    )
    if not episodes:
        await progress.edit_text("🙁 الموسم ده مفيهوش حلقات متاحة.")
        return
    total = len(episodes)
    added = 0
    skipped: list[int] = []
    for num in episodes:
        quality = None
        try:
            streams = await moviebox.get_streams(
                ctx.subject_id, ctx.detail_path, se=se_i, ep=num
            )
            quality = _best_mb_quality(streams.qualities)
        except Exception:  # noqa: BLE001
            log.exception("mbsall episode failed: %s/%s", se_i, num)
        if quality is None:
            skipped.append(num)
            continue
        display = f"{ctx.title} — الموسم {se_i} الحلقة {num}"
        title = f"{display} ({quality.resolution}p 📦)"
        job = DownloadJob(
            task_id=uuid4().hex[:12],
            title=title,
            url=quality.url,
            caption=(
                f"📺 <b>{_esc(display)}</b>\n"
                f"📦 موفي بوكس — 💾 الجودة: {quality.resolution}p"
            ),
            thumb_url=ctx.poster or details.poster,
            referer=VIDEO_REFERER,
            dash_url=streams.dash_url,
            dash_res=quality.resolution,
        )
        await downloader.enqueue(callback.from_user.id, callback.message.chat.id, job)
        await db.log_download(
            callback.from_user.id,
            title,
            f"{quality.resolution}p",
            "queued",
            site="moviebox",
        )
        added += 1
    text = (
        f"✅ اتضاف <b>{added}</b> من أصل <b>{total}</b> حلقة لطابور التحميل\n"
        "الحلقات هتتبعتلك ورا بعض واحدة واحدة 📥"
    )
    if skipped:
        nums = "، ".join(str(n) for n in skipped)
        text += f"\n\n⚠️ الحلقات دي ماتضافتش (مفيش جودة شغالة): {nums}"
    await progress.edit_text(text)


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
    if isinstance(results[0], dict) and results[0].get("site") == "m":  # نتايج موفي بوكس
        await _respond(
            callback,
            _mb_results_text("آخر بحث", results),
            mb_results_kb(results, key),
            photo=_first_poster([it["r"] for it in results]),
        )
        return
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
