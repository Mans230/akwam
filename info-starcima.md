# تقرير تحليل starcima.com — موثق بطلبات حية

**الخلاصة:** Next.js فوق TMDB API مع بروكسي خاص `/api/tmdb/*` + APIs داخلية JSON للسيرفرات. كل شيء يعمل بـ requests عادي بدون حماية/تحدي. **لا يوجد نظام تحميل** (مشاهدة فقط) لكن `/api/extract` يُرجع روابط مباشرة (mp4/m3u8) من بعض السيرفرات.

## الوصول
- HTTP 200 بدون cookies/referer/JS challenge؛ لا rate limiting؛ دومين بديل معلن: starcima.cc
- الصفحات HTML عديمة الفائدة (client-side) — الاعتماد كله على JSON APIs

## البحث
`GET {base}/api/tmdb/search/multi?query={Q}&language=ar-SA&page={N}`
- النتيجة: `{id (tmdb), media_type:'movie'|'tv', title|name (عربي), original_title|original_name, poster_path, release_date|first_air_date, vote_average, overview}`
- pagination: معامل page + total_pages (20/صفحة)؛ البحث العربي ممتاز
- بوستر: `https://image.tmdb.org/t/p/w500{poster_path}` (يعمل بدون referer)
- بحث مدبلج منفصل: `GET /api/dubbed/search?q=..`

## التفاصيل
- فيلم: `/api/tmdb/movie/{id}?language=ar-SA&append_to_response=credits,videos,external_ids`
- مسلسل: `/api/tmdb/tv/{id}?language=ar-SA` → `number_of_seasons`, `seasons[]` (season_number, episode_count)
- حلقات موسم: `/api/tmdb/tv/{id}/season/{N}?language=ar-SA` (أسماء عربية، still_path)
- رابط مشاركة للمستخدم: `{base}/media/{id}?type=movie|tv`
- رابط مشاهدة: `{base}/watch/{tmdbId}?type=movie|tv&title={عربي}&en={إنجليزي}[&season=N&ep=M]`

## سيرفرات المشاهدة
`GET /api/arabic-sources?title={عربي}&type={movie|tv}&englishTitle={en}&year={YYYY}[&season=N&episode=M&absEpisode=K&seasonEpCount=C]`
→ `{"servers":[{"name":"سيرفر 1","embedUrl":"https://...","isTopPriority":false}, ...]}`
- قد يرجع حتى 20 سيرفراً؛ الأسماء دائماً "سيرفر N"
- **روابط akwam.it/watch/... تظهر ضمن النتايج أحياناً** (قابلة لإعادة الاستخدام بسكرابر أكوام)
- showbox وvidzee APIs معطّلان حالياً — لا تعتمد عليهما
- سيرفر احتياطي ثابت (يُضاف client-side): `https://www.vidking.net/embed/movie/{tmdbId}` أو `/embed/tv/{tmdbId}/{season}/{episode}`

## استخراج الرابط المباشر
`GET {base}/api/extract?url={embedUrl}` (أو `/x/?extract=`)
- نجاح: `{"type":"hls"|"mp4","directUrl":"https://...","url":"/api/proxy/...?host=..&sig=.."}`
- **الناجح مؤكداً**: streamwish, updown.icu, vidtube
- **الفاشل**: filemoon, voe, mixdrop, streamtape, luluvdo, d0o0d, uqload, vk
- directUrl موقّت وقد يكون IP-bound → للـ hls استخدم `/api/proxy/...` (يعمل من أي IP، proxy_url = base + url النسبي)
- mp4 المباشر: Range 206 مدعوم + Content-Length كامل (مثال: حلقة 1.18GB) — قابل للتحميل المتوازي
- الصلاحية ~24-36 ساعة → استخرج عند الطلب، لا تخزّن

## الترجمات
`GET /api/wyzie-subs?id={tmdbId}[&season&episode]` → روابط SRT عربية مباشرة

## المدبلج
- `/api/dubbed/catalog?page=`, `/api/dubbed/search?q=`, `/api/dubbed/episodes?series=` (مصدر ArabicToons — لم يُستكشف بعمق؛ افحص الاستجابات الحية عند التنفيذ)

## ملاحظات تنفيذية
- لا تخزن روابط مستخرجة (موقّتة) — خزّن tmdb ids والسياقات فقط
- تعامل بتسامح مع فشل extract (يرجع {"error":...})
- مقارنة بأكوام: starcima = JSON كامل وبحث أقوى؛ أكوام = جودات تحميل صريحة
