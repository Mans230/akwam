"""عميل ستار سيما غير المتزامن (httpx.AsyncClient) — JSON APIs فقط، بدون HTML parsing.

العقد الملزم من SPEC2 3.2. الاعتماد كله على ``/api/tmdb/*`` (بروكسي TMDB)
و``/api/arabic-sources`` و``/api/extract`` و``/api/wyzie-subs`` و``/api/dubbed/*``.
showbox/vidzee معطّلان حالياً — لا نعتمد عليهما إطلاقاً.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote, urlparse

import httpx

from akwam.client import NotFoundError  # إعادة تصدير من أكوام (عقد مشترك)
from akwam.models import SearchResult

from .models import (
    ExtractedStream,
    ScEpisode,
    ScMediaDetails,
    ScSeason,
    ServerLink,
)

__all__ = ["StarcimaClient", "NotFoundError"]

# UA متصفح ثابت — الموقع لا يتطلب cookies ولا referer ولا JS challenge
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

TMDB_IMG = "https://image.tmdb.org/t/p"


def _poster(path: str | None, size: str = "w500") -> str | None:
    """رابط صورة TMDB كامل من مسار نسبي."""
    return f"{TMDB_IMG}/{size}{path}" if path else None


def _year(date_str: str | None) -> int | None:
    """السنة من تاريخ ISO (release_date / first_air_date)."""
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


def _classify_provider(embed_url: str) -> tuple[str, bool, bool]:
    """تصنيف السيرفر من هوست رابط التضمين → (provider, downloadable, is_akwam)."""
    host = urlparse(embed_url).netloc.lower()
    if "akwam" in host:
        return "akwam", True, True
    if "streamwish" in host or "hlswish" in host or "swh" in host:
        return "streamwish", True, False
    if "updown" in host:
        return "updown", True, False
    if "vidtube" in host:
        return "vidtube", True, False
    return "other", False, False


class StarcimaClient:
    """عميل starcima: بحث TMDB + مدبلج + تفاصيل + سيرفرات + استخراج + ترجمات."""

    def __init__(self, base_url: str, timeout: float = 30.0, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    async def close(self) -> None:
        """إغلاق جلسة httpx."""
        await self._client.aclose()

    async def __aenter__(self) -> "StarcimaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET مع retry بـ backoff (1s, 2s, 4s) عند فشل الشبكة أو 5xx.

        يرمي NotFoundError عند 404، وhttpx.HTTPStatusError عند باقي أخطاء 4xx
        بعد استنفاد المحاولات.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code == 404:
                    raise NotFoundError(f"غير موجود (404): {resp.url}")
                if resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"خطأ سيرفر {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2**attempt)  # 1s, 2s, 4s
                        continue
                    raise last_exc
                resp.raise_for_status()
                return resp
            except NotFoundError:
                raise
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def _get_json(self, path: str, params: dict | None = None):
        """GET وإرجاع جسم الاستجابة كـ JSON."""
        resp = await self._get(path, params=params)
        return json.loads(resp.text)

    # ------------------------------------------------------------------ بحث
    async def search(self, query: str, page: int = 1) -> list[SearchResult]:
        """بحث TMDB متعدد (فيلم/مسلسل) بالعربية؛ يتجاهل media_type='person'."""
        data = await self._get_json(
            "/api/tmdb/search/multi",
            params={"query": query, "language": "ar-SA", "page": page},
        )
        results: list[SearchResult] = []
        for item in data.get("results") or []:
            media_type = item.get("media_type")
            if media_type not in ("movie", "tv"):
                continue  # person وغيرها
            tmdb_id = item.get("id")
            if tmdb_id is None:
                continue
            typ = "series" if media_type == "tv" else "movie"
            results.append(
                SearchResult(
                    id=int(tmdb_id),
                    type=typ,
                    title=item.get("title") or item.get("name") or "",
                    url=f"{self.base_url}/media/{tmdb_id}?type={media_type}",
                    poster=_poster(item.get("poster_path")),
                    year=_year(item.get("release_date") or item.get("first_air_date")),
                    rating=item.get("vote_average"),
                )
            )
        return results

    async def search_dubbed(self, query: str) -> list[SearchResult]:
        """بحث المحتوى المدبلج (مصدر ArabicToons) — type='dubbed' والعنوان مسبوق بـ (مدبلج)."""
        data = await self._get_json("/api/dubbed/search", params={"q": query})
        results: list[SearchResult] = []
        for item in data.get("results") or []:
            title = item.get("title") or item.get("key") or ""
            seasons = item.get("seasons") or []
            toons_ids = [str(s.get("arabicToonsId")) for s in seasons if s.get("arabicToonsId")]
            try:
                item_id = int(toons_ids[0]) if toons_ids else 0
            except ValueError:
                item_id = 0
            image = item.get("image")
            poster = None
            if image:
                poster = image if image.startswith("http") else f"{self.base_url}{image}"
            # صفحة المشاهدة الأصلية على الموقع: /dubbed/watch?at=<ids>&title=..
            url = f"{self.base_url}/dubbed/watch?at={','.join(toons_ids)}&title={quote(title)}"
            results.append(
                SearchResult(
                    id=item_id,
                    type="dubbed",
                    title=f"(مدبلج) {title}",
                    url=url,
                    poster=poster,
                )
            )
        return results

    # ---------------------------------------------------------------- تفاصيل
    async def get_media(self, tmdb_id: int, type: str) -> ScMediaDetails:
        """تفاصيل فيلم/مسلسل من بروكسي TMDB؛ المسلسل يشمل المواسم (بدون specials)."""
        kind = "tv" if type in ("series", "tv") else "movie"
        data = await self._get_json(
            f"/api/tmdb/{kind}/{tmdb_id}",
            params={"language": "ar-SA", "append_to_response": "external_ids"},
        )
        seasons: list[ScSeason] = []
        if kind == "tv":
            for s in data.get("seasons") or []:
                num = s.get("season_number")
                if not num:  # استبعاد season_number=0 (specials) والقيم الناقصة
                    continue
                seasons.append(
                    ScSeason(
                        number=int(num),
                        episode_count=int(s.get("episode_count") or 0),
                        name=s.get("name"),
                    )
                )
        return ScMediaDetails(
            id=int(data.get("id", tmdb_id)),
            type="series" if kind == "tv" else "movie",
            title_ar=data.get("title") or data.get("name") or "",
            title_en=data.get("original_title") or data.get("original_name"),
            year=_year(data.get("release_date") or data.get("first_air_date")),
            rating=data.get("vote_average"),
            description=data.get("overview"),
            poster=_poster(data.get("poster_path")),
            seasons=seasons,
        )

    async def get_episodes(self, tmdb_id: int, season: int) -> list[ScEpisode]:
        """حلقات موسم مسلسل — thumb من still_path (w300)."""
        data = await self._get_json(
            f"/api/tmdb/tv/{tmdb_id}/season/{season}",
            params={"language": "ar-SA"},
        )
        episodes: list[ScEpisode] = []
        for ep in data.get("episodes") or []:
            num = ep.get("episode_number")
            if num is None:
                continue
            episodes.append(
                ScEpisode(
                    number=int(num),
                    title=ep.get("name") or f"الحلقة {num}",
                    season=int(ep.get("season_number") or season),
                    overview=ep.get("overview"),
                    thumb=_poster(ep.get("still_path"), "w300"),
                )
            )
        return episodes

    # ---------------------------------------------------------------- سيرفرات
    async def get_servers(
        self,
        title_ar: str,
        title_en: str | None,
        year: int | None,
        type: str,
        season: int | None = None,
        episode: int | None = None,
        abs_episode: int | None = None,
        season_ep_count: int | None = None,
    ) -> list[ServerLink]:
        """سيرفرات المشاهدة مع تصنيف provider؛ القابل للتحميل أولاً (ترتيب مستقر)."""
        params: dict = {
            "title": title_ar,
            "type": "tv" if type in ("series", "tv") else "movie",
            "englishTitle": title_en or title_ar,
        }
        if year is not None:
            params["year"] = year
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episode"] = episode
        if abs_episode is not None:
            params["absEpisode"] = abs_episode
        if season_ep_count is not None:
            params["seasonEpCount"] = season_ep_count
        data = await self._get_json("/api/arabic-sources", params=params)
        links: list[ServerLink] = []
        for s in data.get("servers") or []:
            embed_url = s.get("embedUrl")
            if not embed_url:
                continue
            provider, downloadable, is_akwam = _classify_provider(embed_url)
            links.append(
                ServerLink(
                    name=s.get("name") or f"سيرفر {len(links) + 1}",
                    embed_url=embed_url,
                    provider=provider,
                    downloadable=downloadable,
                    is_akwam=is_akwam,
                )
            )
        # القابل للتحميل أولاً ثم الباقي — sort مستقر فيحفظ ترتيب الموقع داخل كل مجموعة
        links.sort(key=lambda l: not l.downloadable)
        return links

    # --------------------------------------------------------------- استخراج
    async def extract(self, embed_url: str) -> ExtractedStream | None:
        """استخراج رابط مباشر من سيرفر؛ يرجع None عند أي فشل (بدون استثناء)."""
        try:
            data = await self._get_json("/api/extract", params={"url": embed_url})
        except Exception:
            return None  # 404 {"error":...} أو فشل شبكة — الفشل متوقع ومتسامح معه
        if not isinstance(data, dict) or data.get("error"):
            return None
        kind = data.get("type")
        direct_url = data.get("directUrl") or data.get("url")
        if kind not in ("hls", "mp4") or not direct_url:
            return None
        url = data.get("url")
        proxy_url: str | None = None
        if isinstance(url, str) and url:
            if url.startswith("/"):  # نسبي → ركّبه على base_url (بروكسي ستار سيما)
                proxy_url = f"{self.base_url}{url}"
            elif url != direct_url:
                proxy_url = url
        return ExtractedStream(kind=kind, direct_url=direct_url, proxy_url=proxy_url)

    # ---------------------------------------------------------------- ترجمات
    async def get_subtitles(
        self,
        tmdb_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[str]:
        """روابط SRT عربية من wyzie-subs (قد تكون فارغة)."""
        params: dict = {"id": tmdb_id}
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episode"] = episode
        try:
            data = await self._get_json("/api/wyzie-subs", params=params)
        except Exception:
            return []
        subs = data.get("subtitles") if isinstance(data, dict) else data
        links: list[str] = []
        for s in subs or []:
            if isinstance(s, str):  # تسامح: الاستجابة قد تكون قائمة روابط نصية
                links.append(s)
            elif isinstance(s, dict):
                url = s.get("url")
                fmt = (s.get("format") or "srt").lower()
                if url and fmt == "srt":
                    links.append(url)
        return links

    # ---------------------------------------------------------------- مدبلج
    async def get_dubbed_episodes(self, series_key: str) -> list[ScEpisode]:
        """حلقات المحتوى المدبلج — series_key هو arabicToonsId من نتايج البحث."""
        data = await self._get_json("/api/dubbed/episodes", params={"series": series_key})
        episodes: list[ScEpisode] = []
        for ep in (data or {}).get("episodes") or []:
            num = ep.get("number")
            if num is None:
                continue
            thumb = ep.get("thumbnail")
            if thumb and not thumb.startswith("http"):
                thumb = f"{self.base_url}{thumb}"
            episodes.append(
                ScEpisode(
                    number=int(num),
                    title=f"الحلقة {num}",
                    season=1,
                    overview=None,
                    thumb=thumb,
                )
            )
        return episodes
