"""اختبارات حية لحزمة moviebox على الـ API الفعلي h5-api.aoneroom.com.

تغطي: بحث عربي/إنجليزي، الرائج، تفاصيل فيلم (dubs)، تفاصيل مسلسل (seasons/all_ep)،
get_streams لفيلم (جودات مرتبة تنازلياً + captions فيها ar) ولحلقة مسلسل، سلوك
الـ CDN مع/بدون Referer (موثق في info-moviebox.md)، وتجديد التوكن الكسول تلقائياً.
"""

import httpx
import pytest
import pytest_asyncio

from akwam.models import SearchResult
from moviebox import (
    VIDEO_REFERER,
    MbDetails,
    MbStreams,
    MovieboxClient,
)

ONE_PIECE_PATH = "one-piece-CTqWaizwOp3"   # مسلسل (موثق حياً)
AVATAR_PATH = "avatar-WLDIi21IUBa"         # فيلم بجودات 360-1080 وترجمات منها ar
VALID_RESOLUTIONS = {360, 480, 720, 1080}


@pytest_asyncio.fixture
async def client():
    """عميل جديد لكل اختبار مع إغلاق مضمون."""
    c = MovieboxClient()
    try:
        yield c
    finally:
        await c.close()


def _assert_search_result(r: SearchResult) -> None:
    assert isinstance(r.id, int) and r.id > 0
    assert r.type in ("movie", "series")
    assert r.title
    assert r.url.startswith("https://themoviebox.xyz/detail/")
    assert r.poster and r.poster.startswith("http")


# --------------------------------------------------------------------- بحث
@pytest.mark.asyncio
async def test_search_english(client):
    results = await client.search("one piece")
    assert results, "بحث one piece يجب أن يرجع نتايج"
    for r in results:
        _assert_search_result(r)
    top = results[0]
    assert "one piece" in top.title.lower()
    assert top.type == "series"
    assert top.rating is not None and 0 < top.rating <= 10


@pytest.mark.asyncio
async def test_search_arabic(client):
    results = await client.search("ون بيس")
    assert results, "البحث العربي 'ون بيس' يجب أن يرجع نتايج"
    for r in results:
        _assert_search_result(r)
    assert any("one piece" in r.title.lower() for r in results)


# -------------------------------------------------------------------- الرائج
@pytest.mark.asyncio
async def test_trending(client):
    results = await client.trending()
    assert results, "الرائج يجب أن يرجع نتايج"
    for r in results:
        _assert_search_result(r)


# ------------------------------------------------------------------ تفاصيل
@pytest.mark.asyncio
async def test_details_movie(client):
    d = await client.get_details(AVATAR_PATH)
    assert isinstance(d, MbDetails)
    assert d.type == "movie"
    assert d.title  # X-Request-Lang: ar → 'أفاتار'
    assert d.year == 2009
    assert d.rating is not None and 0 < d.rating <= 10
    assert d.subject_id and d.detail_path == AVATAR_PATH
    assert d.seasons == []  # فارغة للأفلام
    if d.dubs:  # النسخ (لو موجودة): Original Audio + Arabic sub + دبلجات
        original = [x for x in d.dubs if x.original]
        assert original, "يجب وجود نسخة أصلية ضمن dubs"
        for x in d.dubs:
            assert x.name and x.lan_code
            assert x.subject_id and x.detail_path
            assert x.type in (0, 1)


@pytest.mark.asyncio
async def test_details_series(client):
    d = await client.get_details(ONE_PIECE_PATH)
    assert d.type == "series"
    assert d.title  # 'ون بيس' بالعربية
    assert d.year == 1999
    assert d.seasons, "مسلسل ون بيس يجب أن يرجع مواسم"
    s1 = d.seasons[0]
    assert s1.se == 1
    assert s1.max_ep > 0
    # allEp فارغ/مفقود → None (كل الحلقات متاحة)
    assert s1.all_ep is None
    assert s1.resolutions and all(r in VALID_RESOLUTIONS for r in s1.resolutions)


# ------------------------------------------------------------------ الجودات
@pytest.mark.asyncio
async def test_streams_movie(client):
    d = await client.get_details(AVATAR_PATH)
    streams = await client.get_streams(d.subject_id, d.detail_path, se=0, ep=0)
    assert isinstance(streams, MbStreams)
    assert streams.qualities, "فيلم يجب أن يرجع جودات"
    resolutions = [q.resolution for q in streams.qualities]
    assert resolutions == sorted(resolutions, reverse=True), "ترتيب تنازلي بالدقة"
    assert set(resolutions) <= VALID_RESOLUTIONS
    for q in streams.qualities:
        assert not q.vip_locked, "الجودات المقفلة VIP مستبعدة"
        assert q.url.startswith("http")
        assert q.size is None or q.size > 0
    lans = [c.lan for c in streams.captions]
    assert "ar" in lans, f"ترجمة عربية مطلوبة ضمن captions، ورد: {lans}"
    ar = next(c for c in streams.captions if c.lan == "ar")
    assert ar.url.startswith("http") and ar.lan_name


@pytest.mark.asyncio
async def test_streams_episode(client):
    d = await client.get_details(ONE_PIECE_PATH)
    streams = await client.get_streams(d.subject_id, d.detail_path, se=1, ep=1)
    assert streams.qualities, "حلقة مسلسل يجب أن ترجع جودات"
    resolutions = [q.resolution for q in streams.qualities]
    assert resolutions == sorted(resolutions, reverse=True)
    assert set(resolutions) <= VALID_RESOLUTIONS
    assert all(not q.vip_locked for q in streams.qualities)
    lans = [c.lan for c in streams.captions]
    assert lans, "حلقة مسلسل يجب أن ترجع ترجمات"
    assert "ar" in lans


# ---------------------------------------------------------------- سلوك CDN
@pytest.mark.asyncio
async def test_cdn_referer_required(client):
    """رابط mp4: HEAD مع Referer → 200/206، بدون Referer → 429 (موثق)."""
    d = await client.get_details(AVATAR_PATH)
    streams = await client.get_streams(d.subject_id, d.detail_path, se=0, ep=0)
    url = streams.qualities[0].url
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        with_ref = await http.head(url, headers={**headers, "Referer": VIDEO_REFERER})
        without_ref = await http.head(url, headers=headers)
    assert with_ref.status_code in (200, 206), (
        f"مع Referer متوقع 200/206، ورد {with_ref.status_code}"
    )
    # النتيجة الفعلية الموثقة: 429 بدون Referer
    assert without_ref.status_code == 429, (
        f"بدون Referer موثق 429، ورد فعلياً {without_ref.status_code}"
    )


# ------------------------------------------------------------- تجديد التوكن
@pytest.mark.asyncio
async def test_token_lazy_refresh(client):
    results = await client.search("one piece")
    assert results
    assert client._token, "التوكن يجب أن يُخزن بعد أول بحث"
    client._token = None  # مسح التوكن المخزن
    results = await client.search("one piece")
    assert results, "search يجب أن يسترجع التوكن تلقائياً بعد مسحه"
    assert client._token
    # توكن فاسد → تجديد مرة واحدة وإعادة المحاولة
    client._token = "invalid-token"
    results = await client.search("one piece")
    assert results, "search يجب أن يجدد التوكن الفاسد ويعيد المحاولة"
    assert client._token != "invalid-token"
