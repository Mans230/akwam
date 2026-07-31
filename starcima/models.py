"""نماذج بيانات حزمة ستار سيما — العقود الملزمة من SPEC2 3.1."""

from dataclasses import dataclass, field


@dataclass
class ServerLink:
    name: str            # "سيرفر 1"
    embed_url: str
    provider: str        # 'akwam' | 'streamwish' | 'updown' | 'vidtube' | 'other'
    downloadable: bool   # provider قابل للاستخراج أو akwam
    is_akwam: bool


@dataclass
class ScSeason:
    number: int
    episode_count: int
    name: str | None = None


@dataclass
class ScMediaDetails:
    id: int              # tmdb id
    type: str            # 'movie' | 'series'
    title_ar: str
    title_en: str | None
    year: int | None
    rating: float | None
    description: str | None
    poster: str | None   # https://image.tmdb.org/t/p/w500{poster_path}
    seasons: list[ScSeason] = field(default_factory=list)


@dataclass
class ScEpisode:
    number: int
    title: str
    season: int
    overview: str | None = None
    thumb: str | None = None


@dataclass
class ExtractedStream:
    kind: str                # 'mp4' | 'hls'
    direct_url: str
    proxy_url: str | None    # بروكسي ستار سيما (يعمل من أي IP) — للـ hls خصوصاً
