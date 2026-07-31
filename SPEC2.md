# SPEC2.md — دمج StarCima في بوت أكوام (مواصفات ملزمة — امتداد لـ SPEC.md)

> المرجع التقني الكامل للموقع: `/mnt/agents/output/info-starcima.md` (تقرير استكشاف موثق بطلبات حية). كل العقود هنا ملزمة حرفياً لأن وكيلين يعملان بالتوازي (وكيل سكرابر D + وكيل بوت E).

## 1. القرارات المقفولة (من المستخدم)
1. زران اختيار موقع (🎬 أكوام / ⭐ ستار سيما) تظهران **قبل كل بحث** — اليوزر يبعت الاسم → البوت يعرض الزرين → البحث في الموقع المختار
2. شارة الموقع بجانب كل نتيجة (🔵 أكوام / ⭐ ستار سيما)
3. ستار سيما: السيرفرات كأزرار + علامة ⚡ للقابل للتحميل؛ القابل أولاً ثم الباقي
4. الإرسال كفيديو من MP4 المباشر فقط؛ HLS يُعرض كرابط مشاهدة (عبر proxy ستار سيما)
5. قواعد البريميوم نفسها على الموقعين (الإرسال للبريميوم فقط)
6. تحميل الموسم كامل متاح في الموقعين (بريميوم)
7. روابط المشاهدة في ستار سيما: أزرار كل السيرفرات + رابط صفحة المشاهدة الأصلية (الاتنين)
8. روابط akwam.it الظاهرة ضمن سيرفرات ستار سيما تُعالج بسكرابر أكوام → أزرار جودات واضحة
9. قسم المدبلج يظهر ضمن نتايج بحث ستار سيما
10. زر ترجمة عربية SRT مع الفيلم/الحلقة
11. لو لا سيرفرات شغالة → زر "🎬 جرّب في أكوام"
12. الأدمن: تفعيل/تعطيل كل موقع من /admin + إحصائيات لكل موقع
13. السيرفرات غير القابلة للتحميل تظهر كروابط مشاهدة فقط
14. عرض السيرفرات: 10 لكل صفحة + تنقل
15. الترتيب: ⚡ القابل للتحميل أولاً

## 2. البنية الجديدة
```
starcima/                # المالك: وكيل D — حزمة جديدة
├── __init__.py          # يصدّر StarcimaClient وكل models
├── models.py            # ServerLink, ScMediaDetails, ScSeason, ScEpisode, ExtractedStream
└── client.py            # StarcimaClient (JSON APIs فقط — لا HTML parsing)
```
الملفات المعدلة (المالك: وكيل E): `bot/handlers_user.py`, `bot/keyboards.py`, `bot/handlers_admin.py`, `bot/db.py`, `bot/config.py`, `main.py`, `.env.example`, `README.md`.
**ممنوع لمس**: `akwam/` (يعمل)، `bot/downloader.py`, `bot/middlewares.py`, `bot/cache.py` — إلا بإضافات غير كاسرة.

## 3. عقود starcima (وكيل D)

### 3.1 models.py
```python
@dataclass
class ServerLink:
    name: str            # "سيرفر 1"
    embed_url: str
    provider: str        # 'akwam' | 'streamwish' | 'updown' | 'vidtube' | 'other'
    downloadable: bool   # provider قابل للاستخراج أو akwam
    is_akwam: bool

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
class ScSeason:
    number: int
    episode_count: int
    name: str | None = None

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
```

