# SPEC3.md — دمج TheMovieBox كموقع ثالث (مواصفات ملزمة — امتداد لـ SPEC/SPEC2)

> المرجع التقني: `/mnt/agents/output/info-moviebox.md` (موثق بطلبات حية). وكيلان بالتوازي: سكرابر (F) وبوت (G) — العقود هنا ملزمة حرفياً.

## 1. القرارات المقفولة (من المستخدم)
1. زر ثالث في اختيار الموقع: **📦 موفي بوكس** + زر **🔥 الرائج** (من موفي بوكس) في نفس رسالة اختيار الموقع
2. بدون فلتر نوع بحث — الكل مباشرة
3. الجودات 360/480/720/1080 كأزرار (إرسال ⭐ بريميوم / رابط تحميل) — نفس نمط أكوام
4. الترجمة: زر رئيسي **📝 ترجمة عربية** + زر **🌐 لغات أخرى** (13 لغة بصفحات) — ملف SRT **يُرسل كملف في الشات** (send_document)
5. النسخ/الدبلجات: زر **🌐 النسخ واللغات** داخل التفاصيل → قائمة النسخ (أصلي/مترجم عربي/دبلجات) → كل نسخة تفتح تفاصيلها بجوداتها
6. تحميل الموسم كامل متاح (بريميوم) — نفس نمط الموقعين
7. قواعد البريميوم نفسها على المواقع الثلاثة
8. الأدمن: تفعيل/تعطيل موفي بوكس من 🌐 المواقع + إحصائياته

## 2. البنية الجديدة
```
moviebox/               # المالك: وكيل F
├── __init__.py         # يصدّر MovieboxClient وكل models وVIDEO_REFERER
├── models.py
└── client.py
tests/test_moviebox.py  # وكيل F
```
وكيل G يعدّل: `bot/handlers_user.py`, `bot/keyboards.py`, `bot/handlers_admin.py`, `bot/db.py` (لا شيء جديد غالباً — site column موجود), `bot/config.py`, `bot/downloader.py` (إضافة referer فقط), `main.py`, `.env.example`, `README.md`.

## 3. عقود moviebox (وكيل F)

### 3.1 models.py
```python
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
```

### 3.2 client.py — `class MovieboxClient`
```python
VIDEO_REFERER = "https://videodownloader.site/"   # ثابت على مستوى الحزمة — يُمرر للداونلودر

def __init__(self, base_url: str = "https://themoviebox.xyz",
             api_base: str = "https://h5-api.aoneroom.com/wefeed-h5api-bff",
             timeout: float = 30.0, max_retries: int = 3): ...
async def close(self) -> None: ...
async def search(self, query: str, page: int = 1) -> list[SearchResult]:
    """POST {api}/subject/search بـ Bearer كسول:
    أول طلب: POST /subject/search-suggest {"keyword":"x","perPage":10} والتقط token من هيدر x-user (JSON {"token":"eyJ.."}).
    خزّنه؛ عند 400/401/invalid token جدّده مرة وأعد. Body: {"keyword":q,"page":page,"perPage":10,"subjectType":0}.
    نتيجة → akwam.models.SearchResult: id=int(subjectId), type 'movie'(subjectType=1)/'series'(2)/غيرها→'movie',
    title, url=f'{base}/detail/{detailPath}', poster=cover.url, year من releaseDate, rating=float(imdbRatingValue) أو None.
    ملاحظة: detailPath مطلوب لاحقاً — خزّنه في url كما ورد (البوت يستخرجه من url)."""
async def trending(self, page: int = 1) -> list[SearchResult]:
    """GET {api}/subject/trending?page=N&perPage=18 (بدون Bearer) — نفس التحويل."""
async def get_details(self, detail_path: str) -> MbDetails:
    """GET {api}/detail?detailPath=.. بـ X-Request-Lang: ar (عنوان/وصف عربي).
    seasons من resource.seasons (allEp نص→list[int]، فارغ/مفقود→None). dubs من subject.dubs.
    subtitle_languages من subject.subtitles (نص مفصول بفواصل→قائمة)."""
async def get_streams(self, subject_id: str, detail_path: str, se: int = 0, ep: int = 0) -> MbStreams:
    """GET {api}/subject/play?subjectId=..&se=..&ep=..&detailPath=..
    هيدرز إلزامية: Origin: https://videodownloader.site — وممنوع إرسال Authorization/X-Client-Token هنا.
    streams → MbQuality (استبعد vipLocked:true أو url فارغ؛ resolutions نص→int) مرتبة تنازلياً.
    captions → MbCaption."""
```
- httpx async، UA متصفح، retry/backoff كالحزم الأخرى، 404 → NotFoundError (أعد تصديره من akwam.client).

## 4. تعديلات البوت (وكيل G)

### 4.1 config/env
- `MOVIEBOX_DOMAIN: str = "https://themoviebox.xyz"` + .env.example بتعليق عربي.

### 4.2 downloader.py (إضافة غير كاسرة فقط)
- `DownloadJob` يكسب حقل `referer: str | None = None` (اختياري، آخر الحقول).
- كل طلبات التحميل (HEAD probe + GET segments/single) ترسل `headers={"Referer": job.referer}` لو مضبوط.

