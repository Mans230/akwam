"""عميل موفي بوكس غير المتزامن (httpx.AsyncClient) — JSON API خالص، بدون HTML.

العقد الملزم من SPEC3 3.2. المصادقة (حرجة — موثقة في info-moviebox.md):
- **Bearer كسول**: ``POST /subject/search-suggest`` (بدون توكن) يرجع هيدر
  ``x-user`` فيه JSON ``{"token": "eyJ..."}`` — نخزّنه للبحث ``subject/search``.
  نجدّده مرة واحدة عند 400/401 (invalid token) ونعيد المحاولة.
- **play**: هيدر ``Origin: https://videodownloader.site`` إلزامي — وممنوع إرسال
  Authorization أو X-Client-Token معه (يرجع قوائم فارغة). التوثيق الحي أظهر أن
  ``Referer: https://videodownloader.site/`` مطلوب أيضاً لإرجاع streams فعلياً.
- الترجمات: حقل ``captions`` في رد play يرد فارغاً حالياً — الترجمات الفعلية من
  ``GET /subject/caption?id=<stream_id>&subjectId=..&se=..&ep=..`` (نفس الترجمات
  لكل سترمات الحلقة/الفيلم — موثق حياً). نقرأ حقل play أولاً ونعتبره مرجعاً احتياطياً.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from akwam.client import NotFoundError  # إعادة تصدير من أكوام (عقد مشترك)
from akwam.models import SearchResult

from .models import MbCaption, MbDetails, MbDub, MbQuality, MbSeason, MbStreams

__all__ = ["MovieboxClient", "NotFoundError", "VIDEO_REFERER"]

# UA متصفح ثابت — الموقع لا يتطلب cookies ولا JS challenge
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Referer إلزامي لروابط الـ CDN (بدونه 429) — يُمرر للداونلودر ويُصدَّر من __init__
VIDEO_REFERER = "https://videodownloader.site/"
# Origin إلزامي لـ play/caption (بدونه قوائم فارغة)
PLAY_ORIGIN = "https://videodownloader.site"


def _int_or_none(value) -> int | None:
    """تحويل متسامح لأعداد ترد كنصوص ('354124744') أو أرقام."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    """تحويل متسامح لتقييم يرد كنص ('9.0')."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _year(date_str: str | None) -> int | None:
    """السنة من releaseDate بصيغة 'YYYY-MM-DD'."""
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