### 3.2 client.py — `class StarcimaClient`
```python
def __init__(self, base_url: str, timeout: float = 30.0, max_retries: int = 3): ...
async def close(self) -> None: ...
async def search(self, query: str, page: int = 1) -> list[SearchResult]:
    """GET {base}/api/tmdb/search/multi?query=..&language=ar-SA&page=N — يرجع akwam.models.SearchResult
    (id=tmdb_id, type='movie'|'series' [media_type 'tv'→'series'], title=العربي,
    url=f'{base}/media/{id}?type={'movie'|'tv'}', poster=tmdb image w500, year من التاريخ, rating=vote_average).
    تجاهل media_type='person'."""
async def search_dubbed(self, query: str) -> list[SearchResult]:
    """GET {base}/api/dubbed/search?q=.. — type='dubbed'، عنوان مسبوق بـ (مدبلج)، url=صفحة المحتوى الأصلية."""
async def get_media(self, tmdb_id: int, type: str) -> ScMediaDetails:
    """type 'movie'→/api/tmdb/movie/{id}، 'series'→/api/tmdb/tv/{id} — language=ar-SA&append_to_response=external_ids.
    المسلسل: seasons من الحقل seasons (استبعد season_number=0 specials)."""
async def get_episodes(self, tmdb_id: int, season: int) -> list[ScEpisode]:
    """GET /api/tmdb/tv/{id}/season/{N}?language=ar-SA — thumb من still_path (w300)."""
async def get_servers(self, title_ar: str, title_en: str | None, year: int | None, type: str,
                      season: int | None = None, episode: int | None = None,
                      abs_episode: int | None = None, season_ep_count: int | None = None) -> list[ServerLink]:
    """GET /api/arabic-sources?title=..&type=..&englishTitle=..&year=..[&season&episode&absEpisode&seasonEpCount]
    كل عنصر {name, embedUrl} → ServerLink مع تصنيف provider من الهوست:
    'akwam' في الرابط → akwam (is_akwam=True, downloadable=True)
    streamwish/hlswish/swh → streamwish (downloadable=True)
    updown → updown (True) ; vidtube → vidtube (True) ; غير ذلك → other (False)
    الترتيب: downloadable أولاً ثم الباقي (مع الحفاظ على ترتيب الموقع داخل كل مجموعة)."""
async def extract(self, embed_url: str) -> ExtractedStream | None:
    """GET /api/extract?url={embed_url} — نجاح: {type:'hls'|'mp4', directUrl, url(proxy)} → ExtractedStream
    (proxy_url = base + url لو نسبي). فشل/خطأ → None. لا ترمِ استثناء."""
async def get_subtitles(self, tmdb_id: int, season: int | None = None, episode: int | None = None) -> list[str]:
    """GET /api/wyzie-subs?id=..[&season&episode] — قائمة روابط SRT العربية (قد تكون فارغة)."""
async def get_dubbed_episodes(self, series_key: str) -> list[ScEpisode]:
    """GET /api/dubbed/episodes?series={key} — حلقات المحتوى المدبلج."""
```
- httpx async، UA متصفح، retry/backoff مثل akwam client، NotFoundError عند 404 (يمكن استيراده من akwam.client أو تعريف محلي وإعادة تصديره).
- متسامح مع الأعطال: showbox/vidzee معطلة حالياً — **لا تعتمد عليها إطلاقاً**.
- السيرفر الاحتياطي الثابت vidking يضاف من طبقة البوت وليس الكلاينت.

## 4. تعديلات البوت (وكيل E)

### 4.1 config + env
- `STARCIMA_DOMAIN: str = "https://starcima.com"` + `SERVERS_PER_PAGE: int = 10` (إضافات فقط) + .env.example بتعليق عربي.

### 4.2 db.py (إضافات migration-آمنة فقط)
- عمود `site TEXT DEFAULT 'akwam'` لجدولي requests وdownloads (ALTER آمن كالسابق).
- جدول `settings(key TEXT PRIMARY KEY, value TEXT)` + `get_setting(key, default=None)` و`set_setting(key, value)`.
- `log_request`/`log_download` يقبلان `site='akwam'` باراميتر اختياري.
- `stats()` يضيف: `requests_akwam`, `requests_starcima`, `downloads_akwam`, `downloads_starcima`.

### 4.3 تدفق البحث (handlers_user + keyboards)
- أي نص (غير أوامر) → خزّن الاستعلام في الكاش `q:{uid}:{uuid6}` → رسالة "اختار الموقع 👇" + `site_picker_kb(key)`: `[🎬 أكوام] cb='site:a:{key}'` `[⭐ ستار سيما] cb='site:s:{key}'` (لو موقع معطّل من الأدمن → الزر يظهر "🔒 متوقف مؤقتاً" cb='noop' أو يُخفى).
- `site:a:{key}` → نفس تدفق أكوام الحالي بالضبط (مع site badge 🔵 في نص النتايج، وlog_request(site='akwam')).
- `site:s:{key}` → `starcima.search(q)` + `starcima.search_dubbed(q)` (مدموجة: العادية أولاً ثم المدبلجة) → نفس شكل عرض النتايج مع شارة ⭐ وشارة 🎙 للمدبلج → تخزين في الكاش بنفس نمط `r:{key}:{i}` (وسّع عنصر الكاش ليشمل `site` و`dubbed`).
- لا نتايج → "مفيش نتايج في ستار سيما — جرّب أكوام 👇" + زر `site:a:{key}`.

### 4.4 تفاصيل ستار سيما
- فيلم/مسلسل من `r:` (site='s') → `get_media` → رسالة التفاصيل (بوستر، عنوان، سنة، تقييم، وصف، ⭐ ستار سيما) + كيبورد ستار سيما:
  - فيلم: `[📡 السيرفرات] cb='srv:{ckey}:1'` `[📝 ترجمة عربية] cb='sub:{tmdb}:0:0'` `[🔗 صفحة المشاهدة] url=watch_page`
  - مسلسل: أزرار المواسم `scseason:{tmdb}:{n}` (من seasons) → حلقات `sceps:{tmdb}:{n}:{page}` (20/صفحة) → الحلقة `scep:{tmdb}:{s}:{e}` → تفاصيل الحلقة + نفس أزرار الفيلم (`sub:{tmdb}:{s}:{e}`، سيرفرات، صفحة مشاهدة بمعاملات season/ep) + زرا التالية/السابقة (نفس نمط scep) + `[⬇ تحميل الموسم ⭐] cb='salls:{tmdb}:{n}'`.
