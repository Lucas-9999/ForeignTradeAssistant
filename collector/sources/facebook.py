"""
Facebook Collector — social media lead sourcing for fastener industry.

Strategy:
  - Use Playwright with persistent profile (login once, session persists)
  - Multi-phase collection: groups → pages → posts → marketplace
  - Scrape public fastener-related content only
  - Extract company names, emails, phones, websites, WhatsApp
  - Target: mbasic.facebook.com (lighter, easier to parse) with fallback to full site

Risk Control (风控):
  - Human-like behavior simulation: random scrolls, variable "reading" delays
  - Session health pre-check before full collection
  - Adaptive rate limiting with conservative defaults (FB is sensitive)
  - Exponential backoff on transient errors
  - Auth error detection: immediate stop on login/security-check pages
  - Circuit breaker on consecutive failures
  - Response integrity checks (detect block/login pages)
  - Gradual speed variation (don't maintain constant pace)
  - Random idle periods between phases

Data Sources (only public content, no login-walled data):
  1. Public fastener trading/sourcing groups (posts + about sections)
  2. Company/business pages in fastener industry
  3. Public post search across the platform
  4. Marketplace listings (if publicly accessible)

Compliance Notes (合规须知):
  - Only accesses PUBLICLY AVAILABLE content — no private profiles, groups, or messages
  - Uses conservative rate limits to avoid server impact
  - Respects robots.txt intent (though FB blocks automated access)
  - Does NOT bypass paywalls, login gates, or security measures
  - Does NOT collect personal data beyond business contact info
  - Collected data: company names, business emails, business phones, websites only
  - Purpose: B2B trade lead generation for fastener industry
  - Recommendation: Use a dedicated business account, not a personal one
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import config
from collector.base import BaseCollector
from collector.rate_limiter import (
    AdaptiveRateLimiter,
    CircuitBreakerOpen,
    RateLimiterConfig,
)
from collector.retry import (
    AuthErrorDetected,
    MaxRetriesExceeded,
    RiskControlError,
    RetryConfig,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

FB_BASE = "https://mbasic.facebook.com"
FB_WWW = "https://www.facebook.com"
FB_PROFILE_DIR = Path(config.DATA_DIR) / "fb_profile"
FB_TARGETS_PATH = Path(config.DATA_DIR) / "fb_target_groups.json"

# Mobile user agent — mbasic is the mobile site and will redirect
# desktop UAs to www.facebook.com (JS SPA, unparseable).
FB_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

# ── Search queries — multi-industry coverage of fastener-consuming sectors ─

# Post search queries: cross-industry procurement + supplier searches
# Strategy: find companies/people in industries that USE fasteners,
# especially those with buying intent (procurement, sourcing, wanted)
FB_SEARCH_QUERIES = [
    # === Fastener industry core ===
    "fasteners import export",
    "fasteners supplier manufacturer",
    "bolts screws manufacturer",
    "industrial fasteners wholesale",
    "fastener distributor wanted",
    "screws bolts buyers sourcing",
    # === Automotive (largest fastener consumer) ===
    "automotive parts buyer sourcing",
    "auto parts importer wanted",
    "car manufacturer procurement",
    "electric vehicle parts supplier needed",
    "汽车零部件采购求购",
    "汽车配件供应商",
    # === Construction & Building Materials ===
    "construction materials buyer sourcing",
    "building materials importer wanted",
    "steel structure supplier needed",
    "建材五金采购求购",
    "建筑材料进口商",
    # === Machinery & Manufacturing ===
    "industrial machinery parts buyer",
    "manufacturing equipment procurement",
    "CNC machining parts wanted",
    "机械制造采购求购",
    "机械设备配件求购",
    # === Electronics & Appliances ===
    "electronics components importer",
    "home appliance parts buyer",
    "consumer electronics manufacturer needed",
    "家电配件采购商",
    # === Furniture & Home ===
    "furniture hardware buyer sourcing",
    "cabinet hardware importer wanted",
    "家具五金配件求购",
    # === Aerospace & Defense ===
    "aerospace parts supplier",
    "aircraft components procurement",
    "aviation fasteners distributor",
    # === Renewable Energy ===
    "solar panel mounting hardware",
    "wind turbine fasteners supplier",
    "renewable energy parts buyer",
    "新能源配件采购",
    "光伏支架配件求购",
    # === Medical Devices ===
    "medical device components buyer",
    "medical equipment parts supplier",
    "医疗器械配件采购",
    # === Fitness & Sports Equipment ===
    "fitness equipment parts sourcing",
    "gym equipment hardware buyer",
    "健身器材配件求购",
    # === Agriculture & Farm Equipment ===
    "agricultural machinery parts importer",
    "farm equipment hardware sourcing",
    "农机配件采购求购",
    # === Marine & Shipbuilding ===
    "marine equipment parts buyer",
    "shipbuilding hardware supplier",
    "船舶配件采购",
    # === Railway & Transportation ===
    "railway parts procurement",
    "rail fasteners supplier wanted",
    "铁路配件紧固件",
    # === Robotics & Automation ===
    "automation equipment parts sourcer",
    "industrial robot components buyer",
    "自动化设备配件采购",
    # === General Procurement / Cross-Industry ===
    "industrial parts procurement manager",
    "OEM parts supplier wanted",
    "mechanical components buyer sourcing",
    "metal parts importer distributor wanted",
    "五金配件采购供应商",
    "零部件采购求购信息",
    # === Multi-language procurement ===
    "tornillos fabricante proveedor comprador",  # Spanish
    "sujetadores industriales importacion",       # Spanish
    "viti bulloni acquirente fornitore",           # Italian
    "vis écrou acheteur fournisseur",             # French
    "schrauben muttern einkauf lieferant",        # German
    "bağlantı elemanları ithalat ihracat",        # Turkish
    "مثبتات براغي مورد مشتري",                     # Arabic
    "parafusos porcas comprador fornecedor",       # Portuguese
]

# ── Group search queries — industry + trade + procurement groups ───────

FB_GROUP_QUERIES = [
    # Fastener-specific groups
    "fasteners import export group",
    "fastener suppliers manufacturers network",
    "bolts screws hardware trading",
    "industrial fasteners buyers sellers",
    "五金紧固件交流群",
    "fastener industry professionals",
    "international trade fasteners",
    "sourcing fasteners hardware",
    # Cross-industry procurement groups
    "industrial parts buyers sellers group",
    "automotive parts import export group",
    "construction materials trading group",
    "machinery parts suppliers buyers",
    "international procurement sourcing group",
    "B2B trade manufacturing group",
    "五金配件采购交流群",
    "外贸进出口采购群",
    # Regional trade groups (emerging markets)
    "industrial importers Africa group",
    "Middle East construction suppliers",
    "Southeast Asia manufacturing group",
    "Latin America importers group",
    "South Asia industrial buyers",
]

# ── Page search queries — company/business pages across industries ────

FB_PAGE_QUERIES = [
    # Fastener companies
    "fasteners manufacturer company",
    "bolts screws factory",
    "fastener supplier exporter",
    "industrial hardware fasteners",
    "紧固件公司",
    "fastener trading company",
    "screws bolts production",
    # Automotive suppliers
    "automotive parts manufacturer",
    "auto components supplier",
    "汽车零部件制造公司",
    "vehicle parts exporter",
    # Construction & building
    "construction hardware supplier",
    "building materials manufacturer",
    "建材五金公司",
    "steel fabrication company",
    # Industrial / Machinery
    "industrial equipment manufacturer",
    "machinery parts supplier",
    "mechanical components factory",
    "metal fabrication company",
    "机械制造公司",
    # Electronics / Appliances
    "electronics manufacturer company",
    "appliance parts supplier",
    "电器配件制造",
    # Furniture manufacturing
    "furniture manufacturer factory",
    "office furniture company",
    "家具制造公司",
    # Energy / Solar
    "solar energy equipment manufacturer",
    "renewable energy parts supplier",
    # General manufacturing / OEM
    "OEM parts manufacturer supplier",
    "industrial manufacturing company",
    "五金制造公司",
    "metal products factory",
    "precision parts manufacturer",
]

# ── Email regex ────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE,
)

# ── Website regex (avoid FB internal URLs) ─────────────────────────────

WEBSITE_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?!facebook\.com|fb\.com|fb\.me|messenger\.com)'
    r'[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}(?:/[^\s,.\"\')\]}>]*)?',
)

# ── Phone regex (international format, loose) ───────────────────────────

PHONE_RE = re.compile(
    r'(?:\+?\d{1,4}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?){2,4}\d{2,4}',
)

# ── WhatsApp-specific patterns ─────────────────────────────────────────

# wa.me short links
WHATSAPP_LINK_RE = re.compile(
    r'(?:https?://)?(?:wa\.me|chat\.whatsapp\.com|api\.whatsapp\.com)/[^\s\'\"]+',
    re.IGNORECASE,
)

# "WhatsApp: +xxx" or "WA +xxx" patterns in text
WHATSAPP_TEXT_RE = re.compile(
    r'(?:whatsapp|wa|WhatsApp|WA)\s*[:：]\s*(\+?\d[\d\s\-().]{7,})',
    re.IGNORECASE,
)

# ── Industry keywords — organized by fastener-CONSUMING sector ─────────
# These are the industries that USE fasteners in their products.
# We search for companies/people in these industries who might need fasteners.

INDUSTRY_KEYWORDS = {
    # 1. Fasteners core (our product)
    "fasteners": [
        "fastener", "screw", "bolt", "nut", "washer", "rivet", "anchor",
        "threaded rod", "stud", "pin", "clip", "clamp", "spring", "staple",
        "五金", "紧固件", "螺丝", "螺母", "螺栓", "垫圈", "铆钉",
        "螺柱", "车削件", "冲压件", "冷镦", "五金配件", "标准件",
        "schraube", "mutter", "bolzen", "befestigung", "verbindungselement",
        "scheibe", "niet", "gewindestange",
        "tornillo", "tuerca", "perno", "arandela", "remache", "sujetador",
        "vis", "écrou", "boulon", "rondelle", "rivet", "fixation",
        "vite", "dado", "bullone", "rondella", "rivetto", "fissaggio",
        "vida", "somun", "civata", "pul", "perçin", "bağlantı elemanı",
        "parafuso", "porca", "arruela", "rebite", "fixador",
    ],
    # 2. Automotive & Transportation (largest fastener consumer)
    "automotive": [
        "automotive", "auto parts", "car manufacturer", "vehicle",
        "truck", "tractor", "motorcycle", "electric vehicle", "ev",
        "automobile", "auto components", "spare parts",
        "汽车", "汽配", "汽车零部件", "车用", "电动车", "新能源车",
        "automobil", "fahrzeug", "autoteile",
        "automóvil", "autopartes", "vehículo",
        "automobile", "véhicule", "pièces auto",
        "otomotiv", "araç", "yedek parça",
    ],
    # 3. Construction & Building Materials
    "construction": [
        "construction", "building materials", "structural",
        "steel structure", "curtain wall", "prefab", "precast",
        "scaffolding", "formwork", "civil engineering",
        "建筑", "建材", "钢结构", "幕墙", "脚手架", "预制",
        "bau", "baumaterial", "baustoff",
        "construcción", "materiales de construcción",
        "construction", "bâtiment", "matériaux de construction",
    ],
    # 4. Machinery & Manufacturing
    "machinery": [
        "machinery", "manufacturing", "industrial equipment",
        "heavy machinery", "cnc", "metal fabrication", "sheet metal",
        "machining", "forging", "stamping", "casting", "injection molding",
        "机械", "制造业", "机加工", "设备制造", "冲压", "铸造", "注塑",
        "maschinenbau", "fertigung", "werkzeugmaschine",
        "maquinaria", "fabricación", "equipo industrial",
        "macchinari", "produzione", "attrezzatura industriale",
    ],
    # 5. Electronics & Home Appliances
    "electronics": [
        "electronics", "electrical", "home appliance", "consumer electronics",
        "white goods", "household appliance", "power tool",
        "电子", "电器", "家电", "消费电子", "电动工具",
        "elektronik", "haushaltsgerät", "elektrowerkzeug",
        "electrónica", "electrodoméstico",
        "elettronica", "elettrodomestico",
    ],
    # 6. Furniture & Home
    "furniture": [
        "furniture", "home furnishings", "cabinet", "wardrobe",
        "office furniture", "outdoor furniture", "bed frame",
        "家具", "家居", "办公家具", "橱柜", "衣柜",
        "möbel", "einrichtung", "schrank",
        "muebles", "mobiliario", "armario",
        "mobili", "arredamento", "mobile",
    ],
    # 7. Aerospace & Aviation
    "aerospace": [
        "aerospace", "aviation", "aircraft", "drone",
        "satellite", "helicopter", "jet engine",
        "航空", "航天", "飞机", "无人机",
        "luftfahrt", "flugzeug", "raumfahrt",
        "aeroespacial", "aviación", "aeronave",
        "aérospatiale", "aviation", "aéronef",
    ],
    # 8. Renewable Energy & Solar
    "energy": [
        "solar", "wind energy", "renewable energy", "solar panel",
        "wind turbine", "energy storage", "power generation",
        "photovoltaic", "pv mounting", "solar tracker",
        "新能源", "太阳能", "风能", "光伏", "储能",
        "solarenergie", "windenergie", "photovoltaik",
        "energía solar", "energía eólica", "renovable",
        "énergie solaire", "éolien", "renouvelable",
    ],
    # 9. Medical Devices & Healthcare
    "medical": [
        "medical device", "medical equipment", "surgical",
        "hospital equipment", "healthcare", "dental equipment",
        "医疗", "医疗器械", "医院设备", "牙科",
        "medizintechnik", "medizingerät", "krankenhaus",
        "dispositivo médico", "equipo médico", "hospitalario",
        "dispositif médical", "équipement médical", "hôpital",
    ],
    # 10. Fitness & Sports Equipment
    "fitness": [
        "fitness equipment", "gym equipment", "sports equipment",
        "exercise", "treadmill", "weight training", "bicycle",
        "健身", "健身器材", "体育器材", "运动设备",
        "fitnessgerät", "sportgerät", "trainingsgerät",
        "equipo de fitness", "equipo deportivo", "gimnasio",
        "équipement de fitness", "équipement sportif",
    ],
    # 11. Agriculture & Farm Equipment
    "agriculture": [
        "agriculture", "agricultural machinery", "farm equipment",
        "tractor", "harvester", "irrigation", "cultivator",
        "农业", "农机", "农用", "拖拉机", "收割机",
        "landwirtschaft", "landmaschine", "traktor",
        "agricultura", "maquinaria agrícola", "tractor",
        "agriculture", "équipement agricole", "tracteur",
    ],
    # 12. Marine & Shipbuilding
    "marine": [
        "shipbuilding", "marine", "offshore", "boat", "yacht",
        "naval", "shipyard", "maritime",
        "船舶", "海洋", "船用", "造船",
        "schiffbau", "marine", "schiff",
        "construcción naval", "marino", "barco",
        "construction navale", "maritime", "navire",
    ],
    # 13. Railway & Metro
    "railway": [
        "railway", "rail", "train", "metro", "high speed rail",
        "locomotive", "rolling stock", "track",
        "铁路", "高铁", "轨道交通", "地铁",
        "eisenbahn", "bahn", "zug", "schiene",
        "ferrocarril", "tren", "metro", "vía",
        "chemin de fer", "train", "métro", "rail",
    ],
    # 14. Robotics & Automation
    "automation": [
        "robotics", "automation", "industrial robot", "automated",
        "assembly line", "conveyor", "pneumatic", "hydraulic",
        "机器人", "自动化", "流水线", "气动", "液压",
        "robotik", "automatisierung", "fließband",
        "robótica", "automatización", "robot industrial",
        "robotique", "automatisation", "chaîne de montage",
    ],
    # 15. HVAC & Plumbing
    "hvac": [
        "HVAC", "air conditioning", "heating system", "ventilation",
        "plumbing", "pipe fitting", "valve", "pump",
        "暖通", "空调", "管道", "阀门", "水泵",
        "klimatechnik", "heizung", "rohrleitung", "ventil",
        "climatización", "calefacción", "tubería", "válvula",
        "CVC", "climatisation", "chauffage", "tuyauterie",
    ],
    # 16. Packaging & Logistics
    "packaging": [
        "packaging machinery", "conveyor system", "warehouse equipment",
        "logistics automation", "material handling",
        "包装", "物流", "输送", "仓储",
        "verpackung", "fördertechnik", "logistik",
        "embalaje", "logística", "transportador",
    ],
    # 17. Military & Defense
    "defense": [
        "defense", "military", "army", "navy", "air force",
        "defense contractor", "weapon", "armored vehicle",
        "军事", "国防", "军工",
        "verteidigung", "militär", "rüstung",
        "defensa", "militar", "ejército",
        "défense", "militaire", "armée",
    ],
    # 18. General Manufacturing / Industrial
    "industrial": [
        "industrial supply", "hardware", "fitting", "mro",
        "c-class parts", "mechanical parts", "metal parts",
        "OEM", "original equipment", "contract manufacturing",
        "工业", "零配件", "配件", "加工件",
        "industriebedarf", "ersatzteil", "zulieferer",
        "suministro industrial", "repuesto", "proveedor industrial",
    ],
}

# Flattened keyword list for relevance scoring (all values merged)
_INDUSTRY_KEYWORDS_FLAT: list[str] = []
for _kw_list in INDUSTRY_KEYWORDS.values():
    _INDUSTRY_KEYWORDS_FLAT.extend(_kw_list)
# De-duplicate while preserving order
_seen_kw: set[str] = set()
INDUSTRY_KEYWORDS_FLAT: list[str] = []
for _kw in _INDUSTRY_KEYWORDS_FLAT:
    if _kw.lower() not in _seen_kw:
        _seen_kw.add(_kw.lower())
        INDUSTRY_KEYWORDS_FLAT.append(_kw)

# ── Procurement/buying intent keywords (multi-language) ────────────────
# Posts containing these signals are higher priority — they indicate active demand

PROCUREMENT_KEYWORDS = [
    # English — strong buying signals
    "looking for", "need supplier", "wanted", "wanted supplier",
    "sourcing", "source supplier", "find supplier",
    "procurement", "purchasing", "buyer", "purchase manager",
    "RFQ", "request for quote", "tender", "bid", "bidding",
    "need manufacturer", "OEM supplier needed", "supplier required",
    "import needed", "distributor wanted", "agent wanted",
    "wholesale buyer", "bulk order", "bulk purchase",
    "we need", "we require", "we are looking",
    "please contact", "send quotation", "quote required",
    "urgent requirement", "monthly requirement",
    # Chinese — strong buying signals
    "求购", "采购", "寻找供应商", "需要供应商",
    "求购信息", "采购需求", "采购经理",
    "招标", "询价", "报价", "寻求合作",
    "大量采购", "长期合作", "寻求厂家",
    "急需", "需求", "寻找货源",
    "采购商", "进口商", "外贸公司",
    # Spanish / LatAm
    "busco proveedor", "necesito proveedor", "comprador",
    "importador", "busco fabricante", "necesito fabricante",
    "cotización", "licitación", "compra al por mayor",
    # Arabic / Middle East
    "مطلوب", "شراء", "مستورد", "مورد مطلوب",
    "مناقصة", "طلب عرض سعر",
    # French
    "cherche fournisseur", "recherche fournisseur", "acheteur",
    "importateur", "appel d'offre", "demande de devis",
    # German
    "suche lieferant", "einkauf", "einkäufer", "beschaffung",
    "ausschreibung", "anfrage", "lieferant gesucht",
    # Italian
    "cerco fornitore", "acquirente", "importatore",
    "gara d'appalto", "richiesta preventivo",
    # Turkish
    "tedarikçi aranıyor", "satın alma", "ithalatçı",
    "ihale", "teklif talebi", "tedarik",
    # Portuguese
    "procuro fornecedor", "comprador", "importador",
    "licitação", "solicitação de cotação",
    # Russian
    "ищу поставщика", "закупка", "тендер",
    "импортер", "запрос предложений",
    # Vietnamese
    "tìm nhà cung cấp", "cần mua", "nhập khẩu",
    "đấu thầu", "yêu cầu báo giá",
]

# ── Facebook auth/block indicators ─────────────────────────────────────

FB_AUTH_INDICATORS = [
    "log in", "sign up", "create an account",
    "you must log in", "please log in",
    "confirm your identity", "security check",
    "unusual activity", "verify your account",
    "checkpoint", "login approval needed",
    "we've detected suspicious", "account temporarily locked",
    "verify it's you", "recover your account",
    "upload a photo of yourself", "confirm your phone",
]

# ── Facebook security checkpoint URL patterns ──────────────────────────

FB_CHECKPOINT_PATTERNS = [
    "/checkpoint/", "/login/", "/recover/",
    "facebook.com/checkpoint", "facebook.com/login",
]

# ═══════════════════════════════════════════════════════════════════════════
# Helper: Human Behavior Simulation
# ═══════════════════════════════════════════════════════════════════════════


async def human_delay(
    base_seconds: float = 2.0,
    variation: float = 0.5,
    label: str = "",
) -> None:
    """Simulate human reading/thinking time with random variation.

    Real users don't load pages at constant intervals. They read, scroll,
    get distracted, type notes, etc. This adds realistic gaps between actions.

    Args:
        base_seconds: Base wait time in seconds.
        variation: Fractional variation (0.5 = ±50%).
        label: Optional debug label.
    """
    jitter = base_seconds * variation
    delay = base_seconds + random.uniform(-jitter, jitter * 2)
    # Occasionally add a longer "distraction" pause (1 in 12 chance)
    if random.random() < 0.08:
        delay += random.uniform(1.0, 4.0)
    delay = max(0.2, delay)
    if label:
        logger.debug(f"  Human delay ({label}): {delay:.1f}s")
    await asyncio.sleep(delay)


def _pid_alive(pid: int) -> bool:
    """Side-effect-free check whether a PID is alive (cross-platform, stdlib).

    - Windows: OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION only — never
      terminates. os.kill() on Windows would TerminateProcess the target, so it
      is deliberately NOT used here.
    - POSIX: os.kill(pid, 0) is a pure existence probe (no signal is sent).
    """
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:
            # Cannot probe (e.g. ctypes unavailable) — assume not alive so a
            # stale lock does not block collection.
            return False
        if not handle:
            return False
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive but owned by another user
    return True


async def human_scroll(page, times: int = 2):
    """Simulate human-like scrolling behavior on a page.

    Real users scroll gradually, pause at interesting content, and
    sometimes scroll back up. This does NOT interact with page elements.
    """
    try:
        for i in range(times):
            # Random scroll distance (200-800px) — humans don't scroll full pages
            scroll_y = random.randint(200, 800)
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
            # Pause between scrolls (reading time — faster but still human)
            await asyncio.sleep(random.uniform(0.3, 1.2))
            # Occasionally scroll back up a bit (1 in 6)
            if random.random() < 0.16:
                back = random.randint(50, 150)
                await page.evaluate(f"window.scrollBy(0, -{back})")
                await asyncio.sleep(random.uniform(0.2, 0.6))
        logger.debug(f"  Human scroll: {times} scrolls completed")
    except Exception:
        # Scroll is cosmetic — don't fail the collection if it errors
        pass


def _clean_fb_url(url: str) -> str:
    """Clean Facebook URL by removing tracking params but keeping id.

    - Strips __cft__, __tn__, and other FB tracking parameters
    - Preserves ?id=xxx for profile.php URLs
    - Removes fragments
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(url)
    # Keep only essential query params
    essential_params = {'id', 'sk', 'view', 'q'}
    if parsed.query:
        qs = parse_qs(parsed.query)
        clean_qs = {k: v for k, v in qs.items() if k in essential_params}
        clean_query = urlencode(clean_qs, doseq=True)
    else:
        clean_query = ''

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        clean_query,
        '',  # remove fragment
    ))


