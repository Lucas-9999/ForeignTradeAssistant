"""SegmentMatcher - maps leads to country/industry segments for template matching.

Segments are configured in data/segments.json (industry groups, country groups,
language overrides, per-industry insight paragraphs). Editing that file is the
only step needed to adjust grouping; no code changes required.

Industry matching (priority order):
  1. Keyword match against each industry group's match_keywords
  2. Otherwise low_relevance_keywords -> general + low_relevance
  3. Everything else (and missing industry) -> general

Country matching:
  1. Exact (case-insensitive) match against each country group's countries
  2. Everything else (and missing country) -> generic
  3. language_overrides per country override greeting/signoff
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

DEFAULT_SEGMENTS_PATH = Path(os.getenv(
    "FTA_SEGMENTS_PATH",
    str(config.DATA_DIR / "segments.json"),
))


class SegmentMatcher:
    """Maps lead country/industry values to segments defined in segments.json."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_SEGMENTS_PATH
        self._config = self._load()
        self._industry_groups: dict = self._config.get("industry_groups", {})
        self._low_keywords: list[str] = [
            kw.lower() for kw in self._config.get("low_relevance_keywords", [])
        ]
        self._country_groups: dict = self._config.get("country_groups", {})
        self._language_overrides: dict[str, dict] = {
            name.lower(): override
            for name, override in self._config.get("language_overrides", {}).items()
        }
        self._insights: dict = self._config.get("industry_insights", {})

    def _load(self) -> dict:
        if not self.config_path.exists():
            logger.warning("Segments file not found: %s", self.config_path)
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------------- Industry helpers ----------------

    def industry_group(self, industry: Optional[str]) -> tuple[str, bool]:
        """Return (group_name, is_low_relevance) for an industry label."""
        label = (industry or "").strip().lower()
        if not label:
            return "general", False
        ordered = sorted(
            (g for g in self._industry_groups if g != "general"),
            key=lambda g: int(self._industry_groups[g].get("priority", 99)),
        )
        for group in ordered:
            for kw in self._industry_groups[group].get("match_keywords", []):
                if kw.lower() in label:
                    return group, False
        for kw in self._low_keywords:
            if kw in label:
                return "general", True
        return "general", False

    def industry_group_names(self) -> list[str]:
        return list(self._industry_groups.keys())

    def industry_group_label(self, group: str) -> str:
        spec = self._industry_groups.get(group, {})
        return spec.get("label", group.replace("_", " ").title())

    def group_keywords(self, group: str) -> list[str]:
        spec = self._industry_groups.get(group, {})
        return [kw.lower() for kw in spec.get("match_keywords", [])]

    def industry_priority_order(self) -> list[str]:
        return sorted(
            self._industry_groups.keys(),
            key=lambda g: int(self._industry_groups[g].get("priority", 99)),
        )

    def low_relevance_keywords(self) -> list[str]:
        return list(self._low_keywords)

    def industry_insight(self, industry_group: str) -> str:
        return self._insights.get(
            industry_group,
            self._insights.get("general", ""),
        )

    # ---------------- Country helpers ----------------

    def country_group(self, country: Optional[str]) -> str:
        """Return the country group name (default: generic)."""
        name = (country or "").strip().lower()
        if not name:
            return "generic"
        for group, spec in self._country_groups.items():
            for c in spec.get("countries", []):
                if c.lower() == name:
                    return group
        return "generic"

    def region(self, country: Optional[str]) -> dict:
        """Return {country_group, region_name, greeting, signoff} for a country."""
        group = self.country_group(country)
        spec = self._country_groups.get(group, {})
        greeting = spec.get("greeting", "Hello")
        signoff = spec.get("signoff", "Best regards")
        label = spec.get("label", "Global (Other Markets)")
        override = self._language_overrides.get((country or "").strip().lower())
        if override:
            greeting = override.get("greeting", greeting)
            signoff = override.get("signoff", signoff)
        return {
            "country_group": group,
            "region_name": label,
            "greeting": greeting,
            "signoff": signoff,
        }

    def country_group_names(self) -> list[str]:
        return list(self._country_groups.keys())

    def country_group_label(self, group: str) -> str:
        spec = self._country_groups.get(group, {})
        return spec.get("label", group.replace("_", " ").title())

    def countries_for_group(self, group: str) -> list[str]:
        spec = self._country_groups.get(group, {})
        return list(spec.get("countries", []))

    # ---------------- SQL helpers (used by batch sender) ----------------

    def _instr_predicate(self, keywords: list[str]) -> str:
        if not keywords:
            return ""
        clauses = [
            "instr(lower(COALESCE(industry, '')), '{}') > 0".format(
                kw.replace("'", "''")
            )
            for kw in keywords
        ]
        return "(" + " OR ".join(clauses) + ")"

    def group_predicate(self, group: str) -> str:
        """SQL predicate matching leads whose industry belongs to a group.

        For specific groups, mirrors industry_group() semantics: a lead is in a
        group only if its keywords match AND no higher-priority group's keywords
        match first. This keeps the SQL filter consistent with the Python
        matcher when keywords overlap across groups (e.g. 'electric scooter
        motor' matches automotive's 'scooter' AND machinery's 'motor' — priority
        resolves it to automotive). Without this, an --industry-group filter
        selected leads the Python matcher would assign to a different group.
        """
        if group == "general":
            others = [
                self._instr_predicate(self.group_keywords(g))
                for g in self.industry_priority_order()
                if g != "general" and self.group_keywords(g)
            ]
            others_sql = " OR ".join(p for p in others if p)
            non_group = others_sql or "0"
            return (
                "(industry IS NULL OR trim(industry) = ''"
                f" OR NOT ({non_group}))"
            )

        pred = self._instr_predicate(self.group_keywords(group))
        if not pred:
            return ""

        ordered = self.industry_priority_order()
        if group not in ordered:
            return pred
        higher = [
            self._instr_predicate(self.group_keywords(g))
            for g in ordered[: ordered.index(group)]
            if g != "general" and self.group_keywords(g)
        ]
        if not higher:
            return pred
        return f"{pred} AND NOT (" + " OR ".join(higher) + ")"

    def low_relevance_predicate(self) -> str:
        return self._instr_predicate(self.low_relevance_keywords())

    def priority_case(self) -> str:
        """SQL CASE ordering core industries first, low-relevance last."""
        whens = []
        for group in self.industry_priority_order():
            if group == "general":
                continue
            pred = self.group_predicate(group)
            if pred:
                whens.append(f"WHEN {pred} THEN {self._priority(group)}")
        low = self.low_relevance_predicate()
        if low:
            whens.append(f"WHEN {low} THEN 99")
        if not whens:
            return "0"
        return "CASE " + " ".join(whens) + " ELSE 8 END"

    def _priority(self, group: str) -> int:
        return int(self._industry_groups.get(group, {}).get("priority", 8)) - 1
