"""Industry sanitization tests (data-collection quality fix, 2026-08-20).

The xunkexing collector returns Chinese ``category_name`` values that are
written straight into ``leads.industry``. When ``_INDUSTRY_MAP`` lacked an
entry, ``sanitize()`` silently stamped ``"fasteners"`` — wrong for
electronics/textiles/chemicals/etc. companies. These tests lock in the
expanded map + bilingual parsing and the one-warning-per-string dedupe.
"""

import logging

import pytest

from utils.sanitize import _lookup_industry, contains_chinese, sanitize


# Every distinct Chinese category observed in the 2026-08-20 run's
# "Could not translate" warnings (minus genuine unknowns like a person name).
UNTRANSLATED_BEFORE = [
    "电子及家电类", "建筑材料", "家用电器", "服装", "纺织服装类",
    "家居日用品", "汽车配件", "医疗器械及医疗保健品", "办公文具用品",
    "工具", "家居装饰品", "纺织原料面料", "家用纺织品", "化工",
    "礼品赠品", "日用消费品类", "浴室用品", "园林园艺",
    "体育及旅游休闲", "卫浴", "摩托车", "个人护理用品", "医药保健",
    "通讯", "电子及家电", "家居日用", "珠宝首饰", "土畜", "鞋类",
    "包装印刷", "运动休闲品", "装饰品", "地毯类", "化工产品制品",
    "化工产品类", "灯具灯饰", "办公文具", "玻璃器皿餐厨用品",
    "办公箱包及体育休闲", "铁石制品", "体育及休闲用品", "安全防护",
    "个人护理", "医疗器械及医疗保健", "医疗器械及保健品", "办公文教",
]

# Bilingual "English (中文)" values that must be resolved from the English
# prefix without individual map entries.
BILINGUAL = [
    ("Mineral / Chemicals (矿产化工)", "Mineral / Chemicals"),
    ("Consumer Electronics (家用电器)", "Consumer Electronics"),
    ("Garments / Leathers (纺织、皮革、羽绒)", "Garments / Leathers"),
    ("office supplies (办公文教)", "office supplies"),
    ("daily use (日用品)", "daily use"),
    ("Others (综合)", "Others"),
    ("ceramics (陶瓷)", "ceramics"),
    ("Medicine / Health / Beauty (医药保健)", "Medicine / Health / Beauty"),
    ("Native Products (土畜)", "Native Products"),
    ("suitcases / leather (箱包皮具)", "suitcases / leather"),
    ("Jewellery /Ornaments (珠宝首饰)", "Jewellery /Ornaments"),
    ("Crafts/Gifts (礼品、工艺品)", "Crafts/Gifts"),
    ("Garments / Apparels (服装、服饰)", "Garments / Apparels"),
    ("Electronics / Electrical (电子电工)", "Electronics / Electrical"),
    ("Telecommunications (通讯产品)", "Telecommunications"),
]


def test_previous_untranslated_categories_now_map():
    """None of the categories that used to fall back to 'fasteners' may do so."""
    for cat in UNTRANSLATED_BEFORE:
        result = sanitize(cat, fallback="fasteners")
        assert result != "fasteners", f"'{cat}' still falls back to fasteners"
        assert not contains_chinese(result), f"'{cat}' → '{result}' has Chinese"


def test_bilingual_values_extract_english_prefix():
    for raw, expected in BILINGUAL:
        assert sanitize(raw, fallback="fasteners") == expected


def test_full_width_parens_bilingual():
    assert sanitize("Consumer Electronics（家用电器）") == "Consumer Electronics"
    assert sanitize("Mineral / Chemicals（矿产化工）") == "Mineral / Chemicals"


def test_existing_map_entries_unchanged():
    assert sanitize("紧固件") == "Fasteners"
    assert sanitize("电子电气") == "Electronics & Electrical Equipment"
    assert sanitize("建材") == "Building Materials"


def test_pure_chinese_no_map_still_falls_back_but_warns_once(caplog):
    """A genuine unknown (e.g. a person name in the category field) still
    falls back, but only one warning per distinct string per process."""
    _lookup_industry.cache_clear()
    # 林宝杰 is a name, not an industry — it must NOT resolve to a fake English
    # term; falling back to the caller's fallback is the safe behavior.
    with caplog.at_level(logging.WARNING, logger="utils.sanitize"):
        r1 = sanitize("林宝杰", fallback="fasteners")
        r2 = sanitize("林宝杰", fallback="fasteners")
    assert r1 == "fasteners" and r2 == "fasteners"
    warns = [m for m in caplog.messages if "Could not translate" in m]
    assert len(warns) == 1


def test_lookup_industry_returns_none_for_unknown():
    assert _lookup_industry("林宝杰") is None
    assert _lookup_industry("五金") == "Hardware"
