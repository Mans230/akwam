# SPEC.md — بوت تليجرام أكوام (akwam bot) — المواصفات الملزمة

> هذا الملف هو المصدر الوحيد للحقيقة. كل وكيل تطوير ينفّذ وحدته مطابقاً للواجهات هنا حرفياً. ممنوع تغيير أسماء الملفات/الدوال/الحقول.

## 1. نظرة عامة
بوت تليجرام (Python 3.11+, aiogram 3.7+) يبحث في موقع akwam عن أفلام/مسلسلات ويعرض:
- نتايج البحث (بوستر + سنة + تقييم + نوع) بأزرار
- فيلم → جودات كأزرار: لكل جودة (إرسال الفيديو هنا / رابط تحميل مباشر) + رابط مشاهدة
- مسلسل → مواسم (تُجمّع من البحث) → حلقات (صفحات 20 حلقة) → نفس خيارات الفيلم + زر الحلقة التالية/السابقة
- زر "تحميل الموسم كامل": إرسال الحلقات ورا بعض واحدة واحدة
- إرسال الفيديو عبر **Bot API Local Server** (لحد 2GB): تحميل من downet.net ثم رفع لتليجرام مع تقدم لايف + زر إلغاء
- اشتراك إجباري بقناة + لوحة أدمن + حدود تحميل لكل يوزر يضبطها الأدمن + كاش مؤقت

## 2. بنية المشروع (أسماء ملزمة)
```
project/
├── requirements.txt          # المالك: main agent
├── .gitignore                # المالك: main agent
├── .env.example              # المالك: main agent
├── README.md                 # المالك: وكيل النشر (C)
├── Dockerfile                # المالك: وكيل النشر (C)
├── docker-compose.yml        # المالك: وكيل النشر (C)
├── main.py                   # المالك: وكيل البوت (B) — نقطة التشغيل
├── akwam/                    # المالك: وكيل السكرابر (A)
│   ├── __init__.py           # يصدّر AkwamClient وكل الـ models
│   ├── client.py             # AkwamClient (httpx async + retries + cache)
│   ├── parsers.py            # دوال parse خالصة (HTML → dataclasses) — بدون شبكة
│   └── models.py             # dataclasses الموثقة أدناه
├── bot/                      # المالك: وكيل البوت (B)
│   ├── __init__.py
│   ├── config.py             # المالك: main agent (موجود مسبقاً — لا تعدّله)
│   ├── db.py                 # SQLite عبر aiosqlite
│   ├── cache.py              # TTL cache بسيط في الذاكرة
│   ├── keyboards.py          # كل InlineKeyboard builders
│   ├── downloader.py         # طابور التحميل/الرفع + التقدم + الإلغاء
│   ├── middlewares.py        # اشتراك إجباري + حظر + تسجيل المستخدم
│   ├── handlers_user.py      # تدفقات المستخدم
│   └── handlers_admin.py     # لوحة الأدمن
└── tests/
    └── test_scraper.py       # المالك: وكيل السكرابر (A)
```

## 3. عقود الواجهات (ملزمة حرفياً)

### 3.1 `akwam/models.py`
```python
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
```

### 3.2 `akwam/client.py` — `class AkwamClient`
```python
class AkwamClient:
    def __init__(self, base_url: str, timeout: float = 30.0, max_retries: int = 3): ...
    async def close(self) -> None: ...
    async def search(self, query: str, section: str | None = None) -> list[SearchResult]: ...
    async def search_seasons(self, query: str) -> list[SearchResult]:
        """بحث section='series' وترجيع نتايج المسلسلات (تُستخدم لتجميع المواسم)."""
    async def get_movie(self, movie_id: int) -> MovieDetails: ...
    async def get_series(self, series_id: int) -> SeriesDetails: ...
    async def get_episode(self, episode_id: int) -> EpisodeDetails: ...
    async def get_direct_links(self, file_id: int, content_id: int) -> list[DirectLink]:
        """GET صفحة /watch/{file_id}/{content_id} واستخراج video#player source لكل الجودات."""
    async def resolve_download(self, file_id: int, content_id: int) -> DirectLink | None:
        """بديل: صفحة /download الوسيطة → div.page-redirect div.btn-loader a. يُستخدم كـ fallback."""
```
- httpx.AsyncClient، headers: UA ثابت (انظر info.md)، follow_redirects=True.
- retry: عند فشل الشبكة أو 5xx → إعادة بـ backoff (1s, 2s, 4s). عند 404 → رمي `akwam.client.NotFoundError`.
- الـ parsing كله في `parsers.py` (دوال خالصة تأخذ نص HTML) — client يجلب فقط.
- التفاصيل التقنية للـ selectors كلها في `/mnt/agents/output/info.md` — التزم بها.

