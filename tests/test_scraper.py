"""اختبارات حية لحزمة akwam على الموقع الفعلي https://akwam.it.

تغطي: البحث، تفاصيل فيلم + الجودات، تجميع المواسم + حلقات مسلسل،
تفاصيل حلقة، الروابط المباشرة من /watch، وfallback صفحة /download، و404.
"""

import re

import pytest
import pytest_asyncio

from akwam import (
    AkwamClient,
    DirectLink,
    EpisodeDetails,
    MovieDetails,
    NotFoundError,
    SeriesDetails,
)

BASE_URL = "https://akwam.it"


@pytest_asyncio.fixture
async def client():
    """عميل جديد لكل اختبار مع إغلاق مضمون."""
    c = AkwamClient(base_url=BASE_URL)
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_search_returns_movies_and_series(client: AkwamClient):
    """بحث 'one piece' يرجع نتايج فيها movie وseries ببيانات صحيحة."""
    results = await client.search("one piece")
    assert results, "لا نتايج للبحث"
    types = {r.type for r in results}
    assert "movie" in types and "series" in types
    for r in results:
        assert r.id > 0
        assert r.title.strip()
        assert re.search(rf"/{r.type}/{r.id}(/|$)", r.url)


@pytest.mark.asyncio
async def test_get_movie_with_qualities(client: AkwamClient):
    """فيلم حقيقي من نتايج البحث: عنوان وqualities مكتملة الحقول."""
    results = await client.search("one piece")
    movie_res = next(r for r in results if r.type == "movie")
    movie = await client.get_movie(movie_res.id)
    assert isinstance(movie, MovieDetails)
    assert movie.id == movie_res.id
    assert movie.title.strip()
    assert movie.qualities, "لا جودات للفيلم"
    for q in movie.qualities:
        assert q.file_id > 0
        assert q.content_id == movie_res.id
        assert q.quality.strip()
        assert f"/watch/{q.file_id}/{q.content_id}" in q.watch_url
        assert f"/download/{q.file_id}/{q.content_id}" in q.download_url
        assert q.size  # الحجم موجود في صفحة الفيلم


@pytest.mark.asyncio
async def test_search_seasons_and_series_episodes(client: AkwamClient):
    """search_seasons يرجع مسلسلات فقط، وget_series يرجع حلقات مرتبة."""
    seasons = await client.search_seasons("one piece")
    assert seasons, "لا نتايج مسلسلات"
    assert all(s.type == "series" for s in seasons)
    series = await client.get_series(seasons[0].id)
    assert isinstance(series, SeriesDetails)
    assert series.id == seasons[0].id
    assert series.title.strip()
    assert series.episodes, "لا حلقات للمسلسل"
    numbers = [e.number for e in series.episodes]
    assert numbers == sorted(numbers), "الحلقات غير مرتبة"
    assert all(n > 0 for n in numbers)
    for e in series.episodes[:5]:
        assert e.id > 0
        assert f"/episode/{e.id}" in e.url


@pytest.mark.asyncio
async def test_get_episode(client: AkwamClient):
    """حلقة حقيقية: رقم صحيح وqualities مكتملة وseries_id موجود."""
    seasons = await client.search_seasons("one piece")
    series = await client.get_series(seasons[0].id)
    ep_ref = series.episodes[0]
    ep = await client.get_episode(ep_ref.id)
    assert isinstance(ep, EpisodeDetails)
    assert ep.id == ep_ref.id
    assert ep.number == ep_ref.number
    assert ep.series_id == seasons[0].id
    assert ep.qualities, "لا جودات للحلقة"
    for q in ep.qualities:
        assert q.file_id > 0 and q.content_id > 0
        assert f"/watch/{q.file_id}/{q.content_id}" in q.watch_url


@pytest.mark.asyncio
async def test_get_direct_links_and_resolve_download(client: AkwamClient):
    """روابط مباشرة من /watch بدقة صحيحة + fallback صفحة /download."""
    results = await client.search("one piece")
    movie_res = next(r for r in results if r.type == "movie")
    movie = await client.get_movie(movie_res.id)
    q = movie.qualities[0]

    links = await client.get_direct_links(q.file_id, q.content_id)
    assert links, "لا روابط مباشرة من صفحة /watch"
    for link in links:
        assert isinstance(link, DirectLink)
        assert "downet.net/download/" in link.url
        assert re.fullmatch(r"\d{3,4}p", link.quality), link.quality
        assert link.filename and link.filename.endswith(".mp4")

    fallback = await client.resolve_download(q.file_id, q.content_id)
    assert fallback is not None
    assert "downet.net/download/" in fallback.url


@pytest.mark.asyncio
async def test_not_found_raises(client: AkwamClient):
    """معرّف غير موجود يرمي NotFoundError."""
    with pytest.raises(NotFoundError):
        await client.get_movie(999999999)
