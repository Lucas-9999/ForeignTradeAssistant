"""
Text Sanitizer — detects and translates Chinese text in outbound emails.

Two-layer strategy:
1. Primary: LLM translation (if LLM_API_KEY is configured)
2. Fallback: built-in dictionary mapping for common industry terms
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Matches any CJK Unified Ideograph (Chinese characters)
_CHINESE_RE = re.compile(r"[一-鿿]")

# ── Built-in industry term mappings (CN → EN) ───────────────────────────
# Covers all Chinese industry values currently found in the XunKeXing leads.
# `_lookup_industry` matches by exact key, then by substring, so short keys
# (e.g. 化工) also catch their suffixed variants (化工产品制品, 化工产品类).
_INDUSTRY_MAP: dict[str, str] = {
    "玻璃工艺品": "Glass Crafts & Glassware",
    "电子消费品": "Consumer Electronics",
    "电子电气": "Electronics & Electrical Equipment",
    "电子及家电": "Electronics & Home Appliances",
    "电子及家电类": "Electronics & Home Appliances",
    "家用电器": "Home Appliances",
    "礼品及赠品": "Gifts & Promotional Products",
    "礼品赠品": "Gifts & Promotional Products",
    "箱包制品及皮具": "Bags, Luggage & Leather Goods",
    "自行车": "Bicycles & Cycling Products",
    "节日用品": "Holiday & Seasonal Products",
    "车辆": "Automotive & Vehicles",
    "钟表": "Watches & Clocks",
    "陶瓷制品": "Ceramics & Porcelain Products",
    "鞋子": "Footwear",
    "鞋类": "Footwear",
    "食品": "Food & Beverage",
    "餐厨餐具用品": "Kitchenware & Tableware",
    "玻璃器皿餐厨用品": "Glassware & Kitchenware",
    "家具": "Furniture",
    "玩具": "Toys & Hobbies",
    "五金": "Hardware",
    "紧固件": "Fasteners",
    "机械": "Machinery",
    "建材": "Building Materials",
    "建筑材料": "Building Materials",
    "照明": "Lighting",
    "灯具灯饰": "Lighting & Lamps",
    "服装": "Textiles & Clothing",
    "纺织服装类": "Textiles & Clothing",
    "纺织原料面料": "Textile Raw Materials & Fabrics",
    "家用纺织品": "Home Textiles",
    "家纺": "Home Textiles",
    "汽车配件": "Auto Parts",
    "摩托车": "Motorcycles",
    "工具": "Tools",
    "医疗器械及医疗保健品": "Medical Devices & Health Products",
    "医疗器械及医疗保健": "Medical Devices & Health Products",
    "医疗器械及保健品": "Medical Devices & Health Products",
    "医药保健": "Medicine & Health Products",
    "家居日用品": "Daily Consumer Goods",
    "日用消费品类": "Daily Consumer Goods",
    "家居装饰品": "Home Decorations",
    "家居装饰品类": "Home Decorations",
    "家居装饰": "Home Decorations",
    "化工": "Chemicals",
    "化工产品制品": "Chemicals",
    "化工产品类": "Chemicals",
    "园林园艺": "Garden & Horticulture",
    "卫浴": "Bathroom & Sanitary Ware",
    "浴室用品": "Bathroom & Sanitary Ware",
    "办公文具用品": "Office & Stationery",
    "办公文具": "Office & Stationery",
    "办公文教": "Office & Educational Supplies",
    "办公箱包及体育休闲": "Office Bags & Sports Leisure",
    "办公箱包及体育休闲类": "Office Bags & Sports Leisure",
    "办公箱包及休闲用品类": "Office Bags & Sports Leisure",
    "珠宝首饰": "Jewelry",
    "装饰品": "Decorations & Ornaments",
    "地毯类": "Carpets & Rugs",
    "体育及旅游休闲": "Sports & Leisure Products",
    "体育及休闲用品": "Sports & Leisure Products",
    "运动休闲品": "Sports & Leisure Products",
    "个人护理用品": "Personal Care Products",
    "个人护理": "Personal Care Products",
    "通讯": "Telecommunications",
    "包装印刷": "Packaging & Printing",
    "铁石制品": "Iron & Stone Products",
    "安全防护": "Safety & Protection",
    "土畜": "Native & Livestock Products",
}

# Matches a bilingual industry value ending in a Chinese parenthetical, e.g.
# "Consumer Electronics (家用电器)" or "Mineral / Chemicals（矿产化工）". The
# English prefix IS the translation — no per-entry map needed. Requires the
# prefix to contain no Chinese so a pure-Chinese value never matches here.
_BILINGUAL_RE = re.compile(
    r"^(?P<en>.+?)[（(](?P<cn>[^（）()]*[一-鿿][^（）()]*)[）)]$"
)

# Distinct untranslatable strings that have already produced a warning — each
# unique string warns once per process instead of once per lead (10k+ log lines
# for ~60 categories in one collection run).
_WARNED_UNTRANSLATED: set[str] = set()

# ── Public API ──────────────────────────────────────────────────────────


def contains_chinese(text: str) -> bool:
    """Check if text contains any Chinese characters."""
    if not text:
        return False
    return bool(_CHINESE_RE.search(text))


def sanitize(text: Optional[str], fallback: str = "industrial supply") -> Optional[str]:
    """Return English-only text for use in outbound emails.

    If the text contains Chinese, attempts to translate it.
    Falls back to `fallback` if no translation is available.
    Returns None if input is None (preserves Jinja2 default filters).

    Args:
        text: The text to sanitize (may contain Chinese).
        fallback: Value returned if Chinese is detected but can't be translated.

    Returns:
        English-only text safe for email templates, or None.
    """
    if text is None:
        return None

    # Empty/whitespace strings → keep as-is (Jinja2 handles defaults)
    text = text.strip()
    if not text:
        return text

    if not contains_chinese(text):
        return text

    # Try industry mapping first
    result = _lookup_industry(text)
    if result:
        logger.debug(f"Sanitized (dict): '{text}' → '{result}'")
        return result

    # Try LLM if available
    result = _llm_translate(text)
    if result:
        logger.debug(f"Sanitized (LLM): '{text}' → '{result}'")
        return result

    # Cannot translate — return fallback instead of exposing Chinese.
    # Log each distinct untranslatable string only once: the same ~60 category
    # values repeat across thousands of fetched leads, so an unconditional
    # warning floods the log (08-20 run: 10,324 lines). New unknown values
    # still surface for diagnosis.
    if text not in _WARNED_UNTRANSLATED:
        _WARNED_UNTRANSLATED.add(text)
        logger.warning(
            f"Could not translate '{text}' — using fallback '{fallback}'"
        )
    return fallback


def sanitize_context(
    company_name: Optional[str] = None,
    contact_name: Optional[str] = None,
    country: Optional[str] = None,
    industry: Optional[str] = None,
    **extra,
) -> dict:
    """Sanitize all fields in a template context for outbound email.

    Returns a cleaned copy of the context dict with no Chinese text.
    """
    return {
        "company_name": sanitize(company_name, fallback="your company"),
        "contact_name": sanitize(contact_name, fallback="Team"),
        "country": sanitize(country, fallback=""),
        "industry": sanitize(industry, fallback="industrial supply"),
        **extra,
    }


# ── Internal helpers ────────────────────────────────────────────────────


@functools.lru_cache(maxsize=4096)
def _lookup_industry(text: str) -> Optional[str]:
    """Check the built-in industry dictionary for an exact or partial match.

    Tries exact match first, then substring match. Cached: batch send renders
    the same industry strings hundreds of times per run — the substring scan
    over _INDUSTRY_MAP was pure repeated work.
    """
    text_clean = text.strip()

    # Exact match
    if text_clean in _INDUSTRY_MAP:
        return _INDUSTRY_MAP[text_clean]

    # Bilingual "English (中文)" — the English prefix is the translation.
    # Checked before the substring scan so the English prefix wins over any
    # accidental partial-Chinese match.
    m = _BILINGUAL_RE.match(text_clean)
    if m and not contains_chinese(m.group("en")):
        return m.group("en").strip()

    # Substring match: check if any known term appears in the text
    for cn_term, en_term in _INDUSTRY_MAP.items():
        if cn_term in text_clean:
            return en_term
        # Also try partial: if the text contains parts of the known term
        if text_clean in cn_term:
            return en_term

    return None


@functools.lru_cache(maxsize=4096)
def _llm_translate(text: str) -> Optional[str]:
    """Translate Chinese text to English via LLM (if configured).

    Returns None if LLM is unavailable or translation fails. Cached: the same
    行业 value repeats across thousands of leads in one batch — uncached, each
    unique-but-repeated term fired an HTTP round-trip and a warning flood
    (08-17 log growth + accelerated log rotation).
    """
    import config

    if not config.LLM_API_KEY or not config.LLM_BASE_URL:
        return None

    try:
        from urllib.request import Request, urlopen
        import json

        prompt = (
            f"Translate this Chinese industry/trade term to English. "
            f"Return ONLY the English translation, no explanation:\n\n{text}"
        )

        payload = {
            "model": config.LLM_MODEL or "gpt-4o",
            "messages": [
                {"role": "system", "content": "You translate Chinese trade/industry terms to English. Reply with ONLY the English translation."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 50,
        }

        req = Request(
            f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config.LLM_API_KEY}",
                "Content-Type": "application/json",
            },
        )

        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result = data["choices"][0]["message"]["content"].strip()
            # Verify the result doesn't still contain Chinese
            if result and not contains_chinese(result):
                return result
            return None

    except Exception as e:
        logger.debug(f"LLM translation failed for '{text}': {e}")
        return None
