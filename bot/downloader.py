"""مدير التحميلات — طابور لكل يوزر + تقدم لايف + إلغاء (حسب SPEC 3.6)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message, URLInputFile

from .db import Database
from .keyboards import cancel_kb

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CHUNK = 256 * 1024
_EDIT_EVERY = 5.0  # ثواني بين تحديثات رسالة التقدم
_MIN_PARALLEL_SIZE = 10 * 1024**2  # التحميل المتوازي للملفات الأكبر من 10MB فقط
_SEGMENT_RETRIES = 3  # محاولات لكل قطعة قبل الرجوع للتدفق الواحد


@dataclass
class DownloadJob:
    task_id: str  # uuid4 hex[:12]
    title: str  # اسم يظهر لليوزر (فيلم/حلقة + جودة) — بصيغة "الاسم (الجودة)"
    url: str  # رابط downet المباشر
    caption: str  # كابشن الفيديو
    thumb_url: str | None = None


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
            await self.bot.send_message(
                chat_id,
                f"📥 «{job.title}» في طابور الانتظار (رقم {waiting + 1}) — "
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
            status_msg = await self.bot.send_message(
                chat_id,
                f"⏳ بدأ تحميل «{job.title}»…",
                reply_markup=cancel_kb(job.task_id),
            )
            segments, premium = await self._segments_for(user_id)
            await self._download_file(job, path, status_msg, segments=segments, premium=premium)
            await self._upload_file(job, path, chat_id, status_msg)
            await self.db.log_download(user_id, job.title, quality, "done")
            await self._safe_edit(status_msg, f"✅ خلص واتبعت: «{job.title}»")
        except asyncio.CancelledError:
            await self.db.log_download(user_id, job.title, quality, "cancelled")
            await self._safe_edit(status_msg, f"❌ اتلغى تحميل «{job.title}»")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("download failed: %s", job.task_id)
            await self.db.log_download(user_id, job.title, quality, "failed")
            await self._safe_edit(
                status_msg,
                f"❌ فشل تحميل «{job.title}»\nالسبب: {type(e).__name__} — جرب تاني.",
            )
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
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
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=300.0),
            headers={"User-Agent": _UA},
        ) as client:
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
                            f"⬇️ بيتم تحميل «{job.title}»\n"
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
                f"⬇️ بيتم تحميل «{job.title}»\n"
                f"📊 {percent} — 🚀 {speed:.1f} MB/s\n"
                f"💾 {_fmt_size(downloaded)} / {_fmt_size(total)}\n"
                f"{mode}",
                with_kb=job.task_id,
            )
            last_edit = now
            last_bytes = downloaded

    async def _upload_file(
        self, job: DownloadJob, path: str, chat_id: int, status_msg: Message
    ) -> None:
        await self._safe_edit(
            status_msg,
            f"📤 بيترفع على تليجرام: «{job.title}»\n(ممكن ياخد وقت حسب حجم الملف)",
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
                        f"📤 بيترفع على تليجرام: «{job.title}»\n⏱ مر {elapsed} ثانية…",
                        with_kb=job.task_id,
                    )

        ticker = asyncio.create_task(_tick())
        try:
            thumbnail = URLInputFile(job.thumb_url) if job.thumb_url else None
            await self.bot.send_video(
                chat_id,
                FSInputFile(path),
                caption=job.caption,
                supports_streaming=True,
                thumbnail=thumbnail,
            )
        finally:
            stop.set()
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    async def _safe_edit(
        self, msg: Message | None, text: str, with_kb: str | None = None
    ) -> None:
        if msg is None:
            return
        try:
            await msg.edit_text(text, reply_markup=cancel_kb(with_kb) if with_kb else None)
        except TelegramBadRequest:
            pass