# ── FB UI strings that are NOT company names ────────────────────────────
_FB_UI_STRINGS: set[str] = {
    "facebook", "home", "friends", "watch", "marketplace", "groups",
    "gaming", "more", "settings", "privacy", "terms", "help",
    "about", "posts", "photos", "videos", "followers", "following",
    "message", "follow", "share", "join", "joined", "create post",
    "whatsapp", "send email", "search", "menu", "notifications",
    "messenger", "create", "logout", "log out", "sign up", "log in",
    "details", "contact info", "number of unread notifications",
    "see all", "all", "featured", "discussion", "people", "events",
    "media", "files", "reels", "live", "shop", "links", "notes",
    "write something", "anonymous post",
}


def _extract_name_from_www_text(text: str) -> str | None:
    """Extract company/profile name from www.facebook.com visible text.

    On www, the <title> tag is always "(N+) Facebook". The actual name
    appears in the body text as the first meaningful line after the
    notification count header.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        # Skip notification count (e.g. "20+", "1")
        if re.match(r"^\d+\+?$", line):
            continue
        # Skip FB UI strings
        if line.lower() in _FB_UI_STRINGS:
            continue
        # Skip lines that are just numbers or too short
        if len(line) < 2:
            continue
        # Skip lines with only special characters
        if re.match(r"^[·•|/\-–—\s]+$", line):
            continue
        # Skip follower counts
        if re.match(r"^\d[\d,.]*\s*(followers|following|likes|members)", line, re.IGNORECASE):
            continue
        # This looks like a name
        return line
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Facebook Collector
# ═══════════════════════════════════════════════════════════════════════════


class FacebookCollector(BaseCollector):
    """Collect fastener industry leads from Facebook public content.

    Multi-phase collection:
      1. Session health check — verify not blocked/logged out
      2. Group search — find fastener-related public groups, extract posts
      3. Page search — find company pages, extract info
      4. Post search — general keyword search across platform
      5. De-duplicate & relevance-score all leads

    Usage:
        # First run: headless=False to log in manually
        collector = FacebookCollector(headless=False, max_posts=50)
        leads = await collector.collect()

        # Subsequent runs: headless=True (session persists)
        collector = FacebookCollector(headless=True, max_posts=50)
        leads = await collector.collect()
    """

    source_name = "facebook"

    def __init__(
        self,
        headless: bool = False,  # default visible — FB blocks headless
        max_posts: int = 80,
        max_per_query: int = 20,
        max_per_group: int = 15,
        max_per_page: int = 10,
        profile_dir: Optional[Path] = None,
        queries: Optional[list[str]] = None,
        group_queries: Optional[list[str]] = None,
        page_queries: Optional[list[str]] = None,
        targeted: Optional[bool] = None,
    ):
        super().__init__(
            delay=config.SCRAPE_REQUEST_DELAY * 2,  # FB needs slower pacing
            timeout=45.0,
        )
        self.headless = headless
        self.max_posts = max_posts
        self.max_per_query = max_per_query
        self.max_per_group = max_per_group
        self.max_per_page = max_per_page
        self.profile_dir = profile_dir or FB_PROFILE_DIR

        # Targeted mode: visit curated trade groups directly instead of broad
        # search. Broad search surfaces few public emails (FB doesn't expose
        # them), but group posts do yield contacts — the 62 leads that have
        # ever been found all came from groups / proven queries.
        if targeted is None:
            targeted = getattr(config, "FB_COLLECTION_MODE", "targeted") == "targeted"
        self.targeted = targeted
        self.target_groups: list[str] = []
        self.targeted_queries: list[str] = []
        self._load_targets()

        self.queries = queries or FB_SEARCH_QUERIES
        self.group_queries = group_queries or FB_GROUP_QUERIES
        self.page_queries = page_queries or FB_PAGE_QUERIES

        # ── Rate limiter (conservative but practical for Facebook) ─────
        rl_config = RateLimiterConfig(
            requests_per_minute=10,       # ~6 seconds between actions (human pace)
            min_delay=2.0,               # minimum 2s between page loads
            max_delay=90.0,              # back off to 90s on errors
            jitter_factor=0.4,           # ±40% jitter
            backoff_multiplier=2.0,
            max_consecutive_errors=4,    # open circuit sooner
            circuit_cooldown_seconds=900, # 15 min cooldown
        )
        self._rate_limiter = AdaptiveRateLimiter(rl_config, source_name="facebook")

        # ── Retry config ────────────────────────────────────────────────
        self._retry_config = RetryConfig(
            max_retries=2,
            base_delay=5.0,
            max_delay=120.0,
            backoff_factor=2.0,
        )

        # ── Playwright state ────────────────────────────────────────────
        self._playwright = None
        self._browser = None
        self._context = None
        self._setup_done = False
        self._session_healthy = False
        self._use_www = False  # ONLY True in manual (headless=False) mode; never for automated collection

        # ── Results tracking ────────────────────────────────────────────
        self._visited_urls: set[str] = set()
        self._seen_companies: set[str] = set()
        self._seen_emails: set[str] = set()

        # ── Stats for logging ───────────────────────────────────────────
        self._stats = {
            "groups_checked": 0,
            "pages_checked": 0,
            "posts_checked": 0,
            "leads_from_groups": 0,
            "leads_from_pages": 0,
            "leads_from_posts": 0,
            "skipped_irrelevant": 0,
            "auth_errors": 0,
        }

    def _load_targets(self) -> None:
        """Load curated target groups + targeted queries from fb_target_groups.json.

        Editable at runtime without code changes: add your own trade groups
        (Fastener/Trade/Procurement communities) to the `target_groups` array.
        """
        if not FB_TARGETS_PATH.exists():
            return
        try:
            data = json.loads(FB_TARGETS_PATH.read_text(encoding="utf-8"))
            groups = data.get("target_groups", []) or []
            self.target_groups = [
                g["url"] for g in groups if isinstance(g, dict) and g.get("url")
            ]
            self.targeted_queries = list(data.get("targeted_queries", []) or [])
            if self.target_groups:
                logger.info(
                    "Targeted mode: %d curated group(s) + %d targeted query(ies) "
                    "from %s",
                    len(self.target_groups),
                    len(self.targeted_queries),
                    FB_TARGETS_PATH.name,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not load FB target groups: {e}")

    # ── Main entry point ─────────────────────────────────────────────────

    async def collect(self) -> list[dict]:
        """Run multi-phase Facebook collection.

        Returns list of lead dicts compatible with pipeline.raw_to_lead().

        Error isolation: each phase is independently wrapped. A single
        phase failure does not abort the entire run. Between phases,
        if the previous phase produced 0 leads, a lightweight health
        re-check is performed to detect mid-session degradation.
        """
        await self._ensure_browser()

        # Phase 0: Session health check
        healthy = await self._session_health_check()
        if not healthy:
            logger.error(
                "Facebook session is not healthy (blocked/logged out/checkpoint). "
                "Run: python -m collector.setup facebook"
            )
            return []

        all_leads: list[dict] = []
        auth_error = False
        consecutive_empty_phases = 0

        # ═════════════════════════════════════════════════════════════════
        # Phase 1: Group search (mbasic only — www groups don't expose posts)
        # ═════════════════════════════════════════════════════════════════
        if not self._use_www and not auth_error and len(all_leads) < self.max_posts:
            logger.info("─ Phase 1: Group Search ─")
            try:
                group_leads = await self._collect_from_groups()
                all_leads.extend(group_leads)
                self._stats["leads_from_groups"] = len(group_leads)
                logger.info("  Groups phase: %d leads", len(group_leads))
                if len(group_leads) == 0:
                    consecutive_empty_phases += 1
                    logger.warning(
                        "  Groups phase produced 0 leads — possible "
                        "mbasic degradation (empty_phases=%d)",
                        consecutive_empty_phases,
                    )
                else:
                    consecutive_empty_phases = 0
            except AuthErrorDetected:
                auth_error = True
                self._stats["auth_errors"] += 1
                logger.error("  Groups phase ABORTED — auth error detected")
            except Exception:
                logger.exception(
                    "  Groups phase FAILED — all group queries lost. "
                    "Continuing to next phase."
                )
            # Idle period between phases (mimic human switching tasks)
            await human_delay(2.0, 0.5, "phase-transition")
        elif self._use_www:
            logger.info("─ Phase 1: Group Search (skipped on www) ─")

        # ═════════════════════════════════════════════════════════════════
        # Phase 2: Page/company search
        # ═════════════════════════════════════════════════════════════════
        if not auth_error and len(all_leads) < self.max_posts:
            # If previous phase generated 0 leads, run lightweight re-check
            if consecutive_empty_phases >= 1:
                if not await self._quick_health_recheck("before-pages"):
                    logger.error("Mid-session health re-check FAILED — aborting remaining phases")
                    auth_error = True

            if not auth_error:
                logger.info("─ Phase 2: Page Search ─")
                try:
                    page_leads = await self._collect_from_pages()
                    all_leads.extend(page_leads)
                    self._stats["leads_from_pages"] = len(page_leads)
                    logger.info("  Pages phase: %d leads", len(page_leads))
                    if len(page_leads) == 0:
                        consecutive_empty_phases += 1
                        logger.warning(
                            "  Pages phase produced 0 leads "
                            "(empty_phases=%d)",
                            consecutive_empty_phases,
                        )
                    else:
                        consecutive_empty_phases = 0
                except AuthErrorDetected:
                    auth_error = True
                    self._stats["auth_errors"] += 1
                    logger.error("  Pages phase ABORTED — auth error detected")
                except Exception:
                    logger.exception(
                        "  Pages phase FAILED — all page queries lost. "
                        "Continuing to next phase."
                    )
            await human_delay(2.0, 0.5, "phase-transition")

        # ═════════════════════════════════════════════════════════════════
        # Phase 3: General post search (mbasic only — www returns 0 results)
        # ═════════════════════════════════════════════════════════════════
        if not self._use_www and not auth_error and len(all_leads) < self.max_posts:
            if consecutive_empty_phases >= 2:
                if not await self._quick_health_recheck("before-posts"):
                    logger.error(
                        "Mid-session health re-check FAILED after %d empty phases "
                        "— aborting remaining phases",
                        consecutive_empty_phases,
                    )
                    auth_error = True

            if not auth_error:
                logger.info("─ Phase 3: Post Search ─")
                try:
                    post_leads = await self._collect_from_posts()
                    all_leads.extend(post_leads)
                    self._stats["leads_from_posts"] = len(post_leads)
                    logger.info("  Posts phase: %d leads", len(post_leads))
                except AuthErrorDetected:
                    auth_error = True
                    self._stats["auth_errors"] += 1
                    logger.error("  Posts phase ABORTED — auth error detected")
                except Exception:
                    logger.exception(
                        "  Posts phase FAILED — all post queries lost."
                    )

        # ── Post-processing: dedup + relevance score ─────────────────────
        all_leads = self._deduplicate_leads(all_leads)
        all_leads = all_leads[:self.max_posts]

        # ── Summary ─────────────────────────────────────────────────────
        logger.info(
            "Facebook collection complete: %d unique leads "
            "(groups=%d, pages=%d, posts=%d, skipped_irrelevant=%d)",
            len(all_leads),
            self._stats['leads_from_groups'],
            self._stats['leads_from_pages'],
            self._stats['leads_from_posts'],
            self._stats['skipped_irrelevant'],
        )
        return all_leads

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 0: Session Health Check
    # ═══════════════════════════════════════════════════════════════════════

    async def _session_health_check(self) -> bool:
        """Pre-flight check: verify mbasic is accessible and we're not blocked.

        Strategy (redesigned to avoid the _use_www trap):
        1. Try mbasic.facebook.com — if FB redirects to www, do NOT switch.
           www search pages are JS SPAs that BeautifulSoup cannot parse.
           Switching to www was the #1 root cause of 0-result collection runs.
        2. If redirected, test a direct mbasic search URL as fallback.
        3. Execute a smoke mini-search and verify results contain parseable links.
        4. Check auth/block indicators on every loaded page.

        The _use_www flag is ONLY for manual (headless=False) mode.
        Automated collection must always use mbasic.
        """
        logger.info("Running session health check...")
        try:
            async with await self._context.new_page() as check_page:
                # ── Step 1: Load mbasic homepage ─────────────────────────
                # _load_page retries transient timeouts/net errors (observed
                # 08-07..08-20: a single 15s goto timeout hard-failed every
                # run) and raises AuthErrorDetected on login/checkpoint/block.
                mbasic_works = False
                content = ""
                current_url = check_page.url
                try:
                    content = await self._load_page(
                        check_page, FB_BASE, timeout=15000, label="health-mbasic"
                    )
                    current_url = check_page.url
                    mbasic_works = True
                except AuthErrorDetected:
                    logger.warning("Health check FAILED — auth/block on mbasic")
                    self._session_healthy = False
                    return False
                except (MaxRetriesExceeded, asyncio.TimeoutError, ConnectionError):
                    # mbasic unreachable at the network level (not a logout) —
                    # fall through to the www fallback below instead of failing.
                    logger.warning(
                        "mbasic load failed after retries — testing www fallback"
                    )

                # ── Step 2: Check for checkpoint/security redirect ────────
                if mbasic_works:
                    for pattern in FB_CHECKPOINT_PATTERNS:
                        if pattern in current_url.lower():
                            logger.warning(
                                "Health check FAILED — checkpoint URL: %s",
                                current_url,
                            )
                            self._session_healthy = False
                            return False

                    # ── Step 3: If mbasic homepage redirected to www, ────
                    # try a direct mbasic search URL as fallback.
                    if "mbasic" not in current_url.lower():
                        logger.info(
                            "mbasic homepage redirected to www — "
                            "testing direct mbasic search URL..."
                        )
                        try:
                            content = await self._load_page(
                                check_page,
                                f"{FB_BASE}/search/groups/?q=fasteners",
                                timeout=15000,
                                label="health-mbasic-search",
                            )
                            current_url = check_page.url
                            mbasic_works = "mbasic" in current_url.lower()
                        except (AuthErrorDetected, MaxRetriesExceeded,
                                asyncio.TimeoutError, ConnectionError) as exc:
                            logger.warning(
                                "Direct mbasic search URL also failed: %s", exc
                            )
                            mbasic_works = False

                if not mbasic_works:
                    # mbasic is blocked (FB forces www for automated browsers).
                    # Fall back to www — the JS-rendered DOM is parseable when
                    # loaded through Playwright (page.content() returns full DOM).
                    logger.info(
                        "mbasic unavailable — falling back to www.facebook.com"
                    )
                    self._use_www = True
                    try:
                        content = await self._load_page(
                            check_page,
                            f"{FB_WWW}/search/groups/?q=fasteners",
                            timeout=30000,
                            label="health-www",
                        )
                        current_url = check_page.url
                    except (AuthErrorDetected, MaxRetriesExceeded,
                            asyncio.TimeoutError, ConnectionError) as exc:
                        logger.warning("www fallback search failed: %s", exc)
                        self._session_healthy = False
                        return False

                # ── Step 4: Detect login page on current URL ─────────────
                if self._is_login_page(content):
                    logger.warning("Health check FAILED — login page detected")
                    self._session_healthy = False
                    return False

                # ── Step 5: Smoke test — load search & verify results ────
                # If we aren't already on a search page, navigate to one.
                if "search" not in current_url.lower():
                    search_url = (
                        f"{FB_WWW}/search/groups/?q=fasteners"
                        if self._use_www
                        else f"{FB_BASE}/search/groups/?q=fasteners"
                    )
                    try:
                        content = await self._load_page(
                            check_page, search_url, timeout=15000,
                            label="health-smoke",
                        )
                        current_url = check_page.url
                    except Exception as exc:
                        logger.warning("Smoke-test search failed to load: %s", exc)

                # ── Step 6: Check auth/block on search results page ───────
                if self._is_blocked_or_auth(content):
                    logger.warning("Health check FAILED — auth/block on search page")
                    self._session_healthy = False
                    return False

                if self._is_login_page(content):
                    logger.warning("Health check FAILED — login on search page")
                    self._session_healthy = False
                    return False

                # ── Step 7: Verify search results are non-empty ───────────
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(content, "html.parser")
                # Look for result links (works for both mbasic and www format)
                result_links = (
                    (soup.select("a[href*='/groups/']") if soup else []) +
                    (soup.select("a[href*='/pages/']") if soup else []) +
                    (soup.select("a[href*='/posts/']") if soup else [])
                )
                if len(result_links) == 0:
                    # Shadow-block: FB serves empty results to detected bots.
                    # Treat as a hard failure — proceeding wastes the whole
                    # 3-day window on a 0-lead run (observed 2026-08-06).
                    logger.error(
                        "Health check FAILED — search returned 0 parseable "
                        "result links (shadow-block: FB serves empty results "
                        "to detected bots). Content length=%d, URL=%s",
                        len(content), current_url[:120],
                    )
                    self._session_healthy = False
                    return False

                logger.info(
                    "Health check PASSED — mbasic available, %d result links, "
                    "URL=%s", len(result_links), current_url[:80]
                )
                self._session_healthy = True
                # NEVER use www for automated collection — www is JS SPA
                # and cannot be parsed by BeautifulSoup.
                if self.headless:
                    self._use_www = False
                return True

        except Exception as e:
            logger.warning("Session health check error: %s", e, exc_info=True)
            # If the check itself crashes, the browser/env is likely in a
            # bad state. Fail fast — don't silently assume healthy.
            self._rate_limiter.report_failure()
            self._session_healthy = False
            return False

    async def _quick_health_recheck(self, context: str = "") -> bool:
        """Lightweight mid-session health re-check.

        Called between phases when the previous phase produced 0 leads.
        Only does a fast page load + auth check — no full smoke test.
        Fails fast (< 10s) so we don't waste time on a dead session.

        Args:
            context: Label for log messages (e.g. "before-pages").
        """
        label = f"[{context}] " if context else ""
        logger.info("%sQuick health re-check...", label)
        try:
            async with await self._context.new_page() as page:
                # Same retry treatment as _session_health_check: a transient
                # timeout must not hard-fail a mid-session recheck.
                content = await self._load_page(
                    page, FB_BASE, timeout=10000, label=f"recheck-{context}"
                )
                current_url = page.url

                # Fast auth/block check only
                if self._is_login_page(content):
                    logger.warning(
                        "%sRe-check FAILED — login page detected at %s",
                        label, current_url[:100],
                    )
                    self._rate_limiter.report_rate_limited()
                    self._session_healthy = False
                    return False

                if self._is_blocked_or_auth(content):
                    logger.warning(
                        "%sRe-check FAILED — auth/block detected at %s",
                        label, current_url[:100],
                    )
                    self._rate_limiter.report_rate_limited()
                    self._session_healthy = False
                    return False

                self._rate_limiter.report_success()
                logger.info("%sRe-check OK", label)
                return True
        except Exception as e:
            logger.warning("%sRe-check error: %s", label, e)
            self._rate_limiter.report_failure()
            return False

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 1: Group Collection
    # ═══════════════════════════════════════════════════════════════════════

    async def _collect_from_groups(self) -> list[dict]:
        """Extract leads from Facebook groups.

        Targeted mode: visit curated trade groups directly (deep post + About
        extraction), then fall back to targeted/broad group search only if the
        curated groups under-deliver. Broad mode: legacy group search only.
        """
        leads: list[dict] = []
        groups_checked = 0

        async def _visit_group(group_url: str) -> None:
            nonlocal groups_checked, leads
            groups_checked += 1
            if group_url in self._visited_urls:
                return
            self._visited_urls.add(group_url)
            group_leads = await self._extract_from_group(group_url)
            for lead in group_leads:
                if self._is_industry_relevant(lead):
                    leads.append(lead)
                else:
                    self._stats["skipped_irrelevant"] += 1
            await human_delay(1.5, 0.4, "between-groups")

        # Targeted mode: curated groups first — these are known fastener trade
        # communities (the only source of the 62 leads ever found).
        search_queries = list(self.group_queries)
        if self.targeted and self.target_groups:
            logger.info(
                f"  Targeted: visiting {len(self.target_groups)} curated group(s)"
            )
            for group_url in self.target_groups:
                if len(leads) >= self.max_posts:
                    break
                try:
                    await _visit_group(group_url)
                except AuthErrorDetected:
                    raise
                except Exception as e:
                    logger.warning(f"  Targeted group error {group_url}: {e}")

            # Prefer the proven targeted queries, fall back to broad list.
            if self.targeted_queries:
                search_queries = list(self.targeted_queries) + search_queries

        for query in search_queries:
            if len(leads) >= self.max_posts:
                break
            if len(leads) >= self.max_per_group * len(search_queries):
                break

            logger.info(f"  Searching groups: '{query}'")
            try:
                group_urls = await self._search_groups(query, max_groups=3)
                for group_url in group_urls:
                    if len(leads) >= self.max_posts:
                        break
                    try:
                        await _visit_group(group_url)
                    except AuthErrorDetected:
                        raise
                    except Exception as e:
                        logger.warning(f"  Group extraction error for '{query}': {e}")

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.warning(f"  Group search error for '{query}': {e}")
                continue

        self._stats["groups_checked"] = groups_checked
        return leads

    async def _search_groups(self, query: str, max_groups: int = 5) -> list[str]:
        """Search for public Facebook groups matching a query."""
        group_urls: list[str] = []
        search_url = self._fb_url(f"/search/groups/?q={quote(query)}")

        await self._rate_limiter.acquire()
        async with await self._context.new_page() as page:
            try:
                content = await self._load_page(
                    page, search_url, timeout=30000, label="group-search"
                )
                await human_scroll(page, times=2)

                soup = await self._parse_html(content)
                if soup is None:
                    return group_urls

                # Extract group links
                for link in soup.select("a[href]"):
                    href = link.get("href", "")
                    # Group URLs: /groups/xxxxx
                    if "/groups/" in href and "search" not in href.lower():
                        full_url = urljoin(FB_WWW if self._use_www else FB_BASE, href.split("?")[0])
                        if full_url not in group_urls:
                            group_urls.append(full_url)
                            if len(group_urls) >= max_groups:
                                break

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Group search page error: {e}")

        return group_urls[:max_groups]

    async def _extract_from_group(self, group_url: str) -> list[dict]:
        """Visit a public group, extract leads from posts + About section.

        Two-phase: (1) scrape posts for contact info, (2) visit About section
        for group description, website, and admin contact details.
        """
        leads: list[dict] = []

        await self._rate_limiter.acquire()
        async with await self._context.new_page() as page:
            try:
                content = await self._load_page(
                    page, group_url, timeout=30000, label="group-page"
                )
                await human_scroll(page, times=3)

                soup = await self._parse_html(content)
                if soup is None:
                    return leads

                # Extract group name
                group_name = ""
                title_el = soup.select_one("title")
                if title_el:
                    group_name = title_el.get_text(strip=True)
                    group_name = group_name.replace(" | Facebook", "").strip()

                # Check group relevance
                if group_name and not self._is_industry_relevant(
                    {"company_name": group_name, "notes": ""}
                ):
                    logger.debug(f"  Skipping non-relevant group: {group_name}")
                    return leads

                # Phase 1: Extract leads from group posts
                # On www, the DOM structure is different — use text extraction
                if self._use_www:
                    # Get visible text from the rendered page
                    page_text = await self._extract_text_from_page(page)
                    if page_text:
                        lead = self._extract_contacts_from_text(
                            page_text, group_url, group_name
                        )
                        if lead:
                            leads.append(lead)
                else:
                    # mbasic: traditional HTML selectors
                    post_sections = soup.select(
                        "[data-ft], article, .story_body_container, "
                        '[role="article"], .userContent, .storyContentWrapper'
                    )
                    if not post_sections:
                        post_sections = soup.select("div > p, div > span > p")

                    for section in post_sections[:self.max_per_group]:
                        post_text = section.get_text(separator=" ", strip=True)
                        if len(post_text) < 20:
                            continue

                        lead = self._parse_post_text(post_text, group_url, group_name)
                        if lead:
                            leads.append(lead)

                # Phase 2: Visit group About section for contact info
                about_info = await self._visit_group_about(page, group_url, group_name)
                if about_info:
                    # Merge About info into existing leads or add as new lead
                    for lead in leads:
                        if not lead.get("website") and about_info.get("website"):
                            lead["website"] = about_info["website"]
                        if not lead.get("email") and about_info.get("email"):
                            lead["email"] = about_info["email"]
                        if not lead.get("whatsapp") and about_info.get("whatsapp"):
                            lead["whatsapp"] = about_info["whatsapp"]
                        if about_info.get("industry"):
                            lead["industry"] = about_info["industry"]
                    # If no leads from posts but About has contact info, add it
                    if not leads and any([about_info.get("email"),
                                          about_info.get("website"),
                                          about_info.get("whatsapp")]):
                        leads.append(about_info)

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Group extraction error: {e}")

        return leads

    async def _visit_group_about(
        self, page, group_url: str, group_name: str
    ) -> Optional[dict]:
        """Visit the About section of a Facebook group to extract contact info.

        Group About sections often contain:
          - Group description (may have website, email)
          - Admin contact info
          - External links
        """
        about_urls = [
            group_url.rstrip("/") + "/about",
            group_url.rstrip("/") + "?view=about",
        ]

        for about_url in about_urls:
            try:
                await self._rate_limiter.acquire()
                content = await self._load_page(
                    page, about_url, timeout=20000, label="group-about"
                )
                await human_scroll(page, times=2)

                if self._is_blocked_or_auth(content):
                    continue

                soup = await self._parse_html(content)
                if soup is None:
                    continue

                about_text = soup.get_text(separator=" ", strip=True)

                if len(about_text) < 50:
                    continue

                # Extract emails
                emails = EMAIL_RE.findall(about_text)
                emails = [e for e in emails if "facebook" not in e.lower()]

                # Extract website from external links
                website = None
                for link in soup.select("a[href*='http']"):
                    href = link.get("href", "")
                    if any(d in href.lower() for d in
                           ["facebook.com", "fb.com", "fb.me", "messenger.com",
                            "instagram.com", "l.facebook.com"]):
                        continue
                    website = href
                    break

                # Extract WhatsApp
                whatsapp = self._extract_whatsapp_from_text(about_text)

                # Extract phones
                phones = PHONE_RE.findall(about_text)
                phones = [p for p in phones if len(re.sub(r"[^\d+]", "", p)) >= 8]

                about_lead = {
                    "company_name": self._clean_name(group_name) or group_name,
                    "contact_name": None,
                    "email": emails[0] if emails else None,
                    "phone": phones[0] if phones else None,
                    "whatsapp": whatsapp or (self._find_whatsapp(phones)),
                    "website": website,
                    "industry": self._classify_industry(about_text),
                    "country": self._infer_country(about_text, group_name),
                    "source_url": about_url,
                    "source_detail": json.dumps({
                        "platform": "facebook",
                        "section": "group_about",
                        "group_url": group_url,
                        "group_name": group_name,
                    }, ensure_ascii=False),
                    "notes": self._truncate(about_text, 500),
                }

                logger.debug(f"  Group About: email={bool(about_lead['email'])}, "
                             f"website={bool(about_lead['website'])}")
                return about_lead

            except Exception as e:
                logger.debug(f"  Group About visit error: {e}")
                continue

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 2: Page Collection
    # ═══════════════════════════════════════════════════════════════════════

    async def _collect_from_pages(self) -> list[dict]:
        """Search for fastener-related company/business pages."""
        leads: list[dict] = []
        pages_checked = 0

        for query in self.page_queries:
            if len(leads) >= self.max_per_page * len(self.page_queries):
                break

            logger.info(f"  Searching pages: '{query}'")
            try:
                page_urls = await self._search_pages(query, max_pages=3)
                pages_checked += len(page_urls)

                for page_url in page_urls:
                    if len(leads) >= self.max_posts:
                        break
                    if page_url in self._visited_urls:
                        continue
                    self._visited_urls.add(page_url)

                    lead = await self._extract_from_page(page_url)
                    if lead and self._is_industry_relevant(lead):
                        leads.append(lead)
                    elif lead:
                        self._stats["skipped_irrelevant"] += 1

                    await human_delay(1.0, 0.4, "between-pages")

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.warning(f"  Page search error for '{query}': {e}")
                continue

        self._stats["pages_checked"] = pages_checked
        return leads

    async def _search_pages(self, query: str, max_pages: int = 5) -> list[str]:
        """Search for Facebook pages matching a query.

        On www, page links use profile.php?id=xxx or custom usernames
        (e.g. /clinchnuts), not the old /pages/xxx pattern.
        """
        page_urls: list[str] = []
        search_url = self._fb_url(f"/search/pages/?q={quote(query)}")

        await self._rate_limiter.acquire()
        async with await self._context.new_page() as page:
            try:
                content = await self._load_page(
                    page, search_url, timeout=30000, label="page-search"
                )
                await human_scroll(page, times=3)

                soup = await self._parse_html(content)
                if soup is None:
                    return page_urls

                # www: extract links using evaluate for more reliability
                if self._use_www:
                    try:
                        raw_links = await page.evaluate("""() => {
                            const links = document.querySelectorAll('a[href]');
                            const seen = new Set();
                            const results = [];
                            for (const a of links) {
                                const href = a.getAttribute('href');
                                if (!href || seen.has(href)) continue;
                                seen.add(href);
                                const text = (a.textContent || '').trim();
                                // Skip navigation, search, help links
                                if (href.includes('/search/') ||
                                    href.includes('/login') ||
                                    href.includes('/help/') ||
                                    href.includes('/settings/') ||
                                    href.includes('/privacy/') ||
                                    href.includes('/hashtag/') ||
                                    href.includes('/watch/') ||
                                    href.includes('/marketplace/') ||
                                    href.includes('/events/') ||
                                    href.includes('/photo') ||
                                    href.includes('/video') ||
                                    href.includes('/reel') ||
                                    href.includes('/posts/') ||
                                    href.includes('/groups/') ||
                                    href === '#' ||
                                    href === '/') continue;
                                // Must have meaningful text
                                if (text.length < 3) continue;
                                results.push({href, text: text.substring(0, 80)});
                            }
                            return results;
                        }""")
                        for item in raw_links:
                            href = item['href']
                            # Skip tracking-only relative URLs
                            if href.startswith('?'):
                                continue
                            if href.startswith('/'):
                                full_url = urljoin(FB_WWW, _clean_fb_url(href))
                            elif 'facebook.com' in href:
                                full_url = _clean_fb_url(href)
                            else:
                                continue
                            if full_url not in page_urls:
                                page_urls.append(full_url)
                                if len(page_urls) >= max_pages:
                                    break
                    except Exception as e:
                        logger.debug(f"  www link extraction error: {e}")

                # Fallback: traditional BeautifulSoup extraction (mbasic)
                if not page_urls:
                    for link in soup.select("a[href]"):
                        href = link.get("href", "")
                        if (
                            href.startswith("/")
                            and "/groups/" not in href
                            and "/search/" not in href
                            and "/posts/" not in href
                            and "/permalink" not in href
                            and "/story" not in href
                            and href.count("/") >= 1
                            and len(href) > 3
                        ):
                            skip_prefixes = [
                                "/help/", "/settings/", "/privacy/", "/terms/",
                                "/hashtag/", "/watch/", "/marketplace/", "/events/",
                                "/photo", "/video", "/reel",
                            ]
                            if any(href.startswith(p) for p in skip_prefixes):
                                continue
                            full_url = _clean_fb_url(urljoin(
                                FB_WWW if self._use_www else FB_BASE, href
                            ))
                            if full_url not in page_urls:
                                page_urls.append(full_url)
                                if len(page_urls) >= max_pages:
                                    break

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Page search error: {e}")

        return page_urls[:max_pages]

    async def _extract_from_page(self, page_url: str) -> Optional[dict]:
        """Visit a Facebook page and extract business contact information.

        Two-phase: (1) main page -> name + visible text, (2) About section -> contact details.
        On www, uses text extraction from rendered DOM (more reliable).
        """
        await self._rate_limiter.acquire()
        async with await self._context.new_page() as page:
            try:
                content = await self._load_page(
                    page, page_url, timeout=30000, label="page"
                )
                await human_scroll(page, times=4)

                soup = await self._parse_html(content)
                if soup is None:
                    return None

                # Extract page name (title tag works on mbasic only;
                # on www the title is always "(N+) Facebook")
                page_name = ""
                title_el = soup.select_one("title")
                if title_el:
                    raw_title = title_el.get_text(strip=True)
                    raw_title = raw_title.replace(" | Facebook", "").strip()
                    # www title is just "Facebook" or "(N+) Facebook" — useless
                    if raw_title and raw_title.lower() not in (
                        "facebook", "(20+) facebook", "(1) facebook",
                    ) and not re.match(r'^\(\d+\+?\)\s*facebook$', raw_title, re.IGNORECASE):
                        page_name = raw_title

                # ── www path: use rendered-text extraction ──────────
                if self._use_www:
                    page_text = await self._extract_text_from_page(page)
                    if page_text:
                        # Extract company name from visible text
                        # (www title tag is always "Facebook")
                        if not page_name:
                            page_name = _extract_name_from_www_text(page_text) or ""
                        lead = self._extract_contacts_from_text(
                            page_text[:8000], page_url, page_name
                        )
                        if lead:
                            # Try About section for more info
                            about_info = await self._visit_page_about(page, page_url)
                            if about_info:
                                if not lead.get("email") and about_info.get("email"):
                                    lead["email"] = about_info["email"]
                                if not lead.get("website") and about_info.get("website"):
                                    lead["website"] = about_info["website"]
                                if not lead.get("phone") and about_info.get("phone"):
                                    lead["phone"] = about_info["phone"]
                            return lead
                        # Fall through to About attempt even if main page
                        # yielded nothing (try About directly)
                        about_only = await self._visit_page_about(page, page_url)
                        if about_only and any([about_only.get("email"),
                                                about_only.get("website")]):
                            about_only["company_name"] = about_only.get("company_name") or page_name
                            return about_only
                    return None

                # ── mbasic path ────────────────────────────────────
                # Extract all visible text
                body_text = ""
                for selector in ["#content", ".main-content", "body", "#root"]:
                    el = soup.select_one(selector)
                    if el:
                        body_text = el.get_text(separator=" ", strip=True)
                        break

                if not body_text:
                    body_text = soup.get_text(separator=" ", strip=True)

                body_text = body_text[:10000]

                # Phase 1: Extract contact info from page main text
                lead = self._parse_post_text(body_text, page_url, page_name)

                # Look for website links on main page
                if lead and not lead.get("website"):
                    for link in soup.select("a[href*='http']"):
                        href = link.get("href", "")
                        text = link.get_text(strip=True).lower()
                        if text in ("website", "visit website", "web", "www", "homepage",
                                    "link", "learn more", "shop now"):
                            if "facebook.com" not in href and "fb.com" not in href:
                                lead["website"] = href
                                break

                # Phase 2: Visit About section for more contact info
                about_info = await self._visit_page_about(page, page_url)
                if about_info and lead:
                    # Merge About info into lead (About data is higher quality)
                    if not lead.get("email") and about_info.get("email"):
                        lead["email"] = about_info["email"]
                    if not lead.get("website") and about_info.get("website"):
                        lead["website"] = about_info["website"]
                    if not lead.get("phone") and about_info.get("phone"):
                        lead["phone"] = about_info["phone"]
                    if not lead.get("whatsapp") and about_info.get("whatsapp"):
                        lead["whatsapp"] = about_info["whatsapp"]
                    # Update industry classification with About text
                    if about_info.get("notes"):
                        combined = f"{lead.get('notes', '')} {about_info['notes']}"
                        lead["notes"] = self._truncate(combined, 500)
                        lead["industry"] = self._classify_industry(
                            f"{lead.get('company_name', '')} {about_info['notes']}"
                        )
                elif about_info and not lead:
                    # Main page yielded nothing but About has info
                    lead = about_info
                    if page_name:
                        lead["company_name"] = lead["company_name"] or page_name

                # Extract WhatsApp
                if lead:
                    full_text = body_text
                    if about_info and about_info.get("notes"):
                        full_text += " " + about_info["notes"]
                    whatsapp = self._extract_whatsapp_from_text(full_text)
                    if whatsapp and not lead.get("whatsapp"):
                        lead["whatsapp"] = whatsapp

                return lead

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Page extraction error: {e}")
                return None

    async def _visit_page_about(self, page, page_url: str) -> Optional[dict]:
        """Visit the About section of a Facebook page to extract contact details.

        Facebook pages typically have contact info in /about or ?sk=about.
        Returns a lead dict with any contact info found.
        """
        # Build About URLs (handle both profile.php and custom URL patterns)
        about_urls = []
        if "profile.php" in page_url:
            # profile.php?id=xxx -> profile.php?id=xxx&sk=about
            about_urls = [
                page_url + "&sk=about",
                page_url + "&sk=page_info",
            ]
        else:
            about_urls = [
                page_url.rstrip("/") + "/about",
                page_url.rstrip("/") + "?sk=about",
                page_url.rstrip("/") + "?sk=page_info",
            ]

        for about_url in about_urls:
            try:
                await self._rate_limiter.acquire()
                content = await self._load_page(
                    page, about_url, timeout=20000, label="page-about"
                )
                await human_scroll(page, times=2)

                if self._is_blocked_or_auth(content):
                    continue

                # ── www: use text-based extraction ─────────────────
                if self._use_www:
                    about_text = await self._extract_text_from_page(page)
                    if about_text and len(about_text) > 80:
                        lead = self._extract_contacts_from_text(
                            about_text, about_url,
                        )
                        if lead and any([lead.get("email"), lead.get("website"),
                                         lead.get("phone"), lead.get("whatsapp")]):
                            lead["source_url"] = about_url
                            lead["source_detail"] = json.dumps({
                                "platform": "facebook",
                                "section": "about",
                                "page_url": page_url,
                            }, ensure_ascii=False)
                            return lead
                    continue

                # ── mbasic: HTML-based extraction ──────────────────
                soup = await self._parse_html(content)
                if soup is None:
                    continue

                about_text = soup.get_text(separator=" ", strip=True)

                # Skip if this looks like the same page (no about content)
                if len(about_text) < 100:
                    continue

                # Extract emails from About
                emails = EMAIL_RE.findall(about_text)
                emails = [e for e in emails if "facebook" not in e.lower()]

                # Extract website — look for external URLs in About
                website = None
                for link in soup.select("a[href*='http']"):
                    href = link.get("href", "")
                    # Skip FB internal links
                    if any(d in href.lower() for d in
                           ["facebook.com", "fb.com", "fb.me", "messenger.com",
                            "instagram.com", "l.facebook.com"]):
                        continue
                    # External link in About → likely company website
                    website = href
                    break

                # Also check for "Website" label pattern
                if not website:
                    for a_tag in soup.select("a[href]"):
                        href = a_tag.get("href", "")
                        text = a_tag.get_text(strip=True).lower()
                        prev_text = a_tag.find_previous(string=True)
                        prev = prev_text.strip().lower() if prev_text else ""
                        if ("website" in text or "web" in text or "www" in text
                                or "website" in prev or "web" in prev):
                            if href and "http" in href:
                                if not any(d in href.lower() for d in
                                          ["facebook", "fb.com", "messenger"]):
                                    website = href
                                    break

                # Extract phones
                phones = PHONE_RE.findall(about_text)
                phones = [p for p in phones if len(re.sub(r"[^\d+]", "", p)) >= 8]

                # Extract WhatsApp
                whatsapp = self._extract_whatsapp_from_text(about_text)

                # Build About lead dict
                about_lead = {
                    "company_name": None,
                    "contact_name": None,
                    "email": emails[0] if emails else None,
                    "phone": phones[0] if phones else None,
                    "whatsapp": whatsapp or (self._find_whatsapp(phones)),
                    "website": website,
                    "industry": self._classify_industry(about_text),
                    "country": self._infer_country(about_text, ""),
                    "source_url": about_url,
                    "source_detail": json.dumps({
                        "platform": "facebook",
                        "section": "about",
                        "page_url": page_url,
                    }, ensure_ascii=False),
                    "notes": self._truncate(about_text, 500),
                }

                # Only return if we actually found something useful
                if any([about_lead["email"], about_lead["website"],
                        about_lead["phone"], about_lead["whatsapp"]]):
                    logger.debug(f"  About page found: email={bool(about_lead['email'])}, "
                                 f"website={bool(about_lead['website'])}, "
                                 f"phone={bool(about_lead['phone'])}")
                    return about_lead

                # Try next URL pattern
            except Exception as e:
                logger.debug(f"  About page visit error ({about_url}): {e}")
                continue

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 3: Post Search (enhanced version of original)
    # ═══════════════════════════════════════════════════════════════════════

    async def _collect_from_posts(self) -> list[dict]:
        """Search Facebook posts for fastener-related leads.

        On www, public post search returns very few (if any) parseable
        results. We cap the number of queries to avoid wasting time.
        """
        all_leads: list[dict] = []
        auth_error = False

        # On www, post search is very limited — try only first N queries
        max_queries = 6 if self._use_www else len(self.queries)
        consecutive_empty = 0

        for query in self.queries[:max_queries]:
            if auth_error:
                break

            if len(all_leads) >= self.max_posts:
                break

            # On www: if first 3 queries return 0, skip remaining
            if self._use_www and consecutive_empty >= 3:
                logger.info(
                    "  www post search: %d empty queries — "
                    "skipping remaining %d queries",
                    consecutive_empty, max_queries - len(all_leads),
                )
                break

            logger.info(f"  Searching posts: '{query}'")
            try:
                leads = await self._search_and_extract_posts(query)
                if not leads:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0
                for lead in leads:
                    if self._is_industry_relevant(lead):
                        all_leads.append(lead)
                    else:
                        self._stats["skipped_irrelevant"] += 1
                logger.info(f"    '{query}': {len(leads)} posts → {len([l for l in leads if self._is_industry_relevant(l)])} relevant")
            except AuthErrorDetected:
                if not all_leads:
                    logger.warning(
                        "Facebook session not logged in. "
                        "Run first-time setup: python -m collector.setup facebook"
                    )
                logger.error(f"    '{query}': AUTH ERROR — stopping")
                auth_error = True
                break
            except Exception:
                logger.exception(f"    '{query}': failed")
                continue

        return all_leads

    async def _search_and_extract_posts(self, query: str) -> list[dict]:
        """Search Facebook for a query and extract leads from results."""
        leads: list[dict] = []
        post_urls: list[str] = []

        search_url = self._fb_url(f"/search/posts/?q={quote(query)}")
        try:
            post_urls = await self._crawl_search_results(
                search_url, max_posts=self.max_per_query
            )
        except Exception as e:
            logger.error(f"  Error crawling search results: {e}")
            return []

        if not post_urls:
            logger.info(f"  No posts found for '{query}'")
            return []

        for url in post_urls:
            if len(leads) >= self.max_per_query:
                break
            if url in self._visited_urls:
                continue
            self._visited_urls.add(url)

            try:
                lead = await self._extract_from_post(url)
                if lead:
                    leads.append(lead)
            except Exception:
                logger.debug(f"  Error extracting from post: {url}")
                continue

        self._stats["posts_checked"] += len(post_urls)
        return leads

    async def _crawl_search_results(
        self, search_url: str, max_posts: int
    ) -> list[str]:
        """Crawl Facebook search result pages and collect post URLs."""
        post_urls: list[str] = []
        current_url = search_url
        page = 1
        max_pages = max(3, max_posts // 10)

        async with await self._context.new_page() as fb_page:
            while len(post_urls) < max_posts and page <= max_pages:
                await self._rate_limiter.acquire()

                try:
                    content = await self._load_page(
                        fb_page, current_url, timeout=30000, label="search-results"
                    )
                    await human_scroll(fb_page, times=2)

                    soup = await self._parse_html(content)
                    if soup is None:
                        break

                    for link in soup.select("a[href]"):
                        href = link.get("href", "")
                        if any(pattern in href for pattern in [
                            "/story.php?", "/permalink.php?",
                            "/posts/", "/videos/",
                        ]):
                            full_url = urljoin(FB_WWW if self._use_www else FB_BASE, href)
                            if full_url not in post_urls:
                                post_urls.append(full_url)

                    # Find next page
                    next_link = None
                    for a in soup.select("a[href*='search/posts']"):
                        text = a.get_text(strip=True).lower()
                        if "see more" in text or "show more" in text or "load more" in text:
                            next_link = urljoin(FB_WWW if self._use_www else FB_BASE, a.get("href", ""))
                            break

                    if next_link and next_link != current_url:
                        current_url = next_link
                        page += 1
                    else:
                        break

                except AuthErrorDetected:
                    raise
                except Exception as e:
                    logger.warning(f"  Error on search page {page}: {e}")
                    break

        return post_urls[:max_posts]

    async def _extract_from_post(self, post_url: str) -> Optional[dict]:
        """Visit a Facebook post and extract lead information."""
        await self._rate_limiter.acquire()

        async with await self._context.new_page() as fb_page:
            try:
                content = await self._load_page(
                    fb_page, post_url, timeout=30000, label="post"
                )
                await human_scroll(fb_page, times=2)

                # ── www: use text-based extraction ─────────────────
                if self._use_www:
                    page_text = await self._extract_text_from_page(fb_page)
                    if page_text and len(page_text) > 50:
                        # Try to extract author from title
                        soup = await self._parse_html(content)
                        author = ""
                        if soup:
                            title_el = soup.select_one("title")
                            if title_el:
                                title_text = title_el.get_text(strip=True)
                                # Title format: "Author Name on Facebook"
                                if " on Facebook" in title_text:
                                    author = title_text.replace(" on Facebook", "").strip()
                        lead = self._extract_contacts_from_text(
                            page_text[:5000], post_url, author
                        )
                        if lead:
                            whatsapp = self._extract_whatsapp_from_text(page_text)
                            if whatsapp:
                                lead["whatsapp"] = whatsapp
                        return lead
                    return None

                # ── mbasic: HTML-based extraction ─────────────────
                soup = await self._parse_html(content)
                if soup is None:
                    return None

                post_text = ""
                for selector in [
                    "#m_story_permalink_view", ".story_body_container",
                    "[data-ft]", "article", ".userContent",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        post_text = el.get_text(separator=" ", strip=True)
                        break

                if not post_text:
                    body = soup.select_one("body")
                    if body:
                        post_text = body.get_text(separator=" ", strip=True)[:5000]

                author = ""
                for selector in ["h3 a", ".actor-link a", "strong a", ".profile-link"]:
                    el = soup.select_one(selector)
                    if el:
                        author = el.get_text(strip=True)
                        break

                comments_text = ""
                for selector in [".comment-container", ".comment", "[data-commentid]"]:
                    comments = soup.select(selector)
                    if comments:
                        comments_text = " ".join(
                            c.get_text(separator=" ", strip=True)
                            for c in comments[:20]
                        )
                        break

                full_text = f"{post_text}\n{comments_text}"
                lead = self._parse_post_text(full_text, post_url, author)
                if lead:
                    whatsapp = self._extract_whatsapp_from_text(full_text)
                    if whatsapp:
                        lead["whatsapp"] = whatsapp
                return lead

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Post extraction error: {e}")
                return None

    # ═══════════════════════════════════════════════════════════════════════
    # Text Parsing & Data Extraction
    # ═══════════════════════════════════════════════════════════════════════

    def _parse_post_text(
        self, text: str, source_url: str, author: str = ""
    ) -> Optional[dict]:
        """Parse post/page text and extract structured lead information.

        Central extraction method used by all phases (groups, pages, posts).
        """
        if not text or len(text.strip()) < 10:
            return None

        # Extract emails
        emails = EMAIL_RE.findall(text)
        # Filter out Facebook internal email patterns
        emails = [e for e in emails if "facebook" not in e.lower()]

        # Extract phones
        phones = PHONE_RE.findall(text)
        # Filter short/invalid phone numbers
        phones = [
            p for p in phones
            if len(re.sub(r"[^\d+]", "", p)) >= 8
        ]

        # Extract websites (exclude FB internal)
        websites_raw = WEBSITE_RE.findall(text)
        websites: list[str] = []
        for w in websites_raw:
            if not any(fb_domain in w.lower() for fb_domain in
                       ["facebook", "fb.com", "fb.me", "messenger", "instagram"]):
                websites.append(w)

        # Extract WhatsApp
        whatsapp = self._extract_whatsapp_from_text(text)

        # Build lead dict
        company_name = self._clean_name(author) if author else None
        if not company_name and emails:
            # Derive company name from email domain
            domain = emails[0].split("@")[1]
            parts = domain.split(".")
            company_name = parts[0].capitalize() if parts else None

        # Build industry classification
        classification_text = f"{company_name or ''} {text[:500]}"
        industry = self._classify_industry(classification_text)
        has_procurement = self._detect_procurement_intent(classification_text)

        source_detail = {
            "platform": "facebook",
            "post_url": source_url,
            "author": author,
            "relevance_score": self._score_relevance(text, author),
            "procurement_intent": has_procurement,
            "industry_category": industry,
        }

        return {
            "company_name": company_name,
            "contact_name": None,
            "email": emails[0] if emails else None,
            "phone": phones[0] if phones else None,
            "whatsapp": whatsapp or (self._find_whatsapp(phones)),
            "website": websites[0] if websites else None,
            "industry": industry,
            "country": self._infer_country(text, author),
            "source_url": source_url,
            "source_detail": json.dumps(source_detail, ensure_ascii=False),
            "notes": self._truncate(text, 500),
        }

    def _extract_whatsapp_from_text(self, text: str) -> Optional[str]:
        """Extract WhatsApp contact from text.

        Detects:
          - wa.me/<number> links (short and full URLs)
          - chat.whatsapp.com/<code> group invite links
          - "WhatsApp: +xxx" or "WA: +xxx" patterns
          - "WhatsApp +xx xxxx xxxx" without colon
        """
        if not text:
            return None

        # Method 1: wa.me links
        wa_links = WHATSAPP_LINK_RE.findall(text)
        for link in wa_links:
            # Extract number from wa.me/123456789
            match = re.search(r"wa\.me/(\+?\d[\d-]+)", link)
            if match:
                number = match.group(1)
                number = re.sub(r"[^\d+]", "", number)
                if len(number) >= 8:
                    return number
            # chat.whatsapp.com invites — return the invite link itself
            if "chat.whatsapp.com" in link:
                return link  # invite link is the WhatsApp contact

        # Method 2: "WhatsApp: +xxx" or "WA: +xxx" patterns
        wa_text = WHATSAPP_TEXT_RE.findall(text)
        if wa_text:
            for match in wa_text:
                number = re.sub(r"[^\d+]", "", match)
                if len(number) >= 8:
                    return number

        # Method 3: "WhatsApp +xx xxxx xxxx" without colon (loose)
        loose_pattern = r"(?:whatsapp|wa)\s+(\+?\d[\d\s\-().]{7,})"
        loose_match = re.search(loose_pattern, text, re.IGNORECASE)
        if loose_match:
            number = re.sub(r"[^\d+]", "", loose_match.group(1))
            if len(number) >= 8:
                return number

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # Relevance Scoring
    # ═══════════════════════════════════════════════════════════════════════

    def _is_industry_relevant(self, lead: dict) -> bool:
        """Check if a lead is relevant to fastener-consuming industries.

        Broad matching: any lead in an industry that USES fasteners is relevant.
        Companies making cars, furniture, machinery, solar panels, etc. all need
        fasteners — we want to find their procurement contacts.

        Always returns True if the lead has valid contact info and no negative
        indicators — err on the side of keeping leads.
        """
        # If there's solid contact info, keep it
        has_contact = bool(
            lead.get("email") or lead.get("website") or lead.get("whatsapp")
        )

        text_to_check = " ".join(
            str(v) for v in [
                lead.get("company_name", ""),
                lead.get("notes", ""),
                lead.get("industry", ""),
            ] if v
        ).lower()

        if len(text_to_check) < 15:
            return has_contact

        # Check multi-industry keyword relevance
        keyword_hits = 0
        for kw in INDUSTRY_KEYWORDS_FLAT:
            if kw.lower() in text_to_check:
                keyword_hits += 1
                if keyword_hits >= 1:
                    return True

        # Check procurement intent — strong buying signal
        if self._detect_procurement_intent(text_to_check):
            return True

        # No keyword hits but has contact info: keep it
        if has_contact and len(text_to_check) > 30:
            return True

        return False

    def _detect_procurement_intent(self, text: str) -> bool:
        """Detect if text shows buying/procurement intent.

        Posts saying "looking for supplier", "need manufacturer",
        "RFQ", "求购" etc. are high-value leads regardless of industry match.
        """
        text_lower = text.lower()
        for kw in PROCUREMENT_KEYWORDS:
            if kw.lower() in text_lower:
                return True
        return False

    def _classify_industry(self, text: str) -> str:
        """Classify which industry a lead belongs to based on keyword matching.

        Returns the industry category name (e.g. 'automotive', 'construction').
        Falls back to 'industrial' if no specific match.
        """
        text_lower = text.lower()
        scores: dict[str, int] = {}
        for category, keywords in INDUSTRY_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw.lower() in text_lower)
            if hits > 0:
                scores[category] = hits

        if not scores:
            return "industrial"

        # Return the category with the most keyword matches
        return max(scores, key=scores.get)  # type: ignore[arg-type]

    def _score_relevance(self, text: str, author: str = "") -> int:
        """Score the industry relevance of a post on a 0-5 scale.

        Factors: industry keywords + procurement intent + contact info signals.
        0 = not relevant, 5 = highly relevant (procurement intent + keywords).
        """
        combined = f"{text} {author}".lower()
        score = 0

        # Industry keyword density
        for kw in INDUSTRY_KEYWORDS_FLAT:
            if kw.lower() in combined:
                score += 1
            if score >= 3:
                break

        # Procurement intent bonus (+2)
        if self._detect_procurement_intent(combined):
            score += 2

        return min(score, 5)

    # ═══════════════════════════════════════════════════════════════════════
    # Deduplication
    # ═══════════════════════════════════════════════════════════════════════

    def _deduplicate_leads(self, leads: list[dict]) -> list[dict]:
        """Remove duplicate leads by email and by company name.

        Preserves the first occurrence (highest priority phase) and
        sorts by relevance score descending.
        """
        seen_emails: set[str] = set()
        seen_names: set[str] = set()
        unique: list[dict] = []

        for lead in leads:
            email = (lead.get("email") or "").strip().lower()
            name = (lead.get("company_name") or "").strip().lower()

            if email and email in seen_emails:
                continue
            if name and name in seen_names:
                continue

            if email:
                seen_emails.add(email)
            if name:
                seen_names.add(name)
            unique.append(lead)

        return unique

    # ═══════════════════════════════════════════════════════════════════════
    # Browser Management (enhanced anti-detection)
    # ═══════════════════════════════════════════════════════════════════════

    async def _ensure_browser(self):
        """Launch Playwright with persistent profile and anti-detection measures.

        Retries up to 3 times on failure with increasing intervals (5s/10s/20s).
        Cleans up partial browser state between attempts.
        """
        if self._setup_done:
            return

        # ── Profile lock guard ─────────────────────────────────────────────
        # A live Chromium holding data/fb_profile would make launch fail 3×
        # with "profile is already in use by another instance of Chromium"
        # (observed 2026-08-14). Detect it up front and abort gracefully so
        # the scheduled run is logged as a clean failure, not a crash loop.
        lock_pid = self._profile_lock_pid()
        if lock_pid is not None:
            raise RuntimeError(
                f"Facebook profile {self.profile_dir} is in use by a live "
                f"Chromium (PID {lock_pid}). Close that browser window first, "
                "then re-run. Skipping Facebook collection this round to avoid "
                "the 'profile already in use' crash."
            )

        from playwright.async_api import async_playwright

        max_retries = 3
        retry_delays = [5, 10, 20]
        last_error = None

        for attempt in range(max_retries):
            try:
                self._playwright = await async_playwright().start()
                self.profile_dir.mkdir(parents=True, exist_ok=True)

                logger.info(
                    "Launching Facebook browser (headless=%s, attempt=%d/%d)",
                    self.headless, attempt + 1, max_retries,
                )

                # Enhanced anti-detection browser arguments
                browser_args = [
                    # Core anti-detection
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    # Performance
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    # Privacy / anti-fingerprinting
                    "--disable-webrtc",
                    "--disable-geolocation",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-hang-monitor",
                    # Disable automation flags
                    "--disable-infobars",
                    "--disable-breakpad",
                    "--disable-sync",
                    "--no-default-browser-check",
                    "--disable-component-update",
                    # Language / locale
                    "--lang=en-US",
                ]

                # Always use mobile UA for mbasic compatibility.
                # Desktop UAs trigger redirect to www (JS SPA, unparseable).
                # Only use desktop UA when explicitly in www mode.
                if self._use_www:
                    ua = config.SCRAPE_USER_AGENT
                    vp = {"width": 1280, "height": 900}
                else:
                    ua = FB_MOBILE_UA
                    vp = {"width": 412, "height": 915}  # Pixel 7
                    browser_args.append(
                        "--user-agent=" + FB_MOBILE_UA
                    )

                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    viewport=vp,
                    user_agent=ua,
                    locale="en-US",
                    timezone_id="America/Chicago",
                    args=browser_args,
                    # Additional stealth: disable webdriver flag
                    bypass_csp=True,
                    ignore_https_errors=True,
                )

                # Inject stealth scripts to hide automation
                if self._context.pages:
                    await self._inject_stealth(self._context.pages[0])

                self._context.on("page", lambda page: asyncio.ensure_future(
                    self._inject_stealth(page)
                ))

                self._setup_done = True
                # Log profile size for diagnostics
                profile_size_mb = sum(
                    f.stat().st_size for f in self.profile_dir.rglob("*")
                    if f.is_file()
                ) / (1024 * 1024)
                logger.info(
                    "Facebook browser launched successfully "
                    "(profile=%.1fMB, pages=%d)",
                    profile_size_mb,
                    len(self._context.pages) if self._context else 0,
                )

                # ── Inject cookies from storage_state.json ──────────────
                # Chromium persistent context on Windows often fails to
                # load manually-persisted cookies from SQLite (DPAPI
                # encryption mismatch). Workaround: load storage_state.json
                # (saved during setup) and inject cookies via Playwright
                # API directly into browser memory.
                await self._inject_storage_state_cookies()

                return  # Success — exit retry loop

            except Exception as e:
                last_error = e
                logger.error(
                    "Browser launch attempt %d/%d failed: %s",
                    attempt + 1, max_retries, e,
                )
                await self._cleanup_browser_state()

                if attempt < max_retries - 1:
                    delay = retry_delays[attempt]
                    logger.info("Retrying browser launch in %ds...", delay)
                    await asyncio.sleep(delay)

        # All retries exhausted
        raise RuntimeError(
            f"Failed to launch Facebook browser after {max_retries} attempts. "
            f"Last error: {last_error}"
        ) from last_error

    async def _cleanup_browser_state(self):
        """Clean up partial browser state after a failed launch attempt.

        Must be safe to call even if nothing was initialized (all-None guard).
        """
        try:
            if self._context:
                await self._context.close()
                self._context = None
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        except Exception:
            pass
        self._setup_done = False

    async def _inject_stealth(self, page):
        """Inject stealth scripts to hide Playwright automation markers."""
        try:
            # Wait for the page to be ready
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            # Override navigator.webdriver
            await page.evaluate(
                "() => {"
                "  Object.defineProperty(navigator, 'webdriver', { get: () => false });"
                "  Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });"
                "  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'zh-CN'] });"
                "  window.chrome = { runtime: {} };"
                "}"
            )
        except Exception:
            # Stealth injection is best-effort; don't fail if it times out
            pass

    async def _inject_storage_state_cookies(self):
        """Load cookies from storage_state.json and inject into browser.

        Chromium persistent context on Windows stores cookies as DPAPI-
        encrypted blobs in SQLite. Manually-written plaintext cookies
        are ignored. This method bypasses SQLite entirely by injecting
        cookies directly into browser memory via Playwright's add_cookies().

        The storage_state.json file is saved by collector.setup after a
        successful manual login.
        """
        import json

        storage_path = self.profile_dir / "storage_state.json"
        if not storage_path.exists():
            logger.debug("No storage_state.json — skipping cookie injection")
            return

        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            cookies = state.get("cookies", [])
            if not cookies:
                logger.debug("storage_state.json has no cookies")
                return

            # Filter to FB-relevant cookies
            fb_cookies = [
                c for c in cookies
                if "facebook" in c.get("domain", "")
            ]

            if not fb_cookies:
                return

            # Inject storage_state cookies (add_cookies overwrites
            # existing cookies with same name+domain)
            await self._context.add_cookies(fb_cookies)

            # Verify injection
            critical = {"c_user", "xs"}
            recheck = await self._context.cookies()
            fb_recheck = {
                c["name"]: c["value"][:8] if c["value"] else "(empty)"
                for c in recheck
                if "facebook" in c.get("domain", "")
            }
            injected_ok = all(
                name in fb_recheck and fb_recheck[name] != "(empty)"
                for name in critical
            )

            if injected_ok:
                logger.info(
                    "Injected %d cookies from storage_state.json "
                    "(c_user + xs verified)",
                    len(fb_cookies),
                )
            else:
                logger.warning(
                    "Cookie injection may have failed: %s",
                    {k: fb_recheck.get(k, "MISSING") for k in critical},
                )

        except Exception as e:
            logger.warning("Failed to inject storage_state cookies: %s", e)

    # ═══════════════════════════════════════════════════════════════════════
    # Helpers (keep all existing method signatures for test compatibility)
    # ═══════════════════════════════════════════════════════════════════════

    async def _load_page(self, page, url: str, timeout: int = 30000, label: str = "") -> str:
        """Navigate to `url` and return page HTML, with retry + rate-limiter feedback.

        Wires the adaptive rate limiter feedback loop: previously only acquire()
        was ever called and report_success/failure/rate_limited never fired, so
        adaptive backoff and the circuit breaker were inert. Also activates the
        previously-unused RetryConfig for transient navigation errors.

        - Transient Playwright navigation errors (timeouts, net::*) are retried
          with exponential backoff via collector.retry.async_retry.
        - Auth/block pages raise AuthErrorDetected and report_rate_limited() so
          the limiter slows down immediately.
        - www mode waits for JS rendering before capturing content (mbasic is
          server-rendered and only needs a short settle).

        Raises:
            AuthErrorDetected: login/checkpoint/block page detected.
            MaxRetriesExceeded: all retries exhausted on a transient error.
        """
        async def _do() -> str:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                if self._use_www:
                    await asyncio.sleep(4.0)  # let JS render before content()
                    try:
                        await human_scroll(page, times=3)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.8)
                return await page.content()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Normalize transient Playwright navigation errors into
                # retryable ones so async_retry applies exponential backoff.
                cls = type(e).__name__.lower()
                msg = str(e).lower()
                if "timeout" in cls or "timeout" in msg:
                    raise asyncio.TimeoutError(str(e)) from e
                if "net::" in msg:
                    raise ConnectionError(str(e)) from e
                raise

        try:
            content = await async_retry(
                _do, config=self._retry_config, context=f"fb_{label or 'page'}",
            )
        except (AuthErrorDetected, RiskControlError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._rate_limiter.report_failure()
            raise

        if self._is_blocked_or_auth(content):
            self._rate_limiter.report_rate_limited()
            raise AuthErrorDetected(self.source_name, f"auth/block on {label or url[:60]}")

        self._rate_limiter.report_success()
        return content

    def _profile_lock_pid(self) -> Optional[int]:
        """Return the PID holding the Chromium profile lock, if that process is alive.

        Chromium writes a SingletonLock file (first line = owning PID) into the
        profile dir. A LIVE lock means another Chromium instance has the profile
        open — launching again would fail with "profile already in use" (the
        Aug-14 crash). A stale lock from a dead process is ignored (Chromium
        self-heals it on next launch).

        Returns None if there is no lock, the file is unparseable, or the PID
        is not alive.
        """
        lock = self.profile_dir / "SingletonLock"
        if not lock.exists():
            return None
        try:
            raw = lock.read_bytes()
            pid_line = raw.split(b"\n", 1)[0]
            pid = int(pid_line.decode(errors="ignore").strip())
        except Exception:
            return None
        if pid <= 0:
            return None
        return pid if _pid_alive(pid) else None

    async def _parse_html(self, content: str):
        """Parse HTML with BeautifulSoup, with error handling."""
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(content, "lxml")
        except Exception:
            return None

    async def _extract_text_from_page(self, page) -> str:
        """Extract visible text from a fully-rendered page.

        Uses page.evaluate() to get innerText from the DOM, which works
        regardless of HTML structure (mbasic or www).
        """
        try:
            text = await page.evaluate("() => document.body.innerText")
            return text or ""
        except Exception:
            try:
                content = await page.content()
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(content, "html.parser")
                return soup.get_text(separator=" ", strip=True) if soup else ""
            except Exception:
                return ""

    def _extract_contacts_from_text(
        self, text: str, source_url: str, source_name: str = ""
    ) -> Optional[dict]:
        """Extract contact info from visible text using regex.

        DOM-independent — works on both mbasic and www rendered pages.
        Central extraction method used as fallback when HTML selectors
        don't find content on www.
        """
        if not text or len(text.strip()) < 20:
            return None

        # ── Emails ──────────────────────────────────────────────────
        emails = EMAIL_RE.findall(text)
        emails = [e for e in emails if "facebook" not in e.lower()]

        # ── Phones ──────────────────────────────────────────────────
        phones = PHONE_RE.findall(text)
        phones = [p for p in phones
                   if len(re.sub(r"[^\d+]", "", p)) >= 8]

        # ── Websites ────────────────────────────────────────────────
        websites: list[str] = []
        for w in WEBSITE_RE.findall(text):
            if not any(d in w.lower() for d in
                       ["facebook", "fb.com", "fb.me", "messenger", "instagram"]):
                websites.append(w)

        # ── WhatsApp ────────────────────────────────────────────────
        whatsapp = self._extract_whatsapp_from_text(text)

        # ── Company name ────────────────────────────────────────────
        company_name = self._clean_name(source_name) if source_name else None
        if not company_name and emails:
            domain = emails[0].split("@")[1]
            parts = domain.split(".")
            company_name = parts[0].capitalize() if parts else None

        # ── Classification ──────────────────────────────────────────
        classification_text = f"{company_name or ''} {text[:500]}"
        industry = self._classify_industry(classification_text)
        has_procurement = self._detect_procurement_intent(classification_text)

        source_detail = {
            "platform": "facebook",
            "source_url": source_url,
            "source_name": source_name,
            "relevance_score": self._score_relevance(text, source_name),
            "procurement_intent": has_procurement,
            "industry_category": industry,
        }

        return {
            "company_name": company_name,
            "contact_name": None,
            "email": emails[0] if emails else None,
            "phone": phones[0] if phones else None,
            "whatsapp": whatsapp or (self._find_whatsapp(phones)),
            "website": websites[0] if websites else None,
            "industry": industry,
            "country": self._infer_country(text, source_name),
            "source_url": source_url,
            "source_detail": json.dumps(source_detail, ensure_ascii=False),
            "notes": self._truncate(text, 500),
        }

    def _is_login_page(self, html: str) -> bool:
        """Detect if the page is an actual Facebook login FORM (not a logged-in page).

        This is more specific than _is_blocked_or_auth — it only triggers on
        login form elements, not on JS-embedded strings that happen to contain
        'login' or 'password' in 3MB of JavaScript.
        """
        html_lower = html.lower()

        # Login form specific indicators (high confidence)
        login_form_indicators = [
            'id="login_form"',
            'id="loginbutton"',
            'name="login"',
            'action="/login/',
            '"loginform"',
            '"login_form"',
        ]

        form_hits = sum(1 for ind in login_form_indicators if ind in html_lower)

        # If we see 2+ login form elements, it's a real login page
        if form_hits >= 2:
            return True

        # "Log in to Facebook" as the page title (not just "Facebook")
        if "<title>log in" in html_lower or "<title>facebook – log in" in html_lower:
            return True

        return False

    def _is_blocked_or_auth(self, html: str) -> bool:
        """Detect Facebook login or block pages.

        Returns True if the page indicates we've been logged out or blocked.
        Excludes the logged-in www feed page where title is "(N+) Facebook".
        """
        html_lower = html.lower()

        # ── Exclude known logged-in pages ──────────────────────────────
        # Logged-in feed: title like "<title>(20+) Facebook</title>"
        if '<title>(' in html_lower and 'facebook</title>' in html_lower:
            return False

        # ── Remove script/style content before counting indicators ─────
        # (prevents false positives from JS bundles containing 'login' text)
        import re as _re
        visible_text = _re.sub(
            r'<script[^>]*>.*?</script>', '', html_lower,
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        visible_text = _re.sub(
            r'<style[^>]*>.*?</style>', '', visible_text,
            flags=_re.DOTALL | _re.IGNORECASE,
        )

        # ── High-signal: explicit login page title ─────────────────────
        if '<title>log in' in visible_text or '<title>facebook – log in' in visible_text:
            return True

        # ── Login form elements (visible text only) ────────────────────
        has_login_form = (
            'id="login_form"' in visible_text
            or 'id="loginbutton"' in visible_text
            or 'name="login"' in visible_text
        )

        # ── Count auth/block indicators in visible text ────────────────
        indicators_found = sum(
            1 for kw in FB_AUTH_INDICATORS if kw.lower() in visible_text
        )

        # ── Checkpoint URL patterns ────────────────────────────────────
        has_checkpoint = any(
            pattern in html_lower for pattern in FB_CHECKPOINT_PATTERNS
        )

        # Trigger: login form + indicators, OR checkpoint, OR 3+ block indicators
        return (
            (indicators_found >= 2 and has_login_form)
            or has_checkpoint
            or indicators_found >= 3  # block/security page without form still has 3+ indicators
        )

    def _fb_url(self, path: str) -> str:
        """Build a Facebook URL, using www if mbasic is redirecting.

        Appends _fb_noscript=1 to mbasic URLs to force the no-JS version,
        which is easier to parse and less likely to redirect.
        """
        base = FB_WWW if self._use_www else FB_BASE
        url = urljoin(base, path)
        # Force no-JS rendering on mbasic (prevents redirect + JS-rendered pages)
        if not self._use_www and "?" in url:
            url += "&_fb_noscript=1"
        elif not self._use_www:
            url += "?_fb_noscript=1"
        return url

    # ═══════════════════════════════════════════════════════════════════════
    # Helpers (keep all existing method signatures for test compatibility)
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _clean_name(name: Optional[str]) -> Optional[str]:
        """Clean up a company/person name extracted from Facebook."""
        if not name:
            return None
        # Strip notification count prefix: "(20+) Some Name" → "Some Name"
        name = re.sub(r'^\(\d+\+?\)\s+', '', name.strip())
        for artifact in [
            " | Facebook", " - Home", " - Posts", " - About",
            " - Community", " - Photos", " - Videos", " | FB",
        ]:
            name = name.replace(artifact, "")
        name = name.strip()
        # Filter out Facebook UI text that isn't a real company name
        non_names = {
            "facebook", "friends", "log in", "sign up", "log out",
            "news feed", "home", "messages", "notifications",
            "create post", "create story", "menu", "settings",
        }
        if name.lower() in non_names:
            return None
        return name if len(name) >= 2 else None

    @staticmethod
    def _find_whatsapp(phones: list[str]) -> Optional[str]:
        """Identify potential WhatsApp numbers (mobile format, starts with +)."""
        for phone in phones:
            clean = re.sub(r"[^\d+]", "", phone)
            # WhatsApp requires international format, 10-15 digits
            if len(clean) >= 10 and len(clean) <= 15 and clean.startswith("+"):
                return clean
        return None

    @staticmethod
    def _infer_country(text: str, author: str) -> Optional[str]:
        """Try to infer country from post text, profile URL, or location hints."""
        countries = [
            "United States", "USA", "Germany", "UK", "United Kingdom",
            "France", "Italy", "Spain", "Netherlands", "Poland",
            "Canada", "Australia", "Japan", "South Korea", "Brazil",
            "Mexico", "Turkey", "India", "Vietnam", "Thailand",
            "Indonesia", "Malaysia", "UAE", "Saudi Arabia", "South Africa",
            "Russia", "China", "Taiwan", "Singapore", "Egypt",
            "Nigeria", "Kenya", "Pakistan", "Bangladesh", "Philippines",
            "Argentina", "Chile", "Colombia", "Peru", "Romania",
            "Czech Republic", "Slovakia", "Hungary", "Ukraine",
            "Sweden", "Norway", "Denmark", "Finland", "Belgium",
            "Austria", "Switzerland", "Portugal", "Greece", "Israel",
            "Qatar", "Kuwait", "Oman", "Bahrain",
        ]
        combined = (text + " " + author).lower()
        for country in sorted(countries, key=len, reverse=True):
            if country.lower() in combined:
                return country
        return None

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """Truncate text to max_len characters at word boundary."""
        if len(text) <= max_len:
            return text
        truncated = text[:max_len]
        last_space = truncated.rfind(" ")
        return (truncated[:last_space] if last_space > max_len // 2 else truncated) + "..."

    # ═══════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════════════════════

    async def close(self):
        """Release Playwright resources."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        await super().close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