### 3.3 `bot/config.py` (موجود مسبقاً من main agent — لا يُعدَّل)
يصدّر `settings` (pydantic-settings BaseSettings) بالحقول:
`BOT_TOKEN: str`, `AKWAM_DOMAIN: str = "https://akwam.it"`, `ADMIN_IDS: list[int]`,
`FORCE_CHANNEL: str = ""` (مثل `@mychannel` أو id؛ فارغ = معطّل),
`BOT_API_SERVER: str = ""` (فارغ = السيرفر الرسمي؛ مثل `http://telegram-bot-api:8081`),
`TG_API_ID: int = 0`, `TG_API_HASH: str = ""`,
`DEFAULT_MAX_CONCURRENT: int = 1`, `CACHE_TTL_HOURS: int = 6`,
`DOWNLOAD_DIR: str = "./downloads"`, `DB_PATH: str = "./data/bot.db"`, `EPISODES_PER_PAGE: int = 20`

### 3.4 `bot/db.py` — `class Database`
```python
class Database:
    def __init__(self, path: str): ...
    async def init(self) -> None: ...
    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None: ...
    async def is_banned(self, user_id: int) -> bool: ...
    async def set_ban(self, user_id: int, banned: bool) -> None: ...
    async def get_user_limit(self, user_id: int) -> int | None: ...      # None = استخدم الافتراضي
    async def set_user_limit(self, user_id: int, limit: int | None) -> None: ...
    async def log_request(self, user_id: int, query: str) -> None: ...
    async def log_download(self, user_id: int, title: str, quality: str, status: str) -> None: ...
    async def stats(self) -> dict:
        """{'users': int, 'banned': int, 'requests': int, 'downloads_done': int, 'downloads_today': int}"""
    async def all_user_ids(self) -> list[int]: ...   # للإذاعة
    async def search_users(self, query: str) -> list[dict]:
        """بحث بالـ id أو username — يرجع [{'id':..,'username':..,'first_name':..,'is_banned':..,'max_concurrent':..}]"""
```
الجداول: `users(id INTEGER PK, username, first_name, joined_at, is_banned INTEGER DEFAULT 0, max_concurrent INTEGER NULL)`,
`requests(id PK, user_id, query, created_at)`, `downloads(id PK, user_id, title, quality, status, created_at)`.
كل الطوابع الزمنية `datetime('now','localtime')`.

### 3.5 `bot/cache.py` — `class TTLCache`
```python
class TTLCache:
    def __init__(self, ttl_seconds: int, max_size: int = 1000): ...
    def get(self, key: str): ...        # None عند الانتهاء/الغياب
    def set(self, key: str, value) -> None: ...
```

### 3.6 `bot/downloader.py` — `class DownloadManager`
```python
class DownloadManager:
    def __init__(self, bot, db: Database, download_dir: str, default_limit: int): ...
    async def enqueue(self, user_id: int, chat_id: int, job: DownloadJob) -> None: ...
    async def cancel(self, task_id: str, user_id: int) -> bool: ...
    def active_count(self, user_id: int) -> int: ...

@dataclass
class DownloadJob:
    task_id: str           # uuid4 hex[:12]
    title: str             # اسم يظهر لليوزر (فيلم/حلقة + جودة)
    url: str               # رابط downet المباشر
    caption: str           # كابشن الفيديو
    thumb_url: str | None = None
```
- طابور asyncio لكل يوزر: `max_concurrent` من `db.get_user_limit` وإلا `default_limit`. الزيادة → رسالة "في طابور الانتظار".
- تحميل httpx streaming إلى `DOWNLOAD_DIR/{task_id}.mp4` مع تعديل رسالة التقدم كل ~5 ثوانٍ: نسبة % + سرعة MB/s + حجم منزّل/كلي + زر إلغاء `cancel:{task_id}`.
- بعد التحميل: رفع `FSInputFile` عبر `bot.send_video` (يدعم Local Server تلقائياً) مع نفس التقدم، ثم حذف الملف و `db.log_download(status='done'|'failed'|'cancelled')`.
- الإلغاء: `asyncio.Task.cancel()` + حذف الملف الجزئي.
- `supports_streaming=True` في send_video.

