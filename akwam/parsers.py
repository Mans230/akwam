"""دوال parse خالصة: تحويل نص HTML من موقع أكوام إلى dataclasses.

لا يوجد أي وصول للشبكة هنا — كل دالة تأخذ نص HTML (وأحياناً base_url)
وتُرجع كائنات من ``akwam.models``. الـ selectors موثقة في info.md.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import (
    DirectLink,
    Episode,
    EpisodeDetails,
    MovieDetails,
    QualityLink,
    SearchResult,
    SeriesDetails,
)

# /watch/{file_id}/{content_id}/... أو /download/{file_id}/{content_id}/...
_IDS_RE = re.compile(r"/(?:watch|download)/(\d+)/(\d+)")
# /movie/{id}/... أو /series/{id}/... أو /episode/{id}/...
_CONTENT_RE = re.compile(r"/(movie|series|episode)/(\d+)")
# رقم الحلقة من الرابط: /الحلقة-11049
_EP_NUM_RE = re.compile(r"/الحلقة-(\d+)")
# التقييم بصيغة "10 / 6.6"
_RATING_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
# الدقة من اسم الملف أو قيمة size
_QUALITY_RE = re.compile(r"(\d{3,4})p?", re.IGNORECASE)


def _soup(html: str) -> BeautifulSoup:
    """إنشاء كائن BeautifulSoup بمحلل lxml."""
    return BeautifulSoup(html, "lxml")


def _abs(base_url: str, href: str) -> str:
    """تحويل رابط نسبي إلى مطلق بالاعتماد على base_url."""
    return urljoin(base_url.rstrip("/") + "/", href)


def _text(tag: Tag | None) -> str | None:
    """نص العنصر مجرداً من المسافات، أو None."""
    if tag is None:
        return None
    txt = tag.get_text(" ", strip=True)
    return txt or None


def _parse_int(value: str | None) -> int | None:
    """استخراج أول رقم صحيح من نص، أو None."""
    if not value:
        return None
    m = re.search(r"\d+", value)
    return int(m.group()) if m else None


def _parse_float(value: str | None) -> float | None:
    """استخراج أول رقم عشري من نص، أو None."""
    if not value:
        return None
    m = re.search(r"\d+(?:\.\d+)?", value)
    return float(m.group()) if m else None


def _extract_ids(url: str) -> tuple[int, int] | None:
    """استخراج (file_id, content_id) من رابط /watch أو /download."""
    m = _IDS_RE.search(url)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    """قراءة content من وسم meta[property=...]."""
    tag = soup.select_one(f'meta[property="{prop}"]')
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return None


def _parse_rating(soup: BeautifulSoup) -> float | None:
    """التقييم من النص بجانب أيقونة النجمة بصيغة '10 / 6.6'."""
    star = soup.select_one("i.icon-star")
    container: Tag | None = star.parent if star else None
    txt = container.get_text(" ", strip=True) if container else ""
    m = _RATING_RE.search(txt)
    if m:
        return float(m.group(2))
    return _parse_float(txt)


def _parse_year(soup: BeautifulSoup) -> int | None:
    """السنة من أول span.badge.badge-secondary يحمل 4 أرقام."""
    for badge in soup.select("span.badge.badge-secondary"):
        txt = badge.get_text(strip=True)
        m = re.search(r"(19|20)\d{2}", txt)
        if m:
            return int(m.group())
    return None


def _parse_qualities(soup: BeautifulSoup, base_url: str) -> list[QualityLink]:
    """تجميع الجودات من تبويبات ul.header-tabs المطابقة لـ div.tab-content.quality.

    النص في التبويب = اسم الجودة (مثل 1080p)، وhref مثل ``#tab-5`` يطابق
    ``div.tab-content.quality#tab-5`` الذي بداخله أزرار المشاهدة/التحميل والحجم.
    """
    qualities: list[QualityLink] = []
    seen: set[tuple[str, int, int]] = set()
    for tab in soup.select("ul.header-tabs li a"):
        quality_name = tab.get_text(strip=True)
        href = tab.get("href") or ""
        tab_id = href.lstrip("#")
        if not quality_name or not tab_id:
            continue
        content = soup.select_one(f"div.tab-content.quality#{tab_id}")
        if content is None:
            # احتياط: id قد يكون على عنصر آخر بنفس الاسم
            content = soup.find("div", id=tab_id)
        if content is None:
            continue
        for row in content.select("div[data-server][data-quality]") or [content]:
            show = row.select_one("a.link-btn.link-show")
            download = row.select_one("a.link-btn.link-download")
            link = download or show
            if link is None or not link.get("href"):
                continue
            ids = _extract_ids(str(link["href"]))
            if ids is None and show is not None and show.get("href"):
                ids = _extract_ids(str(show["href"]))
            if ids is None:
                continue
            file_id, content_id = ids
            key = (quality_name, file_id, content_id)
            if key in seen:
                continue
            seen.add(key)
            watch_href = str(show["href"]) if show and show.get("href") else None
            dl_href = str(download["href"]) if download and download.get("href") else None
            if watch_href is None and dl_href is not None:
                watch_href = dl_href.replace("/download/", "/watch/")
            if dl_href is None and watch_href is not None:
                dl_href = watch_href.replace("/watch/", "/download/")
            if watch_href is None or dl_href is None:
                continue
            size_tag = row.select_one("span.font-size-14.mr-auto")
            qualities.append(
                QualityLink(
                    quality=quality_name,
                    file_id=file_id,
                    content_id=content_id,
                    watch_url=_abs(base_url, watch_href),
                    download_url=_abs(base_url, dl_href),
                    size=_text(size_tag),
                )
            )
    return qualities


def parse_search_results(html: str, base_url: str) -> list[SearchResult]:
    """تحويل صفحة البحث /search إلى قائمة SearchResult."""
    soup = _soup(html)
    results: list[SearchResult] = []
    for box in soup.select("div.entry-box"):
        title_a = box.select_one("h3.entry-title a")
        if title_a is None or not title_a.get("href"):
            continue
        url = _abs(base_url, str(title_a["href"]))
        m = _CONTENT_RE.search(urlparse(url).path)
        if m is None:
            continue
        entry_type, entry_id = m.group(1), int(m.group(2))
        img = box.select_one(".entry-image img")
        poster = None
        if img is not None:
            poster = img.get("data-src") or img.get("src")
            if poster:
                poster = str(poster)
        results.append(
            SearchResult(
                id=entry_id,
                type=entry_type,
                title=title_a.get_text(strip=True),
                url=url,
                poster=poster,
                year=_parse_year(box),
                rating=_parse_float(_text(box.select_one("span.label.rating"))),
                quality=_text(box.select_one("span.label.quality")),
            )
        )
    return results


def parse_movie_details(html: str, movie_id: int, base_url: str) -> MovieDetails:
    """تحويل صفحة فيلم /movie/{id} إلى MovieDetails مع كل الجودات."""
    soup = _soup(html)
    return MovieDetails(
        id=movie_id,
        title=_text(soup.select_one("h1.entry-title")) or "",
        poster=_meta_content(soup, "og:image"),
        year=_parse_year(soup),
        rating=_parse_rating(soup),
        description=_meta_content(soup, "og:description"),
        qualities=_parse_qualities(soup, base_url),
    )


def parse_series_details(html: str, series_id: int, base_url: str) -> SeriesDetails:
    """تحويل صفحة مسلسل /series/{id} إلى SeriesDetails مع الحلقات مرتبة."""
    soup = _soup(html)
    episodes: list[Episode] = []
    container = soup.select_one("div#series-episodes") or soup
    for row in container.select("div.bg-primary2"):
        a = row.select_one("h2 a")
        if a is None or not a.get("href"):
            continue
        url = _abs(base_url, str(a["href"]))
        m = _CONTENT_RE.search(urlparse(url).path)
        if m is None or m.group(1) != "episode":
            continue
        num_m = _EP_NUM_RE.search(urlparse(url).path)
        number = int(num_m.group(1)) if num_m else (_parse_int(a.get_text()) or 0)
        img = row.select_one("img")
        thumb = str(img["src"]) if img is not None and img.get("src") else None
        episodes.append(
            Episode(
                id=int(m.group(2)),
                number=number,
                title=a.get_text(strip=True),
                url=url,
                thumb=thumb,
            )
        )
    episodes.sort(key=lambda e: e.number)
    return SeriesDetails(
        id=series_id,
        title=_text(soup.select_one("h1.entry-title")) or "",
        poster=_meta_content(soup, "og:image"),
        year=_parse_year(soup),
        rating=_parse_rating(soup),
        description=_meta_content(soup, "og:description"),
        episodes=episodes,
    )


def _episode_nav_id(h3: Tag) -> int | None:
    """استخراج id الحلقة من رابط يحيط بعنوان h3 (التالية/السابقة)."""
    a = h3.find_parent("a", href=True)
    if a is None:
        a = h3.find("a", href=True)
    if a is None:
        return None
    href = str(a["href"])
    if "/episode/" not in href:
        return None
    m = _CONTENT_RE.search(urlparse(href).path)
    if m and m.group(1) == "episode":
        return int(m.group(2))
    return None


def parse_episode_details(html: str, episode_id: int, base_url: str) -> EpisodeDetails:
    """تحويل صفحة حلقة /episode/{id} إلى EpisodeDetails.

    رقم الحلقة من ``/الحلقة-(\\d+)`` في الرابط (canonical/og:url أو أي رابط
    ذاتي داخل الصفحة)، والحلقة التالية/السابقة من h3.entry-title المناسب.
    """
    soup = _soup(html)

    # رقم الحلقة من الرابط الكنسي (og:url / canonical) ثم من h1 كاحتياط
    number: int | None = None
    for candidate in (_meta_content(soup, "og:url"),):
        if candidate:
            m = _EP_NUM_RE.search(urlparse(candidate).path)
            if m:
                number = int(m.group(1))
                break
    if number is None:
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical and canonical.get("href"):
            m = _EP_NUM_RE.search(urlparse(str(canonical["href"])).path)
            if m:
                number = int(m.group(1))
    if number is None:
        h1 = soup.select_one("h1.entry-title")
        m = re.search(r"الحلقة\s+(\d+)", h1.get_text(" ", strip=True)) if h1 else None
        if m:
            number = int(m.group(1))

    # id المسلسل من أول رابط /series/ في الصفحة
    series_id: int | None = None
    series_a = soup.select_one('a[href*="/series/"]')
    if series_a is not None:
        m = _CONTENT_RE.search(urlparse(str(series_a["href"])).path)
        if m and m.group(1) == "series":
            series_id = int(m.group(2))

    next_id: int | None = None
    prev_id: int | None = None
    for h3 in soup.select("h3.entry-title"):
        txt = h3.get_text(" ", strip=True)
        if "الحلقة التالية" in txt and next_id is None:
            next_id = _episode_nav_id(h3)
        elif "الحلقة السابقة" in txt and prev_id is None:
            prev_id = _episode_nav_id(h3)

    return EpisodeDetails(
        id=episode_id,
        series_id=series_id,
        number=number,
        title=_text(soup.select_one("h1.entry-title")) or "",
        qualities=_parse_qualities(soup, base_url),
        next_episode_id=next_id,
        prev_episode_id=prev_id,
    )


def parse_direct_links(html: str) -> list[DirectLink]:
    """استخراج الروابط المباشرة من صفحة /watch عبر ``video#player source``.

    كل source فيه ``src`` = رابط MP4 موقّت و``size`` = الدقة (1080/720/480).
    """
    soup = _soup(html)
    links: list[DirectLink] = []
    for source in soup.select("video#player source"):
        src = source.get("src")
        if not src:
            continue
        src = str(src)
        size = source.get("size")
        if size:
            quality = f"{size}p"
        else:
            m = _QUALITY_RE.search(src)
            quality = f"{m.group(1)}p" if m else ""
        filename = urlparse(src).path.rsplit("/", 1)[-1] or None
        links.append(DirectLink(url=src, quality=quality, filename=filename))
    return links


def parse_download_redirect(html: str) -> str | None:
    """استخراج الرابط المباشر من صفحة /download الوسيطة.

    البنية: ``div.page-redirect div.btn-loader a[href*="downet.net/download/"]``.
    """
    soup = _soup(html)
    a = soup.select_one("div.page-redirect div.btn-loader a")
    if a is not None and a.get("href"):
        return str(a["href"])
    # احتياط: أي رابط downet في الصفحة
    fallback = soup.select_one('a[href*="downet.net/download/"]')
    if fallback is not None and fallback.get("href"):
        return str(fallback["href"])
    return None
