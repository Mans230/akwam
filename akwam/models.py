"""نماذج البيانات (dataclasses) لحزمة سكرابر أكوام — العقود الملزمة من SPEC 3.1."""

from dataclasses import dataclass, field


@dataclass
class SearchResult:
    id: int
    type: str            # 'movie' | 'series'
    title: str
    url: str
    poster: str | None = None
    year: int | None = None
    rating: float | None = None
    quality: str | None = None


@dataclass
class QualityLink:
    quality: str         # مثل '1080p'
    file_id: int
    content_id: int
    watch_url: str       # رابط صفحة /watch كامل
    download_url: str    # رابط صفحة /download كامل
    size: str | None = None   # مثل '2.4 GB'


@dataclass
class MovieDetails:
    id: int
    title: str
    poster: str | None
    year: int | None
    rating: float | None
    description: str | None
    qualities: list[QualityLink] = field(default_factory=list)


@dataclass
class Episode:
    id: int
    number: int
    title: str
    url: str
    thumb: str | None = None


@dataclass
class SeriesDetails:
    id: int
    title: str
    poster: str | None
    year: int | None
    rating: float | None
    description: str | None
    episodes: list[Episode] = field(default_factory=list)


@dataclass
class EpisodeDetails:
    id: int
    series_id: int | None
    number: int | None
    title: str
    qualities: list[QualityLink] = field(default_factory=list)
    next_episode_id: int | None = None
    prev_episode_id: int | None = None


@dataclass
class DirectLink:
    url: str             # رابط MP4 مباشر موقّت
    quality: str         # '1080p' / '720p' / '480p'
    filename: str | None = None