### 3.7 `bot/keyboards.py` — الدوال (callback_data ≤ 64 بايت إلزاماً)
```
search_results_kb(results: list[SearchResult], cache_key: str)  # زر لكل نتيجة: cb='r:{cache_key}:{i}' + زر إلغاء البحث
movie_kb(movie: MovieDetails)        # لكل جودة: [⬇ إرسال {q}] cb='send:{file_id}:{content_id}:{q}' و[🔗 رابط {q}] cb='link:{file_id}:{content_id}:{q}' + [👁 مشاهدة] cb='watch:{file_id}:{content_id}' + [🔙 رجوع للنتايج] cb='back'
seasons_kb(seasons: list[SearchResult])  # cb='season:{id}'
series_kb(series_id: int, episodes: list[Episode], page: int, per_page: int, total: int)
    # شبكة أرقام حلقات cb='ep:{id}'، تنقل cb='eps:{series_id}:{page}'، زر [⬇ تحميل الموسم كامل] cb='sall:{series_id}'
episode_kb(ep: EpisodeDetails)       # نفس أزرار الجودة + [⏭ الحلقة التالية] cb='ep:{next_id}' / [⏮ السابقة] + [🔙 الحلقات] cb='eps:{series_id}:1'
links_kb(urls: list[tuple[str,str]]) # أزرار URL عادية (رابط تحميل/مشاهدة مباشر)
cancel_kb(task_id: str)              # cb='cancel:{task_id}'
force_sub_kb(channel: str)           # زر انضمام URL + [✅ تحققت] cb='checksub'
admin_kb()                           # [📊 إحصائيات][📢 إذاعة][👤 إدارة مستخدم][🚫 محظورون] cb='adm:stats' / 'adm:bc' / 'adm:user' / 'adm:bans'
user_manage_kb(user_id, is_banned)   # cb='adm:ban:{id}' / 'adm:unban:{id}' / 'adm:lim:{id}' (ثم الأدمن يبعت الرقم)
```
ملاحظة: `send:`/`link:` يجب أن تبقى ≤64 بايت — file_id/content_id أرقام ≤8 خانات والجودة ≤5 أحرف ✓.

### 3.8 `bot/handlers_user.py`
- `/start` → ترحيب بالعربي (مصري) يشرح: "ابعت اسم الفيلم أو المسلسل".
- أي نص → `client.search(query)` → رسالة صورة (بوستر أول نتيجة إن وجد) + نص النتايج (مرقمة: اسم — سنة — تقييم — نوع) + `search_results_kb`. خزّن النتايج في TTLCache بمفتاح `search:{user_id}:{uuid6}`.
- cb `r:{key}:{i}` → لو movie: `get_movie` → عرض التفاصيل (صورة + وصف مختصر + `movie_kb`). لو series: `search_seasons(query)` لجمع المواسم → `seasons_kb` (لو موسم واحد فقط → تخطَّ مباشرة للحلقات).
- cb `season:{id}` / `eps:{id}:{page}` → `get_series` → `series_kb` (20/صفحة).
- cb `ep:{id}` → `get_episode` → `episode_kb`.
- cb `link:{fid}:{cid}:{q}` → `client.get_direct_links` → اختيار الجودة → `links_kb` بزر URL "⬇ تحميل {q}" + تنبيه "الرابط صالح ~24 ساعة".
- cb `watch:{fid}:{cid}` → روابط المشاهدة: زر URL لصفحة /watch الأصلية + أزرار URL للروابط المباشرة لكل جودة.
- cb `send:{fid}:{cid}:{q}` → `get_direct_links` → `DownloadManager.enqueue`.
- cb `sall:{series_id}` → لكل حلقة بالترتيب: `get_episode` → أول جودة متاحة (أو جودة يختارها من أزرار قبل البدء: `sallq:{series_id}:{q}`) → enqueue واحدة ورا التانية (DownloadManager ينفذها تباعاً بسبب حد التزامن=1).
- cb `cancel:{task_id}` → `downloader.cancel`.
- cb `back` → إعادة عرض آخر نتايج بحث من الكاش.
- كل الرسائل بالعربي، أزرار واضحة بإيموجي.

