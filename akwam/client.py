"""عميل أكوام غير المتزامن (httpx.AsyncClient) مع retries ومعالجة 404.

العقد الملزم من SPEC 3.2 — الـ parsing كله في ``akwam.parsers`` وهنا الجلب فقط.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote_plus

import httpx

from . import parsers
from .models import (
    DirectLink,
    EpisodeDetails,
    MovieDetails,
    SearchResult,
    SeriesDetails,
)

# UA ثابت موثق في info.md — الموقع لا يتطلب cookies ولا referer
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class NotFoundError(Exception):
    """تُرمى عندما يرد الموقع بـ 404 (صفحة غير موجودة)."""


class AkwamClient:
    """عميل سكرابر موقع akwam: بحث + تفاصيل أفلام/مسلسلات/حلقات + روابط مباشرة."""

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

    async def __aenter__(self) -> "AkwamClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict[str, str] | None = None) -> str:
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
                return resp.text
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

    async def search(self, query: str, section: str | None = None) -> list[SearchResult]:
        """بحث في الموقع؛ section اختياري ('movie' / 'series' / ...)."""
        params = {"q": query}
        if section:
            params["section"] = section
        html = await self._get("/search", params=params)
        return parsers.parse_search_results(html, self.base_url)

    async def search_seasons(self, query: str) -> list[SearchResult]:
        """بحث section='series' وترجيع نتايج المسلسلات (تُستخدم لتجميع المواسم)."""
        return await self.search(query, section="series")

    async def get_movie(self, movie_id: int) -> MovieDetails:
        """تفاصيل فيلم من /movie/{id} (الـ id وحده يكفي — redirect للكامل)."""
        html = await self._get(f"/movie/{movie_id}")
        return parsers.parse_movie_details(html, movie_id, self.base_url)

    async def get_series(self, series_id: int) -> SeriesDetails:
        """تفاصيل مسلسل (موسم) من /series/{id} مع كل الحلقات مرتبة."""
        html = await self._get(f"/series/{series_id}")
        return parsers.parse_series_details(html, series_id, self.base_url)

    async def get_episode(self, episode_id: int) -> EpisodeDetails:
        """تفاصيل حلقة من /episode/{id}: الجودات + رقم الحلقة + التالية/السابقة."""
        html = await self._get(f"/episode/{episode_id}")
        return parsers.parse_episode_details(html, episode_id, self.base_url)

    async def get_direct_links(self, file_id: int, content_id: int) -> list[DirectLink]:
        """GET صفحة /watch/{file_id}/{content_id} واستخراج video#player source لكل الجودات."""
        html = await self._get(f"/watch/{file_id}/{content_id}")
        return parsers.parse_direct_links(html)

    async def resolve_download(self, file_id: int, content_id: int) -> DirectLink | None:
        """بديل: صفحة /download الوسيطة → div.page-redirect div.btn-loader a. يُستخدم كـ fallback."""
        html = await self._get(f"/download/{file_id}/{content_id}")
        url = parsers.parse_download_redirect(html)
        if url is None:
            return None
        filename = url.rsplit("/", 1)[-1] or None
        quality = ""
        if filename:
            m = parsers._QUALITY_RE.search(filename)
            if m:
                quality = f"{m.group(1)}p"
        return DirectLink(url=url, quality=quality, filename=filename)
