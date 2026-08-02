"""اختبارات يونيت نقية لأدوات textutil الجديدة (F1/F2) — بدون شبكة.

تغطي: parse_episode_range (أرقام عربية/إنجليزية، شرطات، أطراف، رينج مقلوب)،
closest_mb_quality (تطابق تام ثم الأقرب)، lan_ar (أكواد معروفة/غير معروفة)،
dub_label (الصيغتين: عربي مبسّط + اسم الموقع، الأصلية، مدبلج/مترجم).
"""
from types import SimpleNamespace

from bot.textutil import (
    closest_mb_quality,
    dub_label,
    lan_ar,
    parse_episode_range,
)


# ---------- parse_episode_range ----------

class TestParseEpisodeRange:
    def test_single_number(self):
        assert parse_episode_range("7", 24) == (7, 7)

    def test_simple_range(self):
        assert parse_episode_range("3-15", 24) == (3, 15)

    def test_arabic_digits(self):
        assert parse_episode_range("٣-١٥", 24) == (3, 15)

    def test_spaces_and_unicode_dashes(self):
        assert parse_episode_range(" 3 — 15 ", 24) == (3, 15)
        assert parse_episode_range("3–15", 24) == (3, 15)

    def test_reversed_range_normalized(self):
        assert parse_episode_range("15-3", 24) == (3, 15)

    def test_clamped_to_max(self):
        assert parse_episode_range("1-999", 24) == (1, 24)
        assert parse_episode_range("0-5", 24) == (1, 5)

    def test_out_of_bounds_returns_none(self):
        assert parse_episode_range("30-40", 24) is None

    def test_garbage_returns_none(self):
        assert parse_episode_range("abc", 24) is None
        assert parse_episode_range("", 24) is None
        assert parse_episode_range("3-15-20", 24) is None
        assert parse_episode_range("حلقة خمسة", 24) is None

    def test_zero_max_returns_none(self):
        assert parse_episode_range("1-5", 0) is None


# ---------- closest_mb_quality ----------

def _q(res):
    return SimpleNamespace(resolution=res)


class TestClosestMbQuality:
    def test_exact_match(self):
        quals = [_q(1080), _q(720), _q(480)]
        assert closest_mb_quality(quals, 720).resolution == 720

    def test_closest_when_missing(self):
        quals = [_q(1080), _q(480), _q(360)]
        # 720 مش موجودة — الأقرب 480 (فرق 240) ولا 1080 (فرق 360)
        assert closest_mb_quality(quals, 720).resolution == 480

    def test_tie_prefers_higher(self):
        quals = [_q(600), _q(800)]
        # فرق متساوٍ 200 — يفضّل الأعلى
        assert closest_mb_quality(quals, 700).resolution == 800

    def test_empty_returns_none(self):
        assert closest_mb_quality([], 720) is None

    def test_single_quality_always_returned(self):
        assert closest_mb_quality([_q(360)], 1080).resolution == 360


# ---------- lan_ar ----------

class TestLanAr:
    def test_known_codes(self):
        assert lan_ar("ar") == "عربي"
        assert lan_ar("en") == "إنجليزي"
        assert lan_ar("EN") == "إنجليزي"
        assert lan_ar("ko") == "كوري"

    def test_unknown_code_returned_as_is(self):
        assert lan_ar("xx") == "xx"

    def test_empty(self):
        assert lan_ar("") == "؟"


# ---------- dub_label ----------

def _dub(name, lan_code, type_=0, original=False):
    return SimpleNamespace(name=name, lan_code=lan_code, type=type_, original=original)


class TestDubLabel:
    def test_original(self):
        label = dub_label(_dub("Original Audio", "en", original=True))
        assert "النسخة الأصلية" in label
        assert "Original Audio" in label  # الصيغتين مع بعض

    def test_english_dub_both_formats(self):
        label = dub_label(_dub("English dub", "en", type_=0))
        assert "مدبلج إنجليزي" in label
        assert "English dub" in label

    def test_arabic_sub_type1(self):
        label = dub_label(_dub("Arabic sub", "ar", type_=1))
        assert "مترجم عربي" in label
        assert "Arabic sub" in label

    def test_no_name_falls_back_to_base(self):
        label = dub_label(_dub("", "fr", type_=0))
        assert label == "🎙 مدبلج فرنسي"

    def test_long_label_truncated(self):
        label = dub_label(_dub("X" * 100, "en"))
        assert len(label) <= 45
        assert label.endswith("…")
