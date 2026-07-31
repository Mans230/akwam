"""تدفقات المستخدم: بحث → نتايج → فيلم/مسلسل → جودات/روابط/تحميل (حسب SPEC 3.8)."""
from __future__ import annotations

import asyncio
import html
import logging
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from akwam import AkwamClient
from akwam.client import NotFoundError
from akwam.models import DirectLink, QualityLink, SearchResult

from .cache import TTLCache
from .config import settings
from .db import Database
from .downloader import DownloadJob, DownloadManager
from .keyboards import (
    episode_kb,
    links_kb,
    movie_kb,
    search_results_kb,
    season_all_kb,
    seasons_kb,
    series_kb,
)
from .middlewares import is_subscribed

log = logging.getLogger(__name__)

router = Router()

_esc = html.escape

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
    return "🎬 فيلم" if t == "movie" else "📺 مسلسل"


def _results_text(query: str, results: list[SearchResult]) -> str:
    lines = [f"🔍 نتايج البحث عن <b>«{_esc(query)}»</b>:\n"]
    for i, r in enumerate(results):
        year = f" ({r.year})" if r.year else ""
        rating = f" ⭐ {r.rating}" if r.rating else ""
        lines.append(f"{i + 1}. {_type_ar(r.type)} <b>{_esc(r.title)}</b>{year}{rating}")
    lines.append("\nاختار من الأزرار تحت 👇")
    text = "\n".join(lines)
    return text[:1000]  # حد كابشن الصور


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


# ---------- الأوامر والبحث ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@router.message(F.text, ~F.text.startswith("/"))
async def on_search(
    message: Message, akwam: AkwamClient, cache: TTLCache, db: Database
) -> None:
    query = (message.text or "").strip()
    if not query:
        return
    user_id = message.from_user.id
    await db.log_request(user_id, query)
    wait = await message.answer(f"🔍 بدور على «{_esc(query)}»…")
    try:
        results = await akwam.search(query)
    except NotFoundError:
        results = []
    except Exception:  # noqa: BLE001
        log.exception("search failed: %s", query)
        await wait.edit_text("❌ حصلت مشكلة في البحث دلوقتي، جرب تاني بعد شوية 🙏")
        return
    if not results:
        await wait.edit_text(f"🙁 مفيش نتايج لـ «{_esc(query)}»، جرب اسم تاني.")
        return
    key = uuid4().hex[:6]
    cache.set(f"search:{user_id}:{key}", results)
    cache.set(f"lastsearch:{user_id}", key)
    text = _results_text(query, results)
    kb = search_results_kb(results, key)
    poster = _first_poster(results)
    try:
        await wait.delete()
    except TelegramBadRequest:
        pass
    if poster:
        await message.answer_photo(poster, caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


# ---------- اختيار نتيجة ----------

@router.callback_query(F.data.startswith("r:"))
async def on_result(callback: CallbackQuery, akwam: AkwamClient, cache: TTLCache) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, key, idx = parts
    results: list[SearchResult] | None = cache.get(f"search:{callback.from_user.id}:{key}")
    if not results:
        await callback.answer(
            "⌛ انتهت صلاحية النتايج دي — ابعت البحث تاني من فضلك.", show_alert=True
        )
        return
    try:
        result = results[int(idx)]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    await callback.answer("⏳ بجيب التفاصيل…")
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
    caption = (
        f"🎬 <b>{_esc(movie.title)}</b>{year}\n"
        f"{rating}\n{_esc(desc)}\n\n"
        f"💾 الجودات المتاحة: <b>{_esc(quals)}</b>"
    )[:1000]
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
) -> None:
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
async def on_season_all(callback: CallbackQuery, akwam: AkwamClient) -> None:
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
) -> None:
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
            f"❌ لسه مشتركتش في القناة {channel} — اشترك الأول وبعدين اضغط تحققت.",
            show_alert=True,
        )
