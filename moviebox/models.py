"""نماذج البيانات (dataclasses) لحزمة سكرابر موفي بوكس — العقود الملزمة من SPEC3 3.1."""

from dataclasses import dataclass, field


@dataclass
class MbQuality:
    resolution: int        # 360/480/720/1080
    url: str
    size: int | None       # بايت
    duration: int | None   # ثوانٍ
    vip_locked: bool = False


@dataclass
class MbCaption:
    lan: str               # 'ar','en',...
    lan_name: str          # اسم اللغة كما يرد
    url: str               # SRT موقّع (~7 أيام)
    size: str | None = None
    delay: int = 0


@dataclass
class MbDub:
    name: str              # "Arabic sub" / "English dub" / "Original Audio"
    lan_code: str
    type: int              # 0=دبلجة صوتية، 1=نسخة بترجمة مدمجة
    subject_id: str
    detail_path: str
    original: bool = False


@dataclass
class MbSeason:
    se: int
    max_ep: int
    all_ep: list[int] | None    # None = كل الحلقات متوفرة
    resolutions: list[int] = field(default_factory=list)


@dataclass
class MbDetails:
    subject_id: str
    detail_path: str
    type: str                  # 'movie' | 'series'
    title: str
    description: str | None
    year: int | None
    rating: float | None
    poster: str | None
    genres: list[str] = field(default_factory=list)
    dubs: list[MbDub] = field(default_factory=list)
    seasons: list[MbSeason] = field(default_factory=list)   # فارغة للأفلام
    subtitle_languages: list[str] = field(default_factory=list)


@dataclass
class MbStreams:
    qualities: list[MbQuality] = field(default_factory=list)   # مرتبة تنازلياً بالدقة، vip_locked/url فارغ مستبعد
    captions: list[MbCaption] = field(default_factory=list)
    dash_url: str | None = None   # رابط MPD بديل عبر بروكسي الـ API (لتحميل DASH احتياطي)
