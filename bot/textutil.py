"""أدوات نصوص آمنة لرسائل تليجرام بصيغة HTML.

مشكلتان كانوا بيسببوا TelegramBadRequest: can't parse entities:
1. محتوى ديناميكي (عناوين/أوصاف) بيتدرج خام — الحل: esc() على كل قيمة ديناميكية.
2. قص النص بـ [:N] ممكن يقطع في نص وسم <b> أو كيان &lt; فيفضل وسم مفتوح —
   الحل: truncate_html() اللي بتقص دون كسر وسوم/كيانات وتغلق أي وسم مفتوح.
"""
from __future__ import annotations

import html
import re

# حدود تليجرام مع هامش أمان ~20+ حرف
CAPTION_LIMIT = 1000  # كابشن الصور/الفيديو (الحد الرسمي 1024)
MESSAGE_LIMIT = 4076  # نص الرسالة العادية (الحد الرسمي 4096)

# وسوم HTML المسموحة في تليجرام (اللي ممكن تحتاج إغلاق بعد القص)
_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "a", "blockquote", "tg-spoiler", "span",
}

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^<>]*)?/?>")
# توكن: وسم كامل أو كيان HTML كامل — ممنوع القطع في نص أي منهما
_TOKEN_RE = re.compile(r"<[^<>]*>|&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")

# هامش محجوز لوسوم الإغلاق بعد القص (تعشيش الوسوم عندنا بسيط: <b> أساساً)
_CLOSE_RESERVE = 64


def esc(value) -> str:
    """تهريب أي محتوى ديناميكي قبل إدراجه في رسالة HTML."""
    return html.escape(str(value))


def truncate_html(text: str, limit: int = CAPTION_LIMIT, ellipsis: str = "…") -> str:
    """قص نص HTML بأمان: لا يقطع وسمًا أو كيانًا في المنتصف، ويغلق أي وسم مفتوح.

    الناتج دايماً ≤ limit وصالح للإرسال بـ ParseMode.HTML.
    """
    if len(text) <= limit:
        return text
    budget = limit - len(ellipsis) - _CLOSE_RESERVE
    out: list[str] = []
    stack: list[str] = []  # الوسوم المفتوحة حالياً
    used = 0
    pos = 0
    stopped = False
    for m in _TOKEN_RE.finditer(text):
        plain = text[pos:m.start()]
        if used + len(plain) > budget:
            out.append(plain[: budget - used])
            stopped = True
            break
        out.append(plain)
        used += len(plain)
        tok = m.group(0)
        if used + len(tok) > budget:
            stopped = True  # ممنوع قطع الوسم/الكيان في المنتصف
            break
        out.append(tok)
        used += len(tok)
        tm = _TAG_RE.fullmatch(tok)
        if tm and not tok.rstrip().endswith("/>"):
            closing, name = tm.group(1), tm.group(2).lower()
            if name in _ALLOWED_TAGS:
                if closing:
                    if stack and stack[-1] == name:
                        stack.pop()
                else:
                    stack.append(name)
        pos = m.end()
    if not stopped:
        rest = text[pos:]
        out.append(rest[: max(0, budget - used)])
    result = "".join(out) + ellipsis
    # إغلاق الوسوم المفتوحة بالترتيب العكسي (ضمن الهامش المحجوز)
    for name in reversed(stack):
        result += f"</{name}>"
    return result


# ---------- أدوات اختيار نقية (قابلة للاختبار — بدون اعتماديات تليجرام) ----------

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def parse_episode_range(text: str, max_ep: int) -> tuple[int, int] | None:
    """يحوّل إدخال المستخدم لرينج حلقات (من، إلى) ضمن [1, max_ep].

    يقبل: «5»، «3-15»، أرقام عربية «٣-١٥»، مسافات، شرطات يونيكود.
    يرجع None لو الإدخال غير مفهوم أو الرينج فاضي بعد القص.
    """
    if max_ep < 1:
        return None
    t = (text or "").strip().translate(_AR_DIGITS)
    for dash in ("—", "–", "−", ":"):
        t = t.replace(dash, "-")
    t = t.replace(" ", "")
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", t)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if a > b:
        a, b = b, a
    a = max(1, a)
    b = min(max_ep, b)
    if a > b:
        return None
    return a, b


def closest_mb_quality(qualities, res: int):
    """أقرب جودة موفي بوكس للدقة المطلوبة (تطابق تام أولاً).

    qualities: قائمة كائنات فيها .resolution (int). يرجع None لو القائمة فاضية.
    """
    if not qualities:
        return None
    exact = [q for q in qualities if q.resolution == res]
    if exact:
        return exact[0]
    return min(qualities, key=lambda q: (abs(q.resolution - res), -q.resolution))


_LAN_AR = {
    "ar": "عربي",
    "en": "إنجليزي",
    "fr": "فرنسي",
    "es": "أسباني",
    "tr": "تركي",
    "hi": "هندي",
    "de": "ألماني",
    "ru": "روسي",
    "it": "إيطالي",
    "ja": "ياباني",
    "ko": "كوري",
    "zh": "صيني",
    "pt": "برتغالي",
    "id": "إندونيسي",
    "fa": "فارسي",
    "ur": "أردو",
}


def lan_ar(code: str) -> str:
    """اسم اللغة بالعربي من كودها (ar/en/...) — لو مش معروف يرجع الكود نفسه."""
    return _LAN_AR.get((code or "").lower(), code or "؟")


def dub_label(dub) -> str:
    """ليبل النسخة بالصيغتين: عربي مبسّط + الاسم الأصلي من الموقع.

    أمثلة: «🎙 مدبلج إنجليزي — English dub» / «🎧 النسخة الأصلية — Original Audio».
    dub: كائن فيه name/lan_code/type/original (moviebox.MbDub).
    """
    name = (getattr(dub, "name", "") or "").strip()
    if getattr(dub, "original", False):
        base = "🎧 النسخة الأصلية"
    else:
        kind = "مترجم" if getattr(dub, "type", 0) == 1 else "مدبلج"
        base = f"🎙 {kind} {lan_ar(getattr(dub, 'lan_code', ''))}"
    if name and name.lower() not in base.lower():
        label = f"{base} — {name}"
    else:
        label = base
    return label if len(label) <= 45 else label[:42] + "…"
