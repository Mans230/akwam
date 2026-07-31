"""اختبارات حية لحزمة starcima على الموقع الفعلي https://starcima.com.

تغطي: بحث عربي/إنجليزي، تفاصيل فيلم، مسلسل بمواسم/حلقات، سيرفرات حقيقية
مع تصنيف provider وترتيب القابل أولاً، extract لسيرفر قابل (mp4/hls + proxy_url)
وسيرفر other (None بدون استثناء)، ترجمات SRT، والمحتوى المدبلج.
"""

import pytest
import pytest_asyncio

from akwam.models import SearchResult
from starcima import (
    ExtractedStream,
    NotFoundError,
    ScEpisode,
    ScMediaDetails,
    ServerLink,
    StarcimaClient,
)

BASE_URL = "https://starcima.com"
VALID_PROVIDERS = {"akwam", "streamwish", "updown", "vidtube", "other"}


@pytest_asyncio.fixture
async def client():
    """عميل جديد لكل اختبار مع إغلاق مضمون."""
    c = StarcimaClient(base_url=BASE_URL)
    try:
        yield c
    finally:
        await c.close()


# --------------------------------------------------------------------- بحث
@pytest.mark.asyncio
async def test_search_english(client):
    results = await client.search("one piece")
    assert results, "بحث one piece يجب أن يرجع نتايج"
    for r in results:
        assert isinstance(r, SearchResult)
        assert isinstance(r.id, int) and r.id > 0
        assert r.title
        assert r.type in ("movie", "series")
        assert r.url.startswith(f"{BASE_URL}/media/")
    assert any(r.type == "series" for r in results)
    first = results[0]
    assert first.poster and first.poster.startswith("https://image.tmdb.org/t/p/w500")
    assert first.rating is not None and first.rating > 0
    assert all(r.type in ("movie", "series") for r in results)  # لا person


@pytest.mark.asyncio
async def test_search_arabic(client):
    results = await client.search("ولاد رزق")
    assert results, "بحث ولاد رزق يجب أن يرجع نتايج"
    movies = [r for r in results if r.type == "movie"]
    assert movies
    first = movies[0]
    assert "ولاد رزق" in first.title
    assert first.year and first.year >= 2015
    assert first.poster and first.poster.startswith("https://image.tmdb.org/")
    assert first.rating is not None


# ------------------------------------------------------------------- تفاصيل
@pytest.mark.asyncio
async def test_get_media_movie(client):
    results = await client.search("ولاد رزق")
    movie = next(r for r in results if r.type == "movie")
    details = await client.get_media(movie.id, "movie")
    assert isinstance(details, ScMediaDetails)
    assert details.id == movie.id
    assert details.type == "movie"
    assert details.title_ar and "ولاد رزق" in details.title_ar
    assert details.title_en
    assert details.year and details.year >= 2015
    assert details.rating is not None and details.rating > 0
    assert details.description
    assert details.poster and details.poster.startswith("https://image.tmdb.org/t/p/w500")
    assert details.seasons == []


@pytest.mark.asyncio
async def test_get_media_series_and_episodes(client):
    # ون بيس (الأنمي) tmdb 37854 — مسلسل متعدد المواسم
    details = await client.get_media(37854, "series")
    assert details.type == "series"
    assert details.title_ar
    assert details.title_en
    assert details.year == 1999
    assert details.seasons, "المسلسل يجب أن يحتوي مواسم"
    assert all(s.number >= 1 for s in details.seasons)  # specials (0) مستبعدة
    assert all(s.episode_count > 0 for s in details.seasons)

    episodes = await client.get_episodes(37854, 1)
    assert episodes, "الموسم الأول يجب أن يحتوي حلقات"
    numbers = [e.number for e in episodes]
    assert numbers == sorted(numbers) and 1 in numbers
    for e in episodes:
        assert isinstance(e, ScEpisode)
        assert e.season == 1
        assert e.title
    assert episodes[0].thumb and episodes[0].thumb.startswith(
        "https://image.tmdb.org/t/p/w300"
    )


# ------------------------------------------------------------------ سيرفرات
@pytest.mark.asyncio
async def test_get_servers_classification_and_order(client):
    # ولاد رزق 3: القاضية — موثق أنه يرجع سيرفرات متنوعة
    servers = await client.get_servers(
        title_ar="ولاد رزق 3: القاضية",
        title_en="ولاد رزق 3: القاضية",
        year=2024,
        type="movie",
    )
    assert servers, "يجب أن يرجع سيرفرات"
    for s in servers:
        assert isinstance(s, ServerLink)
        assert s.name
        assert s.embed_url.startswith("http")
        assert s.provider in VALID_PROVIDERS
        assert s.is_akwam == (s.provider == "akwam")
        if s.provider in ("akwam", "streamwish", "updown", "vidtube"):
            assert s.downloadable
        else:
            assert not s.downloadable
    # الترتيب: كل القابل للتحميل قبل غير القابل
    flags = [s.downloadable for s in servers]
    assert flags == sorted(flags, reverse=True)
    # موثق حياً: hlswish ظهر ضمن السيرفرات → streamwish قابل
    assert any(s.provider == "streamwish" for s in servers)


