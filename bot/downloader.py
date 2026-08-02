"""مدير التحميلات — طابور لكل يوزر + تقدم لايف + إلغاء (حسب SPEC 3.6)."""
from __future__ import annotations

import asyncio
import glob
import logging
import math
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message, URLInputFile

from .db import Database
from .keyboards import cancel_kb
from .textutil import CAPTION_LIMIT, esc as _esc, truncate_html

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CHUNK = 256 * 1024
_EDIT_EVERY = 5.0  # ثواني بين تحديثات رسالة التقدم
_MIN_PARALLEL_SIZE = 10 * 1024**2  # التحميل المتوازي للملفات الأكبر من 10MB فقط
_SEGMENT_RETRIES = 3  # محاولات لكل قطعة قبل الرجوع للتدفق الواحد
_DASH_CONCURRENCY = 10  # تحميلات سجمنتات DASH المتزامنة
_DASH_RETRIES = 3  # محاولات لكل سجمنت DASH
_SPLIT_THRESHOLD = 1990 * 1024**2  # ~2GB بهامش أمان — فوقه يتقسّم لأجزاء (F5)


@dataclass
class DownloadJob:
    task_id: str  # uuid4 hex[:12]
    title: str  # اسم يظهر لليوزر (فيلم/حلقة + جودة) — بصيغة "الاسم (الجودة)"
    url: str  # رابط downet المباشر
    caption: str  # كابشن الفيديو
    thumb_url: str | None = None
    referer: str | None = None  # هيدر Referer للمواقع اللي بتطلبه (مثل موفي بوكس)
    dash_url: str | None = None  # رابط MPD احتياطي (موفي بوكس) لو الـ CDN حظر الـ IP
    dash_res: int | None = None  # الدقة المطلوبة من الـ DASH (مطابقة الجودة المختارة)
    hls_url: str | None = None  # رابط m3u8 احتياطي (موفي بوكس) لو مفيش DASH
    waited: bool = False  # اتملى في الطابور — عشان رسالة «جه دورك» عند البدء (F4)