- **سياق السيرفرات**: عند فتح `srv:` أول مرة ابنِ ServerContext (tmdb, type, season, episode, title_ar, title_en, year, abs_episode, season_ep_count) وخزّنه في الكاش بمفتاح قصير `srv:{uuid8}` واستخدمه في ترقيم الصفحات: `srv:{ckey}:{page}`.
- جلب السيرفرات: `get_servers(...)` (abs_episode فقط لو متوفر — للأنمي المتواصل يمكن حسابه من جمع حلقات المواسم السابقة + رقم الحلقة؛ لو تعذر تجاهله) → أضف سيرفر vidking الاحتياطي في النهاية (other) → عرض 10/صفحة:
  - لكل سيرفر ⚡ قابل: زر `[⚡ {name} — تحميل/إرسال] cb='sget:{ckey}:{i}'` (i = رقم السيرفر في القائمة الكاملة)
  - لكل سيرفر akwam: `[🎬 {name} — جودات أكوام] cb='sakw:{ckey}:{i}'`
  - غير قابل: زر URL مباشر `[👁 {name}] url=embed_url`
  - + زر صفحة المشاهدة الأصلية دائماً.
- `sget:` → `extract(embed)`:
  - mp4 → رسالة: `[⬇ إرسال الفيديو ⭐] cb='ssend:{ckey}:{i}'` (بريميوم فقط — نفس حارس _send_allowed) + `[🔗 رابط التحميل] url=direct_url` + تنبيه صلاحية الرابط
  - hls → `[👁 مشاهدة مباشرة] url=proxy_url or direct_url` + نص "السيرفر ده مشاهدة فقط"
  - None → "تعذر الاستخراج — متاح للمشاهدة فقط" + زر embed
- `ssend:` → (بريميوم) extract من جديد → mp4 → DownloadManager.enqueue (title: «{العنوان} ({provider} ⭐ ستار سيما)», caption عربي، thumb=poster). log_download(site='starcima').
- `sakw:` → استخرج `/watch/{fid}/{cid}` من الرابط → `akwam.get_direct_links(fid, cid)` → أزرار جودات أكوام الحقيقية (إرسال ⭐ بريميوم / رابط) — أعد استخدام منطق أكوام القائم قدر الإمكان.
- `salls:{tmdb}:{season}` → (بريميوم) لكل حلقة بالترتيب: get_servers → أول سيرفر downloadable (غير akwam أولاً، ثم akwam) → extract (أو akwam direct links بأعلى جودة) → mp4 فقط يُنفَّذ enqueue؛ الحلقات بدون mp4 تُتخطى وتُذكر في رسالة التلخيص النهائية.
- `sub:` → get_subtitles → أزرار URL لملفات SRT أو "لا توجد ترجمة متاحة".
- المدبلج (type='dubbed'): تفاصيل = get_dubbed_episodes → قائمة حلقات (20/صفحة) → الحلقة: زر URL لصفحة المشاهدة الأصلية فقط (لا استخراج) + شارة 🎙.

### 4.5 الأدمن (handlers_admin + keyboards)
- `admin_kb`: زر `[🌐 المواقع] cb='adm:sites'` → `sites_kb()`: `[🎬 أكوام: ✅/🔒] cb='adm:site:akwam'` `[⭐ ستار سيما: ✅/🔒] cb='adm:site:starcima'` — التبديل يخزن في db settings (`site_akwam`/`site_starcima` = '1'/'0'، افتراضي '1'). اقرأ الحالة عند عرض site_picker_kb.
- `adm:stats` يضيف سطراً: طلبات/تحميلات كل موقع.

### 4.6 main.py
- إنشاء `StarcimaClient(base_url=settings.STARCIMA_DOMAIN)` وحقنه `dp["starcima"]` + إغلاقه في finally.

## 5. الجودة والاختبار
- وكيل D: اختبارات حية على starcima.com في `tests/test_starcima.py` (بحث عربي+إنجليزي، فيلم، مسلسل بمواسم/حلقات، سيرفرات حقيقية مع تصنيف صحيح، extract لسيرفر streamwish/updown يعيد mp4 أو hls، ترجمة، مدبلج) — كلها يجب أن تنجح فعلياً؛ لو API اختلف عن التقرير افحص الاستجابة الحقيقية وكيّف الكلاينت.
- وكيل E: py_compile + import main + smoke tests بأوبجكتات مزيفة (تدفق site picker، عرض سيرفرات مرقم، حارس البريميوم على ssend/salls، تبديل المواقع من الأدمن، callback ≤64 بايت لكل الجديدة).
- لا روابط مستخرجة تُخزن (موقّتة) — استخراج عند الطلب فقط. الكاش مسموح لقوائم السيرفرات والنتايج.
- اختبارات tests/ الأصلية تظل تمر.