class MovieboxClient:
    """عميل themoviebox: بحث (Bearer كسول) + الرائج + تفاصيل + جودات/ترجمات."""

    def __init__(
        self,
        base_url: str = "https://themoviebox.xyz",
        api_base: str = "https://h5-api.aoneroom.com/wefeed-h5api-bff",
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._token: str | None = None  # Bearer كسول — يُجلب عند أول بحث
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        """إغلاق جلسة httpx."""
        await self._client.aclose()

    async def __aenter__(self) -> "MovieboxClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --------------------------------------------------------------- طلبات
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """HTTP request مع retry بـ backoff (1s, 2s, 4s) عند فشل الشبكة أو 5xx.

        يلتقط توكن جديد من هيدر x-user في أي رد (صلاحيته طويلة لكنه يتجدد).
        يرمي NotFoundError عند 404، وhttpx.HTTPStatusError عند باقي الأخطاء
        بعد استنفاد المحاولات. أخطاء 400/401 تُعاد كما هي (لمنطق تجديد التوكن).
        """
        url = f"{self.api_base}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
                self._capture_token(resp)
                if resp.status_code in (400, 401):
                    return resp  # يعالجها المستدعي (invalid token → تجديد)
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

    def _capture_token(self, resp: httpx.Response) -> None:
        """التقاط توكن Bearer من هيدر x-user: JSON '{"token":"eyJ.."}'."""
        x_user = resp.headers.get("x-user")
        if not x_user:
            return
        try:
            token = json.loads(x_user).get("token")
        except (ValueError, AttributeError):
            return
        if token:
            self._token = token

    async def _ensure_token(self) -> str:
        """إرجاع توكن صالح — يجلبه من search-suggest عند أول حاجة (كسول)."""
        if self._token:
            return self._token
        async with self._token_lock:  # منع جلب متزامن مكرر
            if self._token:
                return self._token
            await self._refresh_token()
        assert self._token is not None
        return self._token

    async def _refresh_token(self) -> None:
        """تجديد التوكن عبر search-suggest (بدون Bearer) — يرمي عند الفشل."""
        self._token = None
        # الـ API يخزّن التوكن أيضاً في cookie باسم 'token' — طالما الكوكي صالحاً
        # لا يرد هيدر x-user في search-suggest (موثق حياً) → نمسح الجرة أولاً
        self._client.cookies.clear()
        resp = await self._request(
            "POST",
            "/subject/search-suggest",
            json_body={"keyword": "x", "perPage": 10},
        )
        if resp.status_code in (400, 401) or not self._token:
            raise httpx.HTTPStatusError(
                f"فشل جلب توكن موفي بوكس ({resp.status_code})",
                request=resp.request,
                response=resp,
            )

    async def _post_search(self, body: dict) -> dict:
        """POST /subject/search بـ Bearer؛ يجدّد التوكن مرة واحدة عند 400/401."""
        await self._ensure_token()
        for refreshed in (False, True):
            resp = await self._request(
                "POST",
                "/subject/search",
                json_body=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code in (400, 401):
                if refreshed:
                    resp.raise_for_status()
                await self._refresh_token()
                continue
            return resp.json()
        raise RuntimeError("unreachable")

    async def _get_data(
        self, path: str, *, params: dict | None = None, headers: dict | None = None
    ) -> dict:
        """GET وإرجاع حقل data من الرد الموحّد {"code":0,"message":"ok","data":{..}}."""
        resp = await self._request("GET", path, params=params, headers=headers)
        payload = resp.json()
        if not isinstance(payload, dict):
            return {}
        return payload.get("data") or {}

    # --------------------------------------------------------------- تحويل
    def _to_search_result(self, item: dict) -> SearchResult | None:
        """تحويل عنصر بحث/رائج → SearchResult (عقد SPEC3 3.2)."""
        subject_id = item.get("subjectId")
        if subject_id is None:
            return None
        detail_path = item.get("detailPath") or ""
        subject_type = item.get("subjectType")
        typ = "series" if subject_type == 2 else "movie"  # 1=فيلم، غيرها→movie
        cover = item.get("cover") or {}
        return SearchResult(
            id=int(subject_id),
            type=typ,
            title=item.get("title") or "",
            url=f"{self.base_url}/detail/{detail_path}",
            poster=cover.get("url") or None,
            year=_year(item.get("releaseDate")),
            rating=_float_or_none(item.get("imdbRatingValue")),
        )

    # ----------------------------------------------------------------- بحث
    async def search(self, query: str, page: int = 1) -> list[SearchResult]:
        """بحث (Bearer كسول) — perPage=10 دائماً (أقل قد يرجع فارغاً — خلل موثق)."""
        payload = await self._post_search(
            {"keyword": query, "page": page, "perPage": 10, "subjectType": 0}
        )
        data = payload.get("data") or {}
        results: list[SearchResult] = []
        for item in data.get("items") or []:
            result = self._to_search_result(item)
            if result is not None:
                results.append(result)
        return results

    async def trending(self, page: int = 1) -> list[SearchResult]:
        """الرائج (بدون Bearer) — نفس تحويل البحث."""
        data = await self._get_data(
            "/subject/trending", params={"page": page, "perPage": 18}
        )
        results: list[SearchResult] = []
        for item in data.get("subjectList") or data.get("items") or []:
            result = self._to_search_result(item)
            if result is not None:
                results.append(result)
        return results

    # --------------------------------------------------------------- تفاصيل
    async def get_details(self, detail_path: str) -> MbDetails:
        """تفاصيل عنوان/وصف عربي (X-Request-Lang: ar) + مواسم + نسخ/dubs."""
        data = await self._get_data(
            "/detail",
            params={"detailPath": detail_path},
            headers={"X-Request-Lang": "ar"},
        )
        subject = data.get("subject")
        if not subject:
            raise NotFoundError(f"تفاصيل غير موجودة: {detail_path}")

        dubs: list[MbDub] = []
        for d in subject.get("dubs") or []:
            dubs.append(
                MbDub(
                    name=d.get("lanName") or d.get("name") or "",
                    lan_code=d.get("lanCode") or "",
                    type=int(d.get("type") or 0),
                    subject_id=str(d.get("subjectId") or ""),
                    detail_path=d.get("detailPath") or "",
                    original=bool(d.get("original")),
                )
            )

        seasons: list[MbSeason] = []
        resource = data.get("resource") or {}
        for s in resource.get("seasons") or []:
            se = _int_or_none(s.get("se"))
            if not se:
                continue  # se=0 عنصر وهمي يرد للأفلام — المواسم تبدأ من 1
            # allEp نص مفصول بفواصل → list[int]؛ فارغ/مفقود → None (الكل متاح)
            all_ep_raw = (s.get("allEp") or "").strip()
            all_ep: list[int] | None = None
            if all_ep_raw:
                all_ep = sorted(
                    int(part) for part in all_ep_raw.split(",") if part.strip().isdigit()
                )
            resolutions = sorted(
                r
                for r in (
                    _int_or_none(res.get("resolution"))
                    for res in s.get("resolutions") or []
                )
                if r is not None
            )
            seasons.append(
                MbSeason(
                    se=se,
                    max_ep=int(s.get("maxEp") or 0),
                    all_ep=all_ep,
                    resolutions=resolutions,
                )
            )

        subtitles_raw = subject.get("subtitles") or ""
        subtitle_languages = [p.strip() for p in subtitles_raw.split(",") if p.strip()]
        genre_raw = subject.get("genre") or ""
        genres = [g.strip() for g in genre_raw.split(",") if g.strip()]
        cover = subject.get("cover") or {}

        return MbDetails(
            subject_id=str(subject.get("subjectId") or ""),
            detail_path=subject.get("detailPath") or detail_path,
            type="series" if subject.get("subjectType") == 2 else "movie",
            title=subject.get("title") or "",
            description=subject.get("description") or None,
            year=_year(subject.get("releaseDate")),
            rating=_float_or_none(subject.get("imdbRatingValue")),
            poster=cover.get("url") or None,
            genres=genres,
            dubs=dubs,
            seasons=seasons,
            subtitle_languages=subtitle_languages,
        )

    # --------------------------------------------------------------- الجودات
    @staticmethod
    def _play_headers() -> dict:
        """هيدرز play/caption: Origin إلزامي — وممنوع Authorization/X-Client-Token."""
        return {
            "Origin": PLAY_ORIGIN,
            "Referer": VIDEO_REFERER,
            "X-Client-Info": json.dumps({"timezone": "Africa/Cairo"}),
        }

    async def get_streams(
        self, subject_id: str, detail_path: str, se: int = 0, ep: int = 0
    ) -> MbStreams:
        """جودات MP4 (مرتبة تنازلياً، بدون مقفلة/فارغة) + ترجمات SRT.

        أفلام: se=0&ep=0. الترجمات: حقل captions في play يرد فارغاً حالياً —
        المصدر الفعلي ``GET /subject/caption?id=<stream_id>&subjectId=..&se=..&ep=..``
        (نفس الترجمات لكل السترمات — موثق حياً). اجلب عند الطلب: الروابط تنتهي.
        """
        data = await self._get_data(
            "/subject/play",
            params={
                "subjectId": subject_id,
                "se": se,
                "ep": ep,
                "detailPath": detail_path,
            },
            headers=self._play_headers(),
        )

        qualities: list[MbQuality] = []
        first_stream_id: str | None = None
        for s in data.get("streams") or []:
            url = s.get("url")
            if s.get("vipLocked") or not url:
                continue  # استبعاد المقفل VIP والروابط الفارغة
            resolution = _int_or_none(s.get("resolutions"))
            if resolution is None:
                continue
            if first_stream_id is None:
                first_stream_id = str(s.get("id") or "") or None
            qualities.append(
                MbQuality(
                    resolution=resolution,
                    url=url,
                    size=_int_or_none(s.get("size")),
                    duration=_int_or_none(s.get("duration")),
                    vip_locked=False,
                )
            )
        qualities.sort(key=lambda q: q.resolution, reverse=True)

        captions = self._parse_captions(data.get("captions"))
        if not captions and first_stream_id:
            captions = await self._fetch_captions(first_stream_id, subject_id, se, ep)
        return MbStreams(qualities=qualities, captions=captions)

    async def _fetch_captions(
        self, stream_id: str, subject_id: str, se: int, ep: int
    ) -> list[MbCaption]:
        """ترجمات من /subject/caption — فشلها متسامح (يرجع قائمة فارغة)."""
        try:
            data = await self._get_data(
                "/subject/caption",
                params={"id": stream_id, "subjectId": subject_id, "se": se, "ep": ep},
                headers=self._play_headers(),
            )
        except Exception:
            return []
        return self._parse_captions(data.get("captions"))

    @staticmethod
    def _parse_captions(raw) -> list[MbCaption]:
        """تحويل captions الخام → MbCaption (استبعاد الروابط الفارغة)."""
        captions: list[MbCaption] = []
        for c in raw or []:
            url = c.get("url")
            lan = c.get("lan")
            if not url or not lan:
                continue
            captions.append(
                MbCaption(
                    lan=lan,
                    lan_name=c.get("lanName") or lan,
                    url=url,
                    size=c.get("size") or None,
                    delay=int(c.get("delay") or 0),
                )
            )
        return captions