def _fail_reason(e: Exception) -> str:
    """رسالة عربية مفهومة لسبب فشل التحميل بدل اسم الاستثناء الغامض."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code if e.response is not None else "؟"
        return (
            f"السيرفر رفض التحميل ({code}) — غالبًا حظر مؤقت لآي بي Railway على المحتوى ده "
            "ومفيش مرآة تانية له 😔 جرّب جودة مختلفة، أو دوّر على نفس العنوان في أكوام/ستار سيما."
        )
    if isinstance(e, httpx.TransportError):
        return "حصلت مشكلة اتصال بالشبكة أثناء التحميل — جرب تاني بعد شوية."
    return f"{type(e).__name__} — جرب تاني."


def _fmt_size(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n:.0f} B"


def _extract_quality(title: str) -> str:
    m = re.search(r"\(([^()]+)\)\s*$", title)
    return m.group(1) if m else "-"


# ---------- بارس MPD (DASH) — مسار موفي بوكس الاحتياطي ----------


def _local(tag: str) -> str:
    """اسم العنصر بدون الـ namespace (MPD بنيم سبيس urn:mpeg:dash:schema:mpd:2011)."""
    return tag.rsplit("}", 1)[-1]


def _find_child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


@dataclass
class _DashTrack:
    rep_id: str
    res: int | None  # ارتفاع الفيديو (None للأوديو)
    init_url: str
    seg_urls: list[str]


_VIDEO_CODECS = ("hev1", "hvc1", "avc1", "av01", "vp9", "vp09")
_AUDIO_CODECS = ("mp4a", "ac-3", "ec-3", "opus")


def _dash_kind(aset: ET.Element, rep: ET.Element) -> str | None:
    """تصنيف الـ Representation: فيديو وإلا أوديو وإلا None (نتجاهلها)."""
    ct = (aset.get("contentType") or rep.get("mimeType") or aset.get("mimeType") or "").lower()
    if ct.startswith("video"):
        return "video"
    if ct.startswith("audio"):
        return "audio"
    codecs = (rep.get("codecs") or aset.get("codecs") or "").lower()
    if codecs.startswith(_VIDEO_CODECS):
        return "video"
    if codecs.startswith(_AUDIO_CODECS):
        return "audio"
    return None


def _expand_number(url: str, n: int) -> str:
    """استبدالات قوالب السجمنتات: $Number%05d$ ثم $Number$."""
    url = re.sub(
        r"\$Number%0?(\d+)d\$",
        lambda m: f"{n:0{int(m.group(1))}d}",
        url,
    )
    return url.replace("$Number$", str(n))


def _build_dash_track(rep_id: str, res: int | None, template: ET.Element) -> _DashTrack:
    """يبني init URL + قائمة URLs السجمنتات من SegmentTemplate/SegmentTimeline.

    الروابط في attributes — ElementTree بيفك &amp; تلقائياً (ممنوع unescape يدوي).
    عدد السجمنتات = مجموع (1 + r) على كل عنصر S في SegmentTimeline.
    """
    init_t = template.get("initialization")
    media_t = template.get("media")
    if not init_t or not media_t:
        raise RuntimeError("SegmentTemplate ناقص (initialization/media) في MPD")
    start = int(template.get("startNumber") or 1)
    count = 0
    timeline = _find_child(template, "SegmentTimeline")
    if timeline is not None:
        for s in timeline:
            if _local(s.tag) == "S":
                count += 1 + int(s.get("r") or 0)
    if count <= 0:
        raise RuntimeError("مفيش سجمنتات في SegmentTimeline بتاع MPD")
    init_url = init_t.replace("$RepresentationID$", rep_id)
    seg_urls = [
        _expand_number(media_t.replace("$RepresentationID$", rep_id), n)
        for n in range(start, start + count)
    ]
    return _DashTrack(rep_id=rep_id, res=res, init_url=init_url, seg_urls=seg_urls)


def _parse_mpd(mpd_xml: str, dash_res: int | None) -> tuple[_DashTrack, _DashTrack]:
    """يرجع (مسار الفيديو الأنسب لـ dash_res, أول مسار أوديو) من MPD.

    اختيار الفيديو: تطابق تام → وإلا الأقرب، وعند التعادل الأقل دقة.
    """
    root = ET.fromstring(mpd_xml)
    videos: list[tuple[str, int | None, ET.Element]] = []
    audios: list[tuple[str, int | None, ET.Element]] = []
    for aset in root.iter():
        if _local(aset.tag) != "AdaptationSet":
            continue
        aset_template = _find_child(aset, "SegmentTemplate")
        for rep in aset:
            if _local(rep.tag) != "Representation":
                continue
            kind = _dash_kind(aset, rep)
            if kind is None:
                continue
            template = _find_child(rep, "SegmentTemplate") or aset_template
            if template is None:
                continue
            try:
                height = int(rep.get("height") or 0) or None
            except ValueError:
                height = None
            entry = (str(rep.get("id") or "0"), height, template)
            (videos if kind == "video" else audios).append(entry)
    if not videos or not audios:
        raise RuntimeError("MPD مفيهوش مسارات فيديو/أوديو صالحة")

    if dash_res is None:
        chosen_v = max(videos, key=lambda v: v[1] or 0)
    else:
        exact = [v for v in videos if v[1] == dash_res]
        chosen_v = exact[0] if exact else min(
            videos, key=lambda v: (abs((v[1] or 0) - dash_res), v[1] or 0)
        )
    video = _build_dash_track(*chosen_v)
    audio = _build_dash_track(*audios[0])
    return video, audio


class DownloadManager:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        download_dir: str,
        default_limit: int,
        segments: int | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.download_dir = download_dir
        self.default_limit = default_limit
        if segments is None:
            from .config import settings

            segments = getattr(settings, "DOWNLOAD_SEGMENTS", 8)
        self.segments = max(1, int(segments))
        os.makedirs(self.download_dir, exist_ok=True)

        self._queues: dict[int, asyncio.Queue[DownloadJob]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._running: dict[str, asyncio.Task] = {}  # task_id -> task
        self._owners: dict[str, tuple[int, int]] = {}  # task_id -> (user_id, chat_id)
        self._active_by_user: dict[int, set[str]] = {}

    # ---------- واجهة عامة ----------

    async def enqueue(self, user_id: int, chat_id: int, job: DownloadJob) -> None:
        """يضيف مهمة لطابور اليوزر ويبلّغه لو لسه في طابور الانتظار."""
        queue = self._queues.setdefault(user_id, asyncio.Queue())
        self._owners[job.task_id] = (user_id, chat_id)
        limit = await self._limit_for(user_id)
        busy = self.active_count(user_id)
        waiting = queue.qsize()
        if busy >= limit or waiting:
            job.waited = True
            await self.bot.send_message(
                chat_id,
                f"📥 «{_esc(job.title)}» في طابور الانتظار (رقم {waiting + 1}) — "
                "هيتنفذ لوحده أول ما تحميلك الحالي يخلص.",
            )
        await queue.put(job)
        worker = self._workers.get(user_id)
        if worker is None or worker.done():
            self._workers[user_id] = asyncio.create_task(self._user_worker(user_id))

    async def cancel(self, task_id: str, user_id: int) -> bool:
        """يلغي مهمة شغالة أو لسه في الطابور."""
        owner = self._owners.get(task_id)
        if owner is None or owner[0] != user_id:
            return False
        task = self._running.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        # لو لسه في الطابور: شيلها من هناك
        queue = self._queues.get(user_id)
        if queue is not None:
            kept: list[DownloadJob] = []
            removed = False
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item.task_id == task_id and not removed:
                    removed = True
                    self._owners.pop(task_id, None)
                    await self.db.log_download(
                        user_id, item.title, _extract_quality(item.title), "cancelled"
                    )
                    continue
                kept.append(item)
            for item in kept:
                queue.put_nowait(item)
            return removed
        return False

    def active_count(self, user_id: int) -> int:
        return len(self._active_by_user.get(user_id, set()))

    def total_active(self) -> int:
        """عدد التحميلات النشطة حالياً لكل المستخدمين (للأدمن)."""
        return sum(len(s) for s in self._active_by_user.values())

    async def shutdown(self) -> None:
        """إلغاء كل مهام التحميل عند إيقاف البوت."""
        for worker in self._workers.values():
            worker.cancel()
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(
            *(t for t in list(self._workers.values()) + list(self._running.values())),
            return_exceptions=True,
        )

    # ---------- داخلي ----------

    async def _limit_for(self, user_id: int) -> int:
        limit = await self.db.get_user_limit(user_id)
        return limit if limit else self.default_limit

    async def _segments_for(self, user_id: int) -> tuple[int, bool]:
        """(عدد القطع, هل بريميوم) — البريميوم ياخد PREMIUM_SEGMENTS."""
        try:
            premium = await self.db.is_premium(user_id)
        except Exception:  # noqa: BLE001
            log.exception("is_premium check failed for %s", user_id)
            premium = False
        if premium:
            from .config import settings

            return max(1, int(getattr(settings, "PREMIUM_SEGMENTS", 16))), True
        return self.segments, False

    async def _user_worker(self, user_id: int) -> None:
        queue = self._queues[user_id]
        while True:
            job = await queue.get()
            limit = await self._limit_for(user_id)
            while self.active_count(user_id) >= limit:
                await asyncio.sleep(1)
            task = asyncio.create_task(self._run_job(user_id, job))
            self._running[job.task_id] = task
            self._active_by_user.setdefault(user_id, set()).add(job.task_id)
            task.add_done_callback(
                lambda _t, uid=user_id, tid=job.task_id: self._on_done(uid, tid)
            )

    def _on_done(self, user_id: int, task_id: str) -> None:
        self._active_by_user.get(user_id, set()).discard(task_id)
        self._running.pop(task_id, None)
        self._owners.pop(task_id, None)

    async def _run_job(self, user_id: int, job: DownloadJob) -> None:
        _, chat_id = self._owners.get(job.task_id, (user_id, user_id))
        quality = _extract_quality(job.title)
        path = os.path.join(self.download_dir, f"{job.task_id}.mp4")
        status_msg: Message | None = None
        try:
            start_txt = (
                f"🚀 جه دورك! بدأ تحميل «{_esc(job.title)}»…"
                if job.waited
                else f"⏳ بدأ تحميل «{_esc(job.title)}»…"
            )
            status_msg = await self.bot.send_message(
                chat_id,
                start_txt,
                reply_markup=cancel_kb(job.task_id),
            )
            segments, premium = await self._segments_for(user_id)
            await self._download_file(job, path, status_msg, segments=segments, premium=premium)
            if os.path.getsize(path) > _SPLIT_THRESHOLD:
                await self._split_and_upload(job, path, chat_id, status_msg)
            else:
                await self._upload_file(job, path, chat_id, status_msg)
            await self.db.log_download(user_id, job.title, quality, "done")
            await self._safe_edit(status_msg, f"✅ خلص واتبعت: «{_esc(job.title)}»")
        except asyncio.CancelledError:
            await self.db.log_download(user_id, job.title, quality, "cancelled")
            await self._safe_edit(status_msg, f"❌ اتلغى تحميل «{_esc(job.title)}»")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("download failed: %s", job.task_id)
            await self.db.log_download(user_id, job.title, quality, "failed")
            await self._safe_edit(
                status_msg,
                f"❌ فشل تحميل «{_esc(job.title)}»\n{_fail_reason(e)}",
            )
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            for part in glob.glob(f"{path}.seg*.mp4"):
                try:
                    os.remove(part)
                except OSError:
                    pass

    async def _download_file(
        self,
        job: DownloadJob,
        path: str,
        status_msg: Message,
        segments: int | None = None,
        premium: bool = False,
    ) -> None:
        if segments is None:
            segments = self.segments
        try:
            await self._download_with_client(
                job, path, status_msg, segments, premium, verify=True
            )
        except asyncio.CancelledError:
            raise
        except httpx.ConnectError as exc:
            # بعض الـ CDNs (زي downet.net بتاع روابط أكوام) بيبعت سلسلة شهادات
            # ناقصة، فيفشل تحقق TLS رغم إن الرابط نفسه شغال — نعيد المحاولة
            # مرة واحدة بدون تحقق بدل ما التحميل كله يفشل.
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                raise
            log.warning(
                "SSL certificate verify failed for %s — retrying with verify=False (%s)",
                job.url,
                job.task_id,
            )
            await self._download_with_client(
                job, path, status_msg, segments, premium, verify=False
            )

    async def _download_with_client(
        self,
        job: DownloadJob,
        path: str,
        status_msg: Message,
        segments: int,
        premium: bool,
        verify: bool,
    ) -> None:
        req_headers = {"User-Agent": _UA}
        if job.referer:
            req_headers["Referer"] = job.referer
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=300.0),
            headers=req_headers,
            verify=verify,
        ) as client:
            try:
                total, ranges_ok = await self._probe(client, job.url)
                if ranges_ok and total > _MIN_PARALLEL_SIZE and segments > 1:
                    try:
                        await self._download_parallel(
                            client, job, path, status_msg, total, segments, premium
                        )
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "parallel download failed, falling back to single stream: %s",
                            job.task_id,
                        )
                await self._download_stream(client, job, path, status_msg)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPStatusError, httpx.TransportError):
                # الـ CDN بيحظر آي بيهات الداتاسنتر (403) — جرّب المرايا: DASH ثم HLS
                if job.dash_url:
                    log.warning(
                        "MP4 download failed (%s), trying DASH fallback: %s",
                        job.url,
                        job.task_id,
                    )
                    try:
                        await self._download_dash(client, job, path, status_msg)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if not job.hls_url:
                            raise
                        log.exception(
                            "DASH fallback failed, trying HLS: %s", job.task_id
                        )
                if not job.hls_url:
                    raise
                log.warning(
                    "MP4 download failed (%s), trying HLS fallback: %s",
                    job.url,
                    job.task_id,
                )
                await self._download_hls(job, path, status_msg)

    async def _probe(self, client: httpx.AsyncClient, url: str) -> tuple[int, bool]:
        """HEAD على الرابط: يرجع (الحجم, هل السيرفر يدعم Range). أي فشل = وضع عادي."""
        try:
            resp = await client.head(url)
            if resp.status_code >= 400:
                return 0, False
            total = int(resp.headers.get("content-length") or 0)
            accepts = resp.headers.get("accept-ranges", "").lower() == "bytes"
            return total, accepts and total > 0
        except Exception:
            log.warning("HEAD probe failed, using single stream", exc_info=True)
            return 0, False

    async def _download_stream(
        self, client: httpx.AsyncClient, job: DownloadJob, path: str, status_msg: Message
    ) -> None:
        """التدفق الواحد العادي (السلوك الأصلي)."""
        downloaded = 0
        last_edit = time.monotonic()
        last_bytes = 0
        async with client.stream("GET", job.url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            with open(path, "wb") as f:
                async for chunk in resp.aiter_bytes(_CHUNK):
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_edit >= _EDIT_EVERY:
                        speed = (downloaded - last_bytes) / (now - last_edit) / 1024**2
                        percent = f"{downloaded / total * 100:.0f}%" if total else "؟"
                        total_txt = _fmt_size(total) if total else "غير معروف"
                        await self._safe_edit(
                            status_msg,
                            f"⬇️ بيتم تحميل «{_esc(job.title)}»\n"
                            f"📊 {percent} — 🚀 {speed:.1f} MB/s\n"
                            f"💾 {_fmt_size(downloaded)} / {total_txt}\n"
                            "📥 تحميل عادي",
                            with_kb=job.task_id,
                        )
                        last_edit = now
                        last_bytes = downloaded

    # ---------- تحميل متوازي متعدد القطع (IDM-style) ----------

    async def _download_parallel(
        self,
        client: httpx.AsyncClient,
        job: DownloadJob,
        path: str,
        status_msg: Message,
        total: int,
        segments: int | None = None,
        premium: bool = False,
    ) -> None:
        if segments is None:
            segments = self.segments
        n = max(1, min(segments, total // 1024**2))
        base = total // n
        ranges = [
            (i * base, (i + 1) * base - 1 if i < n - 1 else total - 1)
            for i in range(n)
        ]
        part_paths = [
            os.path.join(self.download_dir, f"{job.task_id}.part{i}") for i in range(n)
        ]
        progress = [0] * n
        reporter = asyncio.create_task(
            self._report_parallel(job, status_msg, progress, total, n, premium)
        )
        try:
            await asyncio.gather(
                *(
                    self._download_segment(
                        client, job.url, start, end, part_paths[i], progress, i
                    )
                    for i, (start, end) in enumerate(ranges)
                )
            )
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
            await asyncio.to_thread(self._merge_parts, part_paths, path)
        finally:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
            for part in part_paths:
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass

    async def _download_segment(
        self,
        client: httpx.AsyncClient,
        url: str,
        start: int,
        end: int,
        part_path: str,
        progress: list[int],
        idx: int,
    ) -> None:
        """يحمّل قطعة [start, end] مع retry (حتى 3 محاولات بـ backoff)."""
        expected = end - start + 1
        for attempt in range(1, _SEGMENT_RETRIES + 1):
            progress[idx] = 0
            try:
                async with client.stream(
                    "GET", url, headers={"Range": f"bytes={start}-{end}"}
                ) as resp:
                    if resp.status_code != 206:
                        raise httpx.HTTPStatusError(
                            f"expected 206, got {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    written = 0
                    with open(part_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(_CHUNK):
                            f.write(chunk)
                            written += len(chunk)
                            progress[idx] = written
                    if written != expected:
                        raise IOError(f"segment {idx}: got {written}, expected {expected}")
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == _SEGMENT_RETRIES:
                    raise
                await asyncio.sleep(2 ** (attempt - 1))

    @staticmethod
    def _merge_parts(part_paths: list[str], path: str) -> None:
        with open(path, "wb") as out:
            for part in part_paths:
                with open(part, "rb") as f:
                    while chunk := f.read(1024**2):
                        out.write(chunk)

    async def _report_parallel(
        self,
        job: DownloadJob,
        status_msg: Message,
        progress: list[int],
        total: int,
        n: int,
        premium: bool = False,
    ) -> None:
        """تحديث رسالة التقدم من مجموع القطع بنفس إيقاع ~5 ثواني."""
        mode = (
            f"⚡ تحميل متوازي ⭐ بريميوم ({n} قطعة)"
            if premium
            else f"⚡ تحميل متوازي ({n} قطع)"
        )
        last_edit = time.monotonic()
        last_bytes = 0
        while True:
            await asyncio.sleep(_EDIT_EVERY)
            downloaded = sum(progress)
            now = time.monotonic()
            speed = (downloaded - last_bytes) / (now - last_edit) / 1024**2
            percent = f"{downloaded / total * 100:.0f}%"
            await self._safe_edit(
                status_msg,
                f"⬇️ بيتم تحميل «{_esc(job.title)}»\n"
                f"📊 {percent} — 🚀 {speed:.1f} MB/s\n"
                f"💾 {_fmt_size(downloaded)} / {_fmt_size(total)}\n"
                f"{mode}",
                with_kb=job.task_id,
            )
            last_edit = now
            last_bytes = downloaded

    # ---------- مسار DASH الاحتياطي (موفي بوكس — CDN بيحظر الداتاسنتر) ----------

    async def _download_dash(
        self, client: httpx.AsyncClient, job: DownloadJob, path: str, status_msg: Message
    ) -> None:
        """تحميل DASH كامل: MPD → سجمنتات → concat → remux بـ ffmpeg إلى path."""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg غير مثبت في الصورة")
        resp = await client.get(job.dash_url)
        resp.raise_for_status()
        video_track, audio_track = _parse_mpd(resp.text, job.dash_res)
        res_label = video_track.res or job.dash_res or 0

        tmp_dir = f"{path}.dash_tmp"
        os.makedirs(tmp_dir, exist_ok=True)
        video_out = os.path.join(tmp_dir, "video.m4s")
        audio_out = os.path.join(tmp_dir, "audio.m4s")
        try:
            tracks = (("v", video_track, video_out), ("a", audio_track, audio_out))
            total = sum(1 + len(track.seg_urls) for _, track, _ in tracks)
            done = [0]
            done_bytes = [0]
            sem = asyncio.Semaphore(_DASH_CONCURRENCY)

            async def _fetch(url: str, dest: str) -> None:
                for attempt in range(1, _DASH_RETRIES + 1):
                    try:
                        async with sem:
                            r = await client.get(url)
                            r.raise_for_status()
                            data = r.content
                        with open(dest, "wb") as f:
                            f.write(data)
                        done[0] += 1
                        done_bytes[0] += len(data)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if attempt == _DASH_RETRIES:
                            raise
                        await asyncio.sleep(2 ** (attempt - 1))

            downloads: list[tuple[str, str]] = []
            for prefix, track, _ in tracks:
                downloads.append((track.init_url, os.path.join(tmp_dir, f"{prefix}_init.m4s")))
                for i, seg_url in enumerate(track.seg_urls, start=1):
                    downloads.append((seg_url, os.path.join(tmp_dir, f"{prefix}_{i:06d}.m4s")))

            reporter = asyncio.create_task(
                self._report_dash(job, status_msg, done, done_bytes, total, res_label)
            )
            try:
                await asyncio.gather(*(_fetch(url, dest) for url, dest in downloads))
            finally:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)

            # concat بترتيب الأرقام (init أولاً)
            for prefix, track, out in tracks:
                with open(out, "wb") as dst:
                    with open(os.path.join(tmp_dir, f"{prefix}_init.m4s"), "rb") as f:
                        shutil.copyfileobj(f, dst)
                    for i in range(1, len(track.seg_urls) + 1):
                        with open(os.path.join(tmp_dir, f"{prefix}_{i:06d}.m4s"), "rb") as f:
                            shutil.copyfileobj(f, dst)

            # remux: h265/aac copy داخل mp4 + faststart
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", video_out, "-i", audio_out,
                "-c", "copy", "-movflags", "+faststart", path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg remux فشل ({proc.returncode}): {stderr.decode(errors='replace')[:300]}"
                )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---------- مسار HLS الاحتياطي (موفي بوكس — لما مفيش DASH) ----------

    async def _download_hls(
        self, job: DownloadJob, path: str, status_msg: Message
    ) -> None:
        """تحميل HLS كامل بـ ffmpeg: m3u8 عبر بروكسي الـ API وسجمنتات .ts من sacdn (غير محظور)."""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg غير مثبت في الصورة")
        base_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", job.hls_url, "-c", "copy", "-movflags", "+faststart",
        ]
        # سجمنتات .ts صوتها ADTS — محتاجة bsf عشان تتعبّى في mp4؛ لو فشل نجرّب من غيره
        attempts = [base_cmd + ["-bsf:a", "aac_adtstoasc", path], base_cmd + [path]]
        last_err = ""
        for cmd in attempts:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            reporter = asyncio.create_task(self._report_hls(job, path, status_msg))
            try:
                _, stderr = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
            finally:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            if proc.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
                return
            last_err = stderr.decode(errors="replace")[:300]
            log.warning("HLS ffmpeg attempt failed (%s): %s", proc.returncode, last_err)
        raise RuntimeError(f"ffmpeg HLS فشل: {last_err}")

    async def _report_hls(self, job: DownloadJob, path: str, status_msg: Message) -> None:
        """تقدم تقريبي لتحميل HLS من نمو حجم الملف على الديسك."""
        last_edit = time.monotonic()
        last_bytes = 0
        while True:
            await asyncio.sleep(_EDIT_EVERY)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            now = time.monotonic()
            speed = (size - last_bytes) / max(now - last_edit, 0.1) / 1024**2
            await self._safe_edit(
                status_msg,
                f"⬇️ بيتم تحميل «{_esc(job.title)}»\n"
                f"📊 {_fmt_size(size)} (HLS)\n"
                f"🚀 {speed:.1f} MB/s",
                with_kb=job.task_id,
            )
            last_edit = now
            last_bytes = size

    async def _report_dash(
        self,
        job: DownloadJob,
        status_msg: Message,
        done: list[int],
        done_bytes: list[int],
        total: int,
        res_label: int,
    ) -> None:
        """تحديث رسالة التقدم لتحميل DASH بنفس إيقاع ~5 ثواني."""
        last_edit = time.monotonic()
        last_bytes = 0
        while True:
            await asyncio.sleep(_EDIT_EVERY)
            now = time.monotonic()
            speed = (done_bytes[0] - last_bytes) / (now - last_edit) / 1024**2
            await self._safe_edit(
                status_msg,
                f"⬇️ بيتم تحميل «{_esc(job.title)}»\n"
                f"📊 {done[0]}/{total} قطعة (DASH {res_label}p)\n"
                f"🚀 {speed:.1f} MB/s",
                with_kb=job.task_id,
            )
            last_edit = now
            last_bytes = done_bytes[0]

    async def _upload_file(
        self,
        job: DownloadJob,
        path: str,
        chat_id: int,
        status_msg: Message,
        caption: str | None = None,
        label: str | None = None,
    ) -> None:
        display = label or job.title
        await self._safe_edit(
            status_msg,
            f"📤 بيترفع على تليجرام: «{_esc(display)}»\n(ممكن ياخد وقت حسب حجم الملف)",
            with_kb=job.task_id,
        )
        started = time.monotonic()
        stop = asyncio.Event()

        async def _tick() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_EDIT_EVERY)
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - started)
                    await self._safe_edit(
                        status_msg,
                        f"📤 بيترفع على تليجرام: «{_esc(display)}»\n⏱ مر {elapsed} ثانية…",
                        with_kb=job.task_id,
                    )

        ticker = asyncio.create_task(_tick())
        try:
            thumbnail = URLInputFile(job.thumb_url) if job.thumb_url else None
            await self.bot.send_video(
                chat_id,
                FSInputFile(path),
                caption=truncate_html(caption if caption is not None else job.caption, CAPTION_LIMIT),
                supports_streaming=True,
                thumbnail=thumbnail,
            )
        finally:
            stop.set()
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    # ---------- تقسيم الملفات الأكبر من 2GB (F5) ----------

    async def _probe_duration(self, path: str) -> float:
        """مدة الفيديو بالثواني من ffprobe (0 لو مقدرناش)."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        try:
            return float(out.decode().strip())
        except ValueError:
            return 0.0

    async def _split_and_upload(
        self, job: DownloadJob, path: str, chat_id: int, status_msg: Message
    ) -> None:
        """تقسيم ملف >2GB بـ ffmpeg segment muxer (بدون إعادة ترميز) ورفع الأجزاء ورا بعض."""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("الملف أكبر من 2 جيجا و ffmpeg مش متاح للتقسيم")
        size = os.path.getsize(path)
        n_parts = math.ceil(size / _SPLIT_THRESHOLD)
        duration = await self._probe_duration(path)
        if duration <= 0:
            raise RuntimeError("مقدرتش أحدد مدة الفيديو عشان أقسّمه")
        seg_time = max(1, math.ceil(duration / n_parts))
        pattern = f"{path}.seg%03d.mp4"
        await self._safe_edit(
            status_msg,
            f"✂️ «{_esc(job.title)}» أكبر من 2 جيجا — بيتم تقسيمه لـ {n_parts} جزء…",
        )
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", path, "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_time", str(seg_time),
            "-reset_timestamps", "1", pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg تقسيم فشل ({proc.returncode}): {stderr.decode(errors='replace')[:300]}"
            )
        parts = sorted(glob.glob(f"{path}.seg*.mp4"))
        if not parts:
            raise RuntimeError("التقسيم مانتجش أجزاء")
        try:
            for i, part in enumerate(parts, 1):
                await self._upload_file(
                    job,
                    part,
                    chat_id,
                    status_msg,
                    caption=f"{job.caption}\n📦 الجزء {i}/{len(parts)}",
                    label=f"{job.title} — الجزء {i}/{len(parts)}",
                )
        finally:
            for part in parts:
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass

    async def _safe_edit(
        self, msg: Message | None, text: str, with_kb: str | None = None
    ) -> None:
        if msg is None:
            return
        try:
            await msg.edit_text(text, reply_markup=cancel_kb(with_kb) if with_kb else None)
        except TelegramBadRequest:
            pass