### 3.9 `bot/handlers_admin.py`
- `/admin` (أو زر) متاح لـ ADMIN_IDS فقط → `admin_kb`.
- `adm:stats` → عرض `db.stats()` + عدد التحميلات النشطة حالياً.
- `adm:bc` → FSM: الأدمن يبعت الرسالة → تأكيد → إرسال لكل `all_user_ids` مع معالجة FloodWait (تأخير 0.05s بين الرسائل) وتقرير نهائي (نجح/فشل).
- `adm:user` → FSM: يبعت id أو @username → `db.search_users` → كارت المستخدم + `user_manage_kb` (حظر/فك حظر/تعيين حد التزامن). بعد `adm:lim:{id}` → FSM يبعت رقم (0 = افتراضي).
- `adm:bans` → قائمة المحظورين.

### 3.10 `bot/middlewares.py`
- `UserTrackMiddleware`: upsert_user لكل update.
- `BanMiddleware`: المحظور → "تم حظرك من استخدام البوت".
- `ForceSubMiddleware` (لو FORCE_CHANNEL مضبوط): `bot.get_chat_member` → لو مش عضو → رسالة الانضمام + `force_sub_kb`. cb `checksub` يعيد الفحص. يُطبّق على الرسائل والكولباك. الأدمن مستثنى.

### 3.11 `main.py`
- تهيئة: config → Database.init → AkwamClient(base_url=settings.AKWAM_DOMAIN) → TTLCache → DownloadManager → Bot(aiogram؛ لو BOT_API_SERVER مضبوط: `TelegramBotAPIServer` من aiogram.client.session.aiohttp + `Bot(token, session=AiohttpSession(api=...))`) → Dispatcher بالـ routers والـ middlewares → `await client.close()` عند الإيقاف. طباعة لوج واضح عند الإقلاع.

## 4. متغيرات البيئة (.env.example)
```
BOT_TOKEN=
AKWAM_DOMAIN=https://akwam.it
ADMIN_IDS=123456789
FORCE_CHANNEL=@yourchannel
BOT_API_SERVER=http://telegram-bot-api:8081
TG_API_ID=
TG_API_HASH=
DEFAULT_MAX_CONCURRENT=1
CACHE_TTL_HOURS=6
EPISODES_PER_PAGE=20
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
```

## 5. النشر (وكيل C)
- `requirements.txt` (main): aiogram>=3.7, httpx, beautifulsoup4, lxml, aiosqlite, pydantic-settings, python-dotenv
- `Dockerfile`: python:3.11-slim، تثبيت requirements، نسخ الكود، CMD `python main.py`.
- `docker-compose.yml`: خدمتان:
  1. `telegram-bot-api`: الصورة `aiogram/telegram-bot-api:latest`، env: `TELEGRAM_API_ID`, `TELEGRAM_API_LOCAL_MODE`... (الصورة تقبل TELEGRAM_API_ID و TELEGRAM_API_HASH)، منفذ 8081، volume `tgbot-data`.
  2. `bot`: build .، env_file .env، depends_on telegram-bot-api، volumes للـ downloads والـ db.
- `README.md` بالعربي: شرح إنشاء البوت من BotFather، الحصول على API_ID/HASH من my.telegram.org، خطوات VPS (docker compose up -d)، خطوات Railway (خدمتان: واحدة بالصورة aiogram/telegram-bot-api وواحدة من الريبو، وربط BOT_API_SERVER بعنوان الداخلية)، ملاحظة أن الملفات حتى 2GB تعمل مع Local Server وبدونه الحد 50MB.

## 6. قواعد الجودة
- type hints في كل الدوال العامة؛ async صح في كل I/O.
- لا روابط downet تُخزن في DB أو كاش (تنتهي خلال 24h) — تُولّد عند الطلب فقط. الكاش مسموح لنتايج البحث وتفاصيل الصفحات (IDs فقط).
- معالجة أخطاء: فشل بحث/صفحة → رسالة عربية مفهومة + لا كراش.
- السكرابر (وكيل A) يجب أن يجتاز اختباراً حياً فعلياً على akwam.it قبل الالتزام (tests/test_scraper.py): بحث حقيقي + فيلم حقيقي + مسلسل + حلقة + get_direct_links.
