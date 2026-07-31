# تقرير تحليل themoviebox.xyz — موثق بطلبات حية

**الخلاصة:** الموقع واجهة Nuxt لمنصة MovieBox — كل البيانات من JSON API خالص: `https://h5-api.aoneroom.com/wefeed-h5api-bff/`. لا حماية، لا سيرفرات وسيطة — روابط MP4 مباشرة من أول طلب. جودات 360/480/720/1080 + ترجمات SRT بـ13 لغة (منها العربية) + نظام نسخ/دبلجات.

## المصادقة (حاسمة)
1. **Bearer JWT**: أي رد API يرجع هيدر `x-user: {"token":"eyJ..."}` — اطلب `search-suggest` مرة وخزّنه (صلاحية طويلة). البحث `subject/search` يتطلبه (يرفض غيره). جدّده عند 400/401.
2. **`play`/`download`**: **بدون أي توكن** + إلزامياً `Origin: https://videodownloader.site` — أي Origin آخر أو إرسال Authorization → قوائم فارغة!
3. الهيدرز القياسية: `X-Client-Info: {"timezone":"Africa/Cairo"}`, `X-Request-Lang: en|ar` (ar → عنوان ووصف عربي).
- الرد دائماً `{"code":0,"message":"ok","data":{...}}`.

## البحث
```
POST {api}/subject/search   (Bearer إلزامي)
Body: {"keyword":"..","page":1,"perPage":10,"subjectType":0}
```
- subjectType: 0=الكل 1=أفلام 2=مسلسلات. يعمل بالعربية. perPage>=10 (أقل قد يرجع فارغاً — خلل).
- pager: {hasMore, nextPage, totalCount}.
- items[]: `subjectId` (رقم كبير نصي), `subjectType`, `title`, `releaseDate`, `genre`, `cover.url` (pbcdnw.aoneroom.com — يعمل بدون referer), `imdbRatingValue`, `subtitles` (نص لغات), `detailPath`, `hasResource`, `dubs`.
- suggest (لجلب التوكن): `POST /subject/search-suggest {"keyword":"x","perPage":10}` (بدون Bearer).

## التفاصيل
`GET {api}/detail?detailPath={path}` (بدون توكن):
- `subject`: title, description, releaseDate, genre, cover, imdbRatingValue, subjectType, subtitles, **dubs[]**
- `resource.seasons[]` (مسلسلات): `{se, maxEp, allEp:"1,2,4.." أو فارغ=الكل, resolutions:[{resolution, epNum}]}`
- صفحة الويب: `https://themoviebox.xyz/detail/{detailPath}`
- **dubs[]** (النسخ): `{name, lanCode, type (0=دبلجة صوتية، 1=نسخة بترجمة مدمجة hardsub), subjectId, detailPath, original}` — كل نسخة subject مستقل بتفاصيله وجوداته. يوجد "Arabic sub" (hardsub عربي) وأحياناً دبلجات لغات.

## الجودات والروابط (MP4 مباشرة)
```
GET {api}/subject/play?subjectId={id}&se={S}&ep={E}&detailPath={path}
Headers: Origin: https://videodownloader.site   (بدون توكن!)
```
- أفلام: se=0&ep=0. مسلسلات: أرقام فعلية.
- `data.streams[]`: `{format:'MP4', url, resolutions:'1080', size, duration, codecName, vipLocked}` — **1080 مفتوحة في play** (في download مقفلة VIP بـ url فارغ) → استخدم play دائماً، تجاهل vipLocked:true/url فارغ.
- الرابط: `https://bcdnxw.hakunaymatata.com/resource/<hash>.mp4?sign=..&t=..` — **CDN يشترط `Referer: https://videodownloader.site/`** (بدونه 429!) — Range 206 مدعوم بالكامل. الصلاحية ~ساعات → اجلب عند الطلب.

## الترجمات (SRT منفصلة — 13 لغة)
ضمن رد play: `data.captions[]`: `{lan:'ar', lanName, url (موقّع CloudFront ~7 أيام، بدون referer), size, delay}`
- اللغات: ar, bn, en, es, fil, fr, hi, in_id, ms, pa, pt, ru, ur (+zh أحياناً).
- نسخ dubs من type=1 (hardsub) captionsها فارغة (الترجمة محروقة على الفيديو).

## أخرى
- `GET {api}/subject/trending?page=1&perPage=18` — الرائج (بدون Bearer).
- لا صفحات حلقات منفصلة ولا أزرار تالية/سابقة — الحلقة = play بـ se/ep.
- لا rate limiting ملحوظ.