### 4.3 site picker (handlers_user + keyboards)
- `site_picker_kb` تصبح: `[🎬 أكوام] [⭐ ستار سيما] [📦 موفي بوكس]` (cb='site:m:{key}') + سطر `[🔥 الرائج] cb='mbtrend:1'` — مع احترام تفعيل الأدمن `site_moviebox`.
- `mbtrend:{page}` → `moviebox.trending(page)` → عرض كنتايج (شارة 📦) بنفس كاش النتايج `r:{key}:{i}` (items موسّعة site='m') + أزرار تنقل الرائج (صفحات).

### 4.4 تدفق موفي بوكس
- نتيجة (site='m') → MbContext مخزن بالكاش `mb:{uuid8}`: {subject_id, detail_path (من r.url بعد '/detail/'), title, poster}.
- تفاصيل: `get_details` → رسالة (بوستر، عنوان، سنة، تقييم، وصف، 📦) + كيبورد:
  - فيلم: `[📥 الجودات والترجمة] cb='mbq:{ckey}:0:0'`
  - مسلسل: مواسم `mbs:{ckey}:{se}` → حلقات `mbeps:{ckey}:{se}:{page}` (20/صفحة، احترم all_ep: اعرض المتاح فقط أو علّم الناقص 🔒) → `mbep:{ckey}:{se}:{ep}` → نفس شاشة الجودات + أزرار ⏭/⏮ (تخطَّ الحلقات غير المتوفرة) + `[⬇ تحميل الموسم ⭐] cb='mbsall:{ckey}:{se}'`
  - + `[🌐 النسخ واللغات] cb='mbdubs:{ckey}'` + `[🔗 صفحة الموقع] url`
- **شاشة الجودات** `mbq:{ckey}:{se}:{ep}`: `get_streams` (استدعاء طازج دائماً — الروابط تنتهي) → لكل جودة: `[⬇ إرسال {res}p ⭐] cb='mbsend:{ckey}:{se}:{ep}:{res}'` و`[🔗 {res}p — {حجم}] cb='mblink:{ckey}:{se}:{ep}:{res}'` + `[📝 ترجمة عربية] cb='mbsub:{ckey}:{se}:{ep}:ar'` + `[🌐 لغات أخرى] cb='mblangs:{ckey}:{se}:{ep}:1'`.
  - ⚠️ انتبه طول الـ callback: `mbsend:` + uuid8 + se/ep (≤2) + res (4) = ≤40 ✓.
- `mbsend:` (بريميوم — نفس _send_allowed) → get_streams طازج → الجودة المطلوبة → `DownloadManager.enqueue` بـ DownloadJob(title=«{العنوان} ({res}p 📦)», url, caption، thumb=poster, **referer=moviebox.VIDEO_REFERER**) + log_download(site='moviebox').
- `mblink:` → get_streams طازج → زر URL للرابط + ملاحظة: "لو الرابط مافتحش في المتصفح (خطأ 429) استخدم ⬇ الإرسال المباشر — الرابط بيحتاج Referer خاص".
- **الترجمة** `mbsub:{ckey}:{se}:{ep}:{lan}`: get_streams → captions → جدّ lan → نزّل ملف SRT (بدون referer) → `bot.send_document(BufferedInputFile(bytes, filename="{title}.ar.srt"))` بكابشن عربي. غير موجودة → "مفيش ترجمة باللغة دي للنسخة دي".
- `mblangs:` → قائمة اللغات (13) 10/صفحة: كل لغة `mbsub:..:{lan}` (lan قصير ≤5) + عرض lan_name.
- **النسخ** `mbdubs:{ckey}` → قائمة dubs (مع تمييز: الأصلي 🎧، عربي 🇪🇬، type=1 "مترجم مدمج"، type=0 "دبلجة") → `mbd:{ckey}:{i}` → context جديد بنفس ckey محدّث (subject_id/detail_path للنسخة) → إعادة عرض التفاصيل.
- `mbsall:` (بريميوم) → لكل حلقة متاحة: get_streams → أفضل جودة ≤720 متاحة (أو الأعلى) → enqueue بـ referer — تخطَّ الفاشل ولخّص.
- لا سيرفرات/جودات → زر "جرّب موقع آخر" (site picker بنفس المفتاح).

### 4.5 الأدمن
- `sites_kb` يكسب `[📦 موفي بوكس: ✅/🔒] cb='adm:site:moviebox'` (settings key `site_moviebox`).
- stats: requests_moviebox/downloads_moviebox.

### 4.6 main.py
- `MovieboxClient(base_url=settings.MOVIEBOX_DOMAIN)` + `dp["moviebox"]` + إغلاق في finally.

## 5. الجودة والاختبار
- وكيل F: `tests/test_moviebox.py` حي: بحث عربي/إنجليزي، trending، تفاصيل فيلم (dubs)، تفاصيل مسلسل (seasons/all_ep)، get_streams فيلم (جودات مرتبة + captions فيها ar)، get_streams حلقة، وتحقق أن رابط mp4 يعمل بـ HEAD مع Referer (200/206) ويفشل بدونه (429) — لتوثيق السلوك.
- وكيل G: py_compile + import + smoke tests بأوبجكتات مزيفة (3 أزرار picker + الرائج، شاشة جودات، حارس بريميوم mbsend/mbsall، إرسال SRT كـ document، تبديل الأدمن، callbacks ≤64 بايت بأسوأ القيم، تهريب HTML).
- كل نصوص الرد مهربة (textutil الموجود) — ممنوع كسر HTML.
- tests/ الأصلية (18) تظل تمر.
