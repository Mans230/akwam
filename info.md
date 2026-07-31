# تقرير تحليل موقع AKWAM (akwam.it) — موثق ومتحقق منه بطلبات حية

**الخلاصة:** الموقع مبني على Laravel، Cloudflare موجود كـ CDN فقط بدون JS challenge. يكفي `requests/httpx + BeautifulSoup` — لا حاجة لـ Playwright. سيرفرات المشاهدة ذاتية الاستضافة على `downet.net` وروابط MP4 المباشرة مكشوفة في HTML.

## 1. الوصول
- `GET https://akwam.it/` بـ UA متصفح عادي → 200، بدون cookies/referer مطلوبة.
- لا rate limiting ملحوظ (10 طلبات متتالية كلها 200).
- `akwam.to` مرآة تعمل. الدومين يجب أن يكون قابلاً للإعداد.
- UA: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36`

## 2. البحث
- `GET {base}/search?q=QUERY` — فلاتر GET اختيارية: `section` (movie|series|show|...), `year`, `rating`, `formats`, `quality`.
- **لا يوجد pagination في البحث** (حد ~24 نتيجة).
- بنية النتيجة `div.entry-box`:
  - الرابط/العنوان: `h3.entry-title a` (href + text)
  - الصورة: `.entry-image img` → خذ `data-src` وليس `src` (lazy loading)
  - التقييم: `span.label.rating` (نص)
  - الجودة: `span.label.quality`
  - السنة: أول `span.badge.badge-secondary`
  - النوع من الرابط: `/movie/{id}/...` أو `/series/{id}/...`

## 3. صفحة الفيلم `/movie/{id}/{slug}` (الـ id وحده يكفي)
- العنوان: `h1.entry-title`؛ البوستر: `meta[property="og:image"]`
- التقييم بجانب `i.icon-star` بصيغة "10 / 6.6"
- قسم التحميل: `div.widget` فيه `header#downloads`:
  - تبويبات الجودة: `ul.header-tabs li a` → href مثل `#tab-5` والنص `1080p`
  - كل تبويب `div.tab-content.quality#tab-5` فيه صفوف `div[data-server][data-quality]`:
    - مشاهدة: `a.link-btn.link-show` → `/watch/{file_id}/{content_id}/{slug}`
    - تحميل: `a.link-btn.link-download` → `/download/{file_id}/{content_id}/{slug}` + الحجم في `span.font-size-14.mr-auto`
  - **file_id مختلف لكل جودة**؛ content_id = id الفيلم. `/download/{fid}/{cid}` يعمل بدون slug.

## 4. تتبع التحميل → الرابط المباشر
- `GET /download/{fid}/{cid}` → صفحة وسيطة واحدة `div.page-redirect` (لا عداد/لا captcha/لا POST)
- الرابط المباشر: `div.page-redirect div.btn-loader a[href*="downet.net/download/"]`
- الصيغة: `https://s{srv}d{n}.downet.net/download/{expiry_epoch}/{hash}/{file}.mp4` — **ينتهي بعد ~24 ساعة** → لا يُخزن، يُولّد عند الطلب.
- HEAD → 200 + Content-Length + Accept-Ranges: bytes؛ Range request بدون referer → 206. قابل للتحميل المباشر.

## 5. المسلسل والحلقات
- `/series/{id}/{slug}` — **كل موسم مدخل /series مستقل** (لا روابط بين المواسم) → المواسم تُجمع من البحث.
- الحلقات كلها في صفحة واحدة بدون pagination: `div.widget#series-episodes` → كل حلقة `div.bg-primary2`:
  - الرابط/العنوان: `h2 a` → `/episode/{episode_id}/{series-slug}/الحلقة-{N}`
  - رقم الحلقة من `/الحلقة-(\d+)` أو من النص
  - الثمبنيل: `img` في `src` مباشرة هنا
  - `/episode/{id}` وحده يعمل (redirect للكامل)

## 6. صفحة الحلقة `/episode/{id}/...`
- العنوان: `h1.entry-title`؛ رابط المسلسل: `a[href*="/series/"]`
- قسم التحميل مطابق لصفحة الفيلم تماماً (نفس tabs/link-btn)
- زر الحلقة التالية/السابقة: `h3.entry-title` نصه "الحلقة التالية"/"الحلقة السابقة" داخل `a[href*="/episode/"]`

## 7. صفحة المشاهدة `/watch/{fid}/{cid}`
- **لا سيرفرات خارجية إطلاقاً** — مشغل HTML5 أصلي:
  - `video#player source` → لكل source: `src` = رابط MP4 مباشر (موقّت ~24h)، `size` = الدقة (1080/720/480)
- **طلب /watch واحد يعطي كل الجودات كروابط مباشرة** — الاستراتيجية المثلى للاستخراج.

## 8. الصور
- `https://img.downet.net/thumb/{WxH}/uploads/{file}` — تعمل مباشرة بدون referer.
- في القوائم اقرأ `data-src or src`؛ في صفحات الحلقات `src`.

## 9. توصيات
1. httpx + BeautifulSoup كافيان 100%.
2. خزّن فقط content_id/file_ids/slugs — لا تخزن روابط downet الموقّتة.
3. id-only URLs تعمل في كل مكان.
4. أضف retry/backoff بسيط.
5. الدومين قابل للإعداد (AKWAM_DOMAIN).
