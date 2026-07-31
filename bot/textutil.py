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