@pytest.mark.asyncio
async def test_get_servers_series_params(client):
    # تمرير season/episode/absEpisode فقط عند إعطائها — لا خطأ وبنية صحيحة
    servers = await client.get_servers(
        title_ar="ون بيس",
        title_en="One Piece",
        year=1999,
        type="series",
        season=1,
        episode=1,
        abs_episode=1,
        season_ep_count=61,
    )
    assert isinstance(servers, list)
    for s in servers:
        assert s.provider in VALID_PROVIDERS


# ------------------------------------------------------------------ استخراج
async def _find_downloadable_servers(client) -> list[ServerLink]:
    """يجمع سيرفرات قابلة للتحميل من عناوين موثقة حياً."""
    queries = [
        ("ولاد رزق 3: القاضية", "ولاد رزق 3: القاضية", 2024, "movie"),
        ("ذا باتمان", "The Batman", 2022, "movie"),
        ("جون ويك: الفصل 4", "John Wick: Chapter 4", 2023, "movie"),
    ]
    found: list[ServerLink] = []
    for title_ar, title_en, year, typ in queries:
        try:
            servers = await client.get_servers(title_ar, title_en, year, typ)
        except Exception:
            continue
        found.extend(s for s in servers if s.downloadable and not s.is_akwam)
    return found


@pytest.mark.asyncio
async def test_extract_downloadable_server(client):
    servers = await _find_downloadable_servers(client)
    assert servers, "يجب إيجاد سيرفر قابل للاستخراج على الأقل"
    stream = None
    for s in servers:
        stream = await client.extract(s.embed_url)
        if stream is not None:
            break
    assert stream is not None, "extract يجب أن ينجح مع سيرفر streamwish/updown/vidtube واحد على الأقل"
    assert isinstance(stream, ExtractedStream)
    assert stream.kind in ("mp4", "hls")
    assert stream.direct_url.startswith("http")
    if stream.kind == "hls":
        assert stream.proxy_url, "الـ hls يجب أن يكون له proxy_url"
        assert stream.proxy_url.startswith(BASE_URL)


@pytest.mark.asyncio
async def test_extract_other_server_returns_none(client):
    servers = await client.get_servers(
        title_ar="ولاد رزق 3: القاضية",
        title_en="ولاد رزق 3: القاضية",
        year=2024,
        type="movie",
    )
    others = [s for s in servers if s.provider == "other"]
    assert others, "يجب وجود سيرفر other ضمن النتايج"
    # لا يرمي استثناء إطلاقاً — يرجع None أو (نادراً) نجاح؛ المهم عدم الاستثناء
    result = await client.extract(others[0].embed_url)
    assert result is None or isinstance(result, ExtractedStream)
    # سيرفر معروف بالفشل (voe) → None قاطع بدون استثناء
    voe = next((s for s in others if "voe" in s.embed_url), None)
    if voe is not None:
        assert await client.extract(voe.embed_url) is None


# ------------------------------------------------------------------- ترجمات
@pytest.mark.asyncio
async def test_get_subtitles(client):
    # The Batman (414906) — موثق حياً أن له ترجمات عربية SRT
    subs = await client.get_subtitles(414906)
    assert isinstance(subs, list)
    assert subs, "ذا باتمان له ترجمات عربية موثقة"
    assert all(isinstance(u, str) and u.startswith("http") for u in subs)


@pytest.mark.asyncio
async def test_get_subtitles_series_no_error(client):
    # مسلسل معروف — قد تكون فارغة؛ المهم لا خطأ وبنية قائمة نصية صحيحة
    subs = await client.get_subtitles(37854, season=1, episode=1)
    assert isinstance(subs, list)
    assert all(isinstance(u, str) for u in subs)


# -------------------------------------------------------------------- مدبلج
@pytest.mark.asyncio
async def test_search_dubbed_and_episodes(client):
    results = await client.search_dubbed("سبونج")
    assert results, "بحث مدبلج 'سبونج' يجب أن يرجع نتايج"
    first = results[0]
    assert isinstance(first, SearchResult)
    assert first.type == "dubbed"
    assert first.title.startswith("(مدبلج)")
    assert first.url.startswith(f"{BASE_URL}/dubbed/watch")
    assert first.poster and first.poster.startswith("http")

    episodes = await client.get_dubbed_episodes(str(first.id))
    assert episodes, "يجب أن يرجع حلقات مدبلجة"
    numbers = [e.number for e in episodes]
    assert numbers == sorted(numbers) and 1 in numbers
    for e in episodes:
        assert isinstance(e, ScEpisode)
        assert e.title
    assert episodes[0].thumb is None or episodes[0].thumb.startswith("http")


# --------------------------------------------------------------------- أخطاء
@pytest.mark.asyncio
async def test_get_media_not_found(client):
    with pytest.raises(NotFoundError):
        await client.get_media(1, "movie")
