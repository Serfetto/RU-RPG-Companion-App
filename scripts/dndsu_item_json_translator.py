#!/usr/bin/env python3
"""
Translate item_*.rpg.json resources using dnd.su item pages first and a
fallback translator for anything that cannot be sourced from the site.

Notes:
- dnd.su /items/ is rendered from /piece/items/index-list/, so the script
  builds its index from that HTML fragment;
- item resources can exist as the top-level file payload or nested inside
  another item resource;
- the script never rewrites item stats.name values into Russian; instead it
  stores translated titles in stats.ru_name;
- when dnd.su exposes only a family page for multiple item variants, the
  script only applies the translations it can adapt safely and leaves the
  rest to the fallback translator.
"""

from __future__ import annotations

import argparse
import copy
import glob
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dndsu_feat_json_translator import (
    DEFAULT_TRANSLATOR_TIMEOUT,
    DEFAULT_TRANSLATOR_URL,
    IGNORE_REMAINING_ENGLISH_PATH_PARTS,
    REQUEST_PAUSE_SEC,
    TIMEOUT,
    TRANSLATABLE_SUFFIXES,
    StringRef,
    TranslatorClient,
    canonical_key,
    english_title_key,
    load_json,
    log,
    looks_english,
    normalize_lookup_text,
    normalize_slug_for_compare,
    normalize_spaces,
    save_json,
    slug_similarity,
    slugify_name,
    strip_brackets_title,
    titles_match,
    token_overlap_ratio,
    translate_static_text,
)

BASE_URL = "https://dnd.su"
INDEX_URL = urljoin(BASE_URL, "/piece/items/index-list/")
CACHE_DIR = Path(".dndsu_cache") / "items"

PAGE_STOP_MARKERS = ("комментар", "галере")
HEADING_RE = re.compile(r"^(.*?)\s*\[([^\]]+)\](?:\s+.*)?$")

SPECIFIC_CANDIDATE_KINDS = {
    "original",
    "suffix_specific",
    "reordered_specific",
    "paren_specific",
}

ITEM_METADATA_PREFIXES = (
    "Распечатать",
    "Рекомендованная стоимость:",
    "Чудесный предмет",
    "Оружие",
    "Доспех",
    "Жезл",
    "Посох",
    "Кольцо",
    "Свиток",
    "Зелье",
    "Волшебная палочка",
    "Патроны",
)

STAGE_ALIASES = {
    "slumbering": "slumbering",
    "stirring": "stirring",
    "wakened": "wakened",
    "wakenend": "ascendant",
    "ascendant": "ascendant",
}

STAGE_ORDER = ("slumbering", "stirring", "wakened", "ascendant")

STAGE_TITLE_RU_MASC = {
    "slumbering": "Спящий",
    "stirring": "Пробуждающийся",
    "wakened": "Пробуждённый",
    "ascendant": "Восходящий",
}

STAGE_TITLE_RU_FEM = {
    "slumbering": "Спящая",
    "stirring": "Пробуждающаяся",
    "wakened": "Пробуждённая",
    "ascendant": "Восходящая",
}

BAG_OF_TRICKS_COLORS_RU = {
    "gray": "Серая",
    "rust": "Рыжая",
    "tan": "Коричневая",
}

DRAGON_TOUCHED_FOCUS_FAMILY_RU = {
    "chromatic": "хроматическая",
    "gem": "самоцветная",
    "metallic": "металлическая",
}

SPELL_SCROLL_LEVEL_RU = {
    "cantrip": "заговор",
    "1st level": "1-го уровня",
    "2nd level": "2-го уровня",
    "3rd level": "3-го уровня",
    "4th level": "4-го уровня",
    "5th level": "5-го уровня",
    "6th level": "6-го уровня",
    "7th level": "7-го уровня",
    "8th level": "8-го уровня",
    "9th level": "9-го уровня",
}


@dataclass(frozen=True)
class LookupCandidate:
    value: str
    kind: str


@dataclass
class ItemStatsRef:
    stats: Dict[str, Any]
    path: str


@dataclass
class SiteEntry:
    href: str
    title_ru: str
    title_en: str
    slug: str


@dataclass
class ItemPageData:
    title_ru: str = ""
    title_en: str = ""
    description: str = ""
    paragraphs: List[str] = field(default_factory=list)


@dataclass
class VariantInfo:
    kind: str
    token: str = ""
    base_name: str = ""
    extra: str = ""


@dataclass
class ResolvedItemPage:
    url: str
    expected_en_name: str
    exact_match: bool = True
    matched_candidate_kind: str = "original"
    matched_candidate_value: str = ""
    variant: Optional[VariantInfo] = None


def get_top_stats(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc.get("stats", {})


def clean_site_text(text: str) -> str:
    text = normalize_lookup_text(text)
    text = re.sub(r"\s*\[([^\]]*[A-Za-z][^\]]*)\]", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return normalize_spaces(text)


def lowercase_first(text: str) -> str:
    text = normalize_spaces(text)
    if not text:
        return text
    return text[:1].lower() + text[1:]


def normalize_stage(stage: str) -> str:
    return STAGE_ALIASES.get(normalize_lookup_text(stage).lower(), normalize_lookup_text(stage).lower())


def split_index_search_titles(search: str, fallback_ru: str) -> Tuple[str, str]:
    search = normalize_lookup_text(search)
    fallback_ru = normalize_lookup_text(fallback_ru)
    if not search:
        return fallback_ru, ""

    latin_match = re.search(r"[A-Za-z]", search)
    if not latin_match:
        return fallback_ru or search.strip(" ,"), ""

    split_at = latin_match.start()
    ru_title = search[:split_at].strip(" ,")
    en_title = search[split_at:].strip(" ,")
    return ru_title or fallback_ru, en_title


def detect_item_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    candidates: List[str] = []

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("item_"):
        stem = stem[len("item_") :]
    candidates.append(stem.replace("_", " "))
    candidates.append(stem.replace("-", " "))

    stats_id = normalize_lookup_text(stats.get("id", ""))
    if stats_id:
        candidates.append(stats_id.replace("_", " "))

    seen: set[str] = set()
    for candidate in candidates:
        candidate = normalize_lookup_text(candidate)
        key = canonical_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if looks_english(candidate):
            return candidate

    raise RuntimeError(f"Не удалось восстановить английское имя предмета из файла {path.name!r}")


def add_lookup_candidate(candidates: List[LookupCandidate], value: Optional[str], kind: str) -> None:
    if not value:
        return
    value = normalize_lookup_text(value)
    if not value:
        return
    key = canonical_key(value)
    if not key:
        return
    if any(canonical_key(existing.value) == key for existing in candidates):
        return
    candidates.append(LookupCandidate(value=value, kind=kind))


def build_lookup_candidates(name: str) -> List[LookupCandidate]:
    normalized = normalize_lookup_text(name)
    candidates: List[LookupCandidate] = []
    add_lookup_candidate(candidates, normalized, "original")

    bonus_match = re.match(r"^(\+\d+)\s+(.+)$", normalized)
    if bonus_match:
        base_name = normalize_lookup_text(bonus_match.group(2))
        add_lookup_candidate(candidates, base_name, "bonus_base")
        add_lookup_candidate(candidates, f"{base_name} +1, +2, +3", "bonus_range")

    stage_match = re.match(r"^(Slumbering|Stirring|Wakened|Wakenend|Ascendant)\s+(.+)$", normalized, flags=re.I)
    if stage_match:
        remainder = normalize_lookup_text(stage_match.group(2))
        add_lookup_candidate(candidates, remainder, "stage_base")
        if remainder.lower().endswith("dragon-touched focus"):
            add_lookup_candidate(candidates, "Dragon-Touched Focus", "stage_base")

    if "," in normalized:
        left, right = [normalize_spaces(part) for part in normalized.split(",", 1)]
        add_lookup_candidate(candidates, left, "comma_base")
        add_lookup_candidate(candidates, right, "suffix_specific")
        add_lookup_candidate(candidates, f"{right} {left}", "reordered_specific")

    paren_match = re.match(r"^(.+?)\s*\(([^()]+)\)$", normalized)
    if paren_match:
        base = normalize_spaces(paren_match.group(1))
        inner = normalize_spaces(paren_match.group(2))
        add_lookup_candidate(candidates, base, "parenthetical_base")
        add_lookup_candidate(candidates, f"{base} {inner}", "paren_specific")
        add_lookup_candidate(candidates, inner, "suffix_specific")

    return candidates


def extract_variant_info(item_en_name: str, candidate: LookupCandidate) -> Optional[VariantInfo]:
    normalized = normalize_lookup_text(item_en_name)

    bonus_match = re.match(r"^(\+\d+)\s+(.+)$", normalized)
    if bonus_match and candidate.kind in {"bonus_base", "bonus_range"}:
        return VariantInfo(kind="bonus", token=bonus_match.group(1), base_name=normalize_lookup_text(bonus_match.group(2)))

    stage_match = re.match(r"^(Slumbering|Stirring|Wakened|Wakenend|Ascendant)\s+(.+)$", normalized, flags=re.I)
    if stage_match and candidate.kind == "stage_base":
        stage = normalize_stage(stage_match.group(1))
        base_name = normalize_lookup_text(stage_match.group(2))
        extra = ""
        focus_match = re.match(r"^(Chromatic|Gem|Metallic)\s+(Dragon-Touched Focus)$", base_name, flags=re.I)
        if focus_match:
            extra = normalize_lookup_text(focus_match.group(1)).lower()
            base_name = normalize_lookup_text(focus_match.group(2))
        return VariantInfo(kind="stage", token=stage, base_name=base_name, extra=extra)

    if candidate.kind == "comma_base":
        comma_match = re.match(r"^(.+?),\s*(.+)$", normalized)
        if comma_match:
            return VariantInfo(
                kind="comma_family",
                token=normalize_lookup_text(comma_match.group(2)),
                base_name=normalize_lookup_text(comma_match.group(1)),
            )

    if candidate.kind == "parenthetical_base":
        paren_match = re.match(r"^(.+?)\s*\(([^()]+)\)$", normalized)
        if paren_match:
            return VariantInfo(
                kind="parenthetical_family",
                token=normalize_lookup_text(paren_match.group(2)),
                base_name=normalize_lookup_text(paren_match.group(1)),
            )

    return None


def candidate_match_score(candidate_value: str, entry_title_en: str, entry_slug: str) -> float:
    score = 0.0
    entry_title_en = normalize_lookup_text(entry_title_en)
    entry_slug = normalize_lookup_text(entry_slug)

    if entry_title_en:
        if titles_match(entry_title_en, candidate_value):
            return 1.0
        if english_title_key(entry_title_en) and english_title_key(entry_title_en) == english_title_key(candidate_value):
            score = max(score, 0.99)
        score = max(score, slug_similarity(candidate_value, entry_title_en))
        score = max(score, token_overlap_ratio(candidate_value, entry_title_en))

    if entry_slug:
        normalized_candidate_slug = normalize_slug_for_compare(slugify_name(candidate_value))
        normalized_entry_slug = normalize_slug_for_compare(entry_slug)
        if normalized_candidate_slug and normalized_candidate_slug == normalized_entry_slug:
            score = max(score, 0.97)
        score = max(score, slug_similarity(candidate_value, entry_slug))
        score = max(score, token_overlap_ratio(candidate_value, entry_slug))

    return score


def build_stage_variant_title(page: ItemPageData, variant: VariantInfo) -> Optional[str]:
    if not page.title_ru:
        return None

    base_name_key = canonical_key(variant.base_name)
    if base_name_key == canonical_key("Dragon-Touched Focus"):
        prefix = STAGE_TITLE_RU_FEM.get(variant.token)
        if not prefix:
            return None
        family_ru = DRAGON_TOUCHED_FOCUS_FAMILY_RU.get(variant.extra, "")
        base_title = lowercase_first(page.title_ru)
        if family_ru:
            return f"{prefix} {family_ru} {base_title}"
        return f"{prefix} {base_title}"

    prefix = STAGE_TITLE_RU_MASC.get(variant.token)
    if not prefix:
        return None
    return f"{prefix} {lowercase_first(page.title_ru)}"


def build_bonus_variant_title(page: ItemPageData, token: str) -> Optional[str]:
    if not page.title_ru:
        return None
    title = normalize_spaces(page.title_ru)
    updated = re.sub(r"\+\s*1\s*,\s*\+\s*2\s*,\s*\+\s*3", token, title)
    if updated != title:
        return normalize_spaces(updated)
    if re.search(r"\+\s*\d", title):
        return title
    return f"{title} {token}"


def build_stage_variant_description(page: ItemPageData, variant: VariantInfo) -> Optional[str]:
    base_name_key = canonical_key(variant.base_name)
    if base_name_key == canonical_key("Dragon-Touched Focus") and variant.extra:
        return None
    if base_name_key not in {
        canonical_key("Dragon Vessel"),
        canonical_key("Scaled Ornament"),
        canonical_key("Dragon-Touched Focus"),
    }:
        return None
    if not page.paragraphs:
        return None

    try:
        stage_index = STAGE_ORDER.index(variant.token)
    except ValueError:
        return None

    intro = page.paragraphs[:1]
    tier_paragraphs = page.paragraphs[1 : 2 + stage_index]
    parts = intro + tier_paragraphs
    if not parts:
        return None
    return "\n\n".join(dict.fromkeys(parts))


def build_family_variant_title(page: ItemPageData, variant: VariantInfo) -> Optional[str]:
    base_name_key = canonical_key(variant.base_name)
    token_key = normalize_lookup_text(variant.token).lower()

    if base_name_key == canonical_key("Bag of Tricks"):
        color = BAG_OF_TRICKS_COLORS_RU.get(token_key)
        if color and page.title_ru:
            return f"{color} {lowercase_first(page.title_ru)}"

    if base_name_key == canonical_key("Carpet of Flying") and page.title_ru:
        size_text = normalize_spaces(variant.token)
        size_text = re.sub(r"\s*[xх]\s*", " x ", size_text, flags=re.I)
        size_text = size_text.replace("ft.", "фт.")
        size_text = size_text.replace("Ft.", "фт.")
        return f"{page.title_ru}, {size_text}"

    if base_name_key == canonical_key("Spell Scroll") and page.title_ru:
        level_ru = SPELL_SCROLL_LEVEL_RU.get(token_key)
        if level_ru:
            return f"{page.title_ru} ({level_ru})"

    return None


def ensure_item_ru_names(obj: Any) -> None:
    if isinstance(obj, dict):
        if obj.get("resource_id") == "item" and isinstance(obj.get("stats"), dict):
            stats = obj["stats"]
            name_value = normalize_lookup_text(stats.get("name", {}).get("value", ""))
            ru_name = stats.get("ru_name")
            if isinstance(ru_name, dict):
                if "value" not in ru_name or not ru_name.get("value"):
                    ru_name["value"] = name_value
            elif name_value:
                stats["ru_name"] = {"value": name_value}
        for value in obj.values():
            ensure_item_ru_names(value)
    elif isinstance(obj, list):
        for item in obj:
            ensure_item_ru_names(item)


def collect_item_stats_blocks(obj: Any, path: str = "") -> List[ItemStatsRef]:
    found: List[ItemStatsRef] = []
    if isinstance(obj, dict):
        if obj.get("resource_id") == "item" and isinstance(obj.get("stats"), dict):
            found.append(ItemStatsRef(stats=obj["stats"], path=path))
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(collect_item_stats_blocks(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            found.extend(collect_item_stats_blocks(value, child_path))
    return found


def set_item_ru_name(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    ru_name = stats.get("ru_name")
    if isinstance(ru_name, dict):
        ru_name["value"] = value
    else:
        stats["ru_name"] = {"value": value}


def set_item_description(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    description = stats.get("description")
    if isinstance(description, dict):
        description["value"] = value
    else:
        stats["description"] = {"value": value}


class DndSuItemClient:
    def __init__(self, cache_dir: Path = CACHE_DIR, timeout: int = TIMEOUT) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
                )
            }
        )
        self._item_index: Optional[List[SiteEntry]] = None
        self._page_cache: Dict[str, ItemPageData] = {}

    def _cache_path(self, url: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", url)
        return self.cache_dir / f"{safe}.html"

    def get_html(self, url: str) -> str:
        cache_path = self._cache_path(url)
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8")
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        html_text = response.text
        cache_path.write_text(html_text, encoding="utf-8")
        time.sleep(REQUEST_PAUSE_SEC)
        return html_text

    def get_soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.get_html(url), "html.parser")

    def build_item_index(self) -> List[SiteEntry]:
        if self._item_index is not None:
            return self._item_index

        soup = self.get_soup(INDEX_URL)
        entries: List[SiteEntry] = []
        seen: set[str] = set()

        for div in soup.select("div.for_filter"):
            anchor = div.find("a", href=True)
            if anchor is None:
                continue

            href = urljoin(BASE_URL, anchor["href"])
            if "/items/" not in href or "/homebrew/" in href:
                continue
            if href in seen:
                continue
            seen.add(href)

            title_ru, title_en = split_index_search_titles(
                normalize_spaces(div.get("data-search", "")),
                normalize_spaces(anchor.get_text(" ", strip=True)),
            )
            slug = re.sub(r"^\d+-", "", href.rstrip("/").split("/")[-1])
            entries.append(SiteEntry(href=href, title_ru=title_ru, title_en=title_en, slug=slug))

        self._item_index = entries
        return entries

    def _find_exact_entry(self, candidates: Sequence[LookupCandidate]) -> Optional[Tuple[SiteEntry, LookupCandidate]]:
        entries = self.build_item_index()
        slug_candidates: Dict[str, LookupCandidate] = {}
        key_candidates: Dict[str, LookupCandidate] = {}
        for candidate in candidates:
            if candidate.value:
                slug_candidates.setdefault(slugify_name(candidate.value), candidate)
                key_candidates.setdefault(canonical_key(candidate.value), candidate)

        for entry in entries:
            if entry.slug in slug_candidates:
                return entry, slug_candidates[entry.slug]
            if entry.title_en:
                entry_key = canonical_key(entry.title_en)
                if entry_key in key_candidates:
                    return entry, key_candidates[entry_key]
        return None

    def resolve_item_page(self, item_en_name: str) -> Optional[ResolvedItemPage]:
        candidates = build_lookup_candidates(item_en_name)
        exact_match = self._find_exact_entry(candidates)
        if exact_match:
            entry, candidate = exact_match
            variant = extract_variant_info(item_en_name, candidate)
            return ResolvedItemPage(
                url=entry.href,
                expected_en_name=candidate.value,
                exact_match=candidate.kind in SPECIFIC_CANDIDATE_KINDS and variant is None,
                matched_candidate_kind=candidate.kind,
                matched_candidate_value=candidate.value,
                variant=variant,
            )

        scored: List[Tuple[float, SiteEntry, LookupCandidate]] = []
        for entry in self.build_item_index():
            best_candidate: Optional[LookupCandidate] = None
            best_score = 0.0
            for candidate in candidates:
                score = candidate_match_score(candidate.value, entry.title_en, entry.slug)
                if score > best_score:
                    best_score = score
                    best_candidate = candidate
            if best_candidate is not None and best_score >= 0.70:
                scored.append((best_score, entry, best_candidate))

        scored.sort(key=lambda item: item[0], reverse=True)
        for _, entry, candidate in scored[:20]:
            try:
                page = self.parse_item_page(entry.href, entry.title_en or candidate.value)
            except Exception:
                continue

            page_name = page.title_en or entry.title_en
            if not page_name:
                continue

            if titles_match(page_name, candidate.value) or candidate_match_score(candidate.value, page_name, entry.slug) >= 0.86:
                variant = extract_variant_info(item_en_name, candidate)
                return ResolvedItemPage(
                    url=entry.href,
                    expected_en_name=candidate.value,
                    exact_match=candidate.kind in SPECIFIC_CANDIDATE_KINDS and variant is None,
                    matched_candidate_kind=candidate.kind,
                    matched_candidate_value=candidate.value,
                    variant=variant,
                )

        return None

    def parse_item_page(self, url: str, expected_en_name: str) -> ItemPageData:
        cache_key = f"{url}::{expected_en_name}"
        cached = self._page_cache.get(cache_key)
        if cached is not None:
            return cached

        soup = self.get_soup(url)
        elements: List[Tuple[str, str]] = []
        for tag in soup.find_all(["h2", "h3", "p", "li"]):
            text = normalize_spaces(tag.get_text(" ", strip=True))
            if text:
                elements.append((tag.name.lower(), text))

        result = ItemPageData()
        start_index: Optional[int] = None

        for index, (tag_name, text) in enumerate(elements):
            if tag_name != "h2":
                continue
            match = HEADING_RE.match(text)
            if not match:
                continue

            title_en = normalize_spaces(match.group(2))
            if titles_match(title_en, expected_en_name) or candidate_match_score(expected_en_name, title_en, "") >= 0.86:
                result.title_ru = strip_brackets_title(text)
                result.title_en = title_en
                start_index = index
                break

        if start_index is None:
            raise RuntimeError(f"Не удалось найти начало страницы предмета: {url}")

        end_index = len(elements)
        for index in range(start_index + 1, len(elements)):
            tag_name, text = elements[index]
            if tag_name == "h2" and any(marker in canonical_key(text) for marker in PAGE_STOP_MARKERS):
                end_index = index
                break

        paragraph_parts: List[str] = []
        fallback_parts: List[str] = []
        for index in range(start_index + 1, end_index):
            tag_name, text = elements[index]
            if tag_name == "h3":
                break

            clean = clean_site_text(text)
            if not clean or clean.startswith("На данный момент в галерее"):
                continue

            if tag_name == "p":
                paragraph_parts.append(clean)
                continue

            if tag_name == "li":
                text_key = canonical_key(clean)
                if text_key == canonical_key("Распечатать"):
                    continue
                if any(text_key.startswith(canonical_key(prefix)) for prefix in ITEM_METADATA_PREFIXES):
                    continue
                fallback_parts.append(clean)

        parts = paragraph_parts or fallback_parts
        unique_parts = list(dict.fromkeys(parts))
        result.paragraphs = unique_parts
        if unique_parts:
            result.description = "\n\n".join(unique_parts)

        self._page_cache[cache_key] = result
        return result


def apply_variant_translation(
    stats: Dict[str, Any],
    page: ItemPageData,
    variant: VariantInfo,
) -> bool:
    applied = False

    if variant.kind == "bonus":
        title_ru = build_bonus_variant_title(page, variant.token)
        if title_ru:
            set_item_ru_name(stats, title_ru)
            applied = True

    elif variant.kind == "stage":
        title_ru = build_stage_variant_title(page, variant)
        if title_ru:
            set_item_ru_name(stats, title_ru)
            applied = True

        description_ru = build_stage_variant_description(page, variant)
        if description_ru:
            current_description = normalize_lookup_text(stats.get("description", {}).get("value", ""))
            if not current_description or looks_english(current_description):
                set_item_description(stats, description_ru)
                applied = True

    elif variant.kind in {"comma_family", "parenthetical_family"}:
        title_ru = build_family_variant_title(page, variant)
        if title_ru:
            set_item_ru_name(stats, title_ru)
            applied = True

    return applied


def apply_site_translations_to_items(
    doc: Dict[str, Any],
    path: Path,
    client: DndSuItemClient,
    missing: List[str],
) -> None:
    item_refs = collect_item_stats_blocks(doc)
    top_stats = get_top_stats(doc)

    for ref in item_refs:
        stats = ref.stats
        item_en_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
        if (not item_en_name or not looks_english(item_en_name)) and stats is top_stats:
            try:
                item_en_name = detect_item_lookup_name(path, stats)
            except Exception as exc:
                missing.append(f"item_lookup_name:{exc}")
                continue

        if not item_en_name or not looks_english(item_en_name):
            continue

        resolved = client.resolve_item_page(item_en_name)
        if resolved is None:
            if stats is top_stats:
                missing.append(f"item_page:not_found:{item_en_name}")
            continue

        try:
            page = client.parse_item_page(resolved.url, resolved.expected_en_name)
        except Exception as exc:
            if stats is top_stats:
                missing.append(f"item_page:{item_en_name}:{exc}")
            continue

        current_description = normalize_lookup_text(stats.get("description", {}).get("value", ""))
        applied = False

        if resolved.variant is not None:
            applied = apply_variant_translation(stats, page, resolved.variant)
        elif resolved.exact_match:
            if page.title_ru:
                set_item_ru_name(stats, page.title_ru)
                applied = True
            if page.description and (not current_description or looks_english(current_description)):
                set_item_description(stats, page.description)
                applied = True

        if not applied and resolved.exact_match and page.title_ru:
            set_item_ru_name(stats, page.title_ru)
            applied = True

        if not applied and stats is top_stats:
            missing.append(f"item_variant:fallback:{item_en_name}->{resolved.expected_en_name}")


def is_translatable_string_path(path: str, resource_type: Optional[str]) -> bool:
    if resource_type == "item" and path.endswith(".name.value"):
        return False
    if any(path.endswith(suffix) for suffix in IGNORE_REMAINING_ENGLISH_PATH_PARTS):
        return False
    return any(path.endswith(suffix) for suffix in TRANSLATABLE_SUFFIXES)


def collect_translatable_refs(obj: Any, path: str = "", resource_type: Optional[str] = None) -> List[StringRef]:
    refs: List[StringRef] = []
    if isinstance(obj, dict):
        current_resource_type = obj.get("resource_id", resource_type)
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(value, str):
                if is_translatable_string_path(child_path, current_resource_type) and looks_english(value):
                    refs.append(StringRef(obj, key, child_path, value))
            else:
                refs.extend(collect_translatable_refs(value, child_path, current_resource_type))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            if isinstance(value, str):
                if is_translatable_string_path(child_path, resource_type) and looks_english(value):
                    refs.append(StringRef(obj, index, child_path, value))
            else:
                refs.extend(collect_translatable_refs(value, child_path, resource_type))
    return refs


def collect_remaining_english_strings(
    obj: Any,
    path: str = "",
    resource_type: Optional[str] = None,
) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        current_resource_type = obj.get("resource_id", resource_type)
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(collect_remaining_english_strings(value, child_path, current_resource_type))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            found.extend(collect_remaining_english_strings(value, child_path, resource_type))
    elif isinstance(obj, str):
        if looks_english(obj) and is_translatable_string_path(path, resource_type):
            found.append((path, obj))
    return found


def patch_known_leaf_translations(obj: Any, resource_type: Optional[str] = None) -> None:
    if isinstance(obj, dict):
        current_resource_type = obj.get("resource_id", resource_type)
        for key, value in obj.items():
            if (
                key in {"name", "description", "options", "components", "casting_time", "range", "duration", "ru_name", "prerequisites"}
                and isinstance(value, dict)
                and isinstance(value.get("value"), str)
                and not (current_resource_type == "item" and key == "name")
            ):
                translated = translate_static_text(value["value"])
                if translated:
                    value["value"] = translated
            else:
                patch_known_leaf_translations(value, current_resource_type)
    elif isinstance(obj, list):
        for item in obj:
            patch_known_leaf_translations(item, resource_type)


def translate_remaining_strings(
    doc: Dict[str, Any],
    translator: Optional[TranslatorClient],
    missing: List[str],
) -> None:
    refs = collect_translatable_refs(doc)
    if not refs:
        return

    grouped: Dict[str, List[StringRef]] = {}
    for ref in refs:
        static_translation = translate_static_text(ref.value)
        if static_translation:
            ref.container[ref.key] = static_translation
        else:
            grouped.setdefault(normalize_lookup_text(ref.value), []).append(ref)

    if not grouped:
        return

    if not translator or not translator.enabled:
        missing.append(f"translator:disabled:{len(grouped)}")
        return

    translated_map = translator.translate_many(list(grouped.keys()))
    for source_text, ref_list in grouped.items():
        translated = translated_map.get(source_text, source_text)
        if translated and isinstance(translated, str) and translated.strip():
            translated = normalize_spaces(translated)
            for ref in ref_list:
                current_value = ref.container[ref.key]
                if isinstance(current_value, str) and looks_english(current_value):
                    ref.container[ref.key] = translated


def translate_file(
    path: Path,
    out_path: Path,
    client: DndSuItemClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    missing: List[str] = []

    ensure_item_ru_names(doc)
    apply_site_translations_to_items(doc, path, client, missing)
    patch_known_leaf_translations(doc)
    translate_remaining_strings(doc, translator, missing)

    remaining = collect_remaining_english_strings(doc)
    save_json(out_path, doc)
    return missing, remaining


def expand_input_patterns(patterns: Iterable[str]) -> List[Path]:
    paths: List[Path] = []
    for pattern in patterns:
        matches = [Path(match) for match in glob.glob(pattern)]
        if matches:
            paths.extend(matches)
        else:
            maybe_path = Path(pattern)
            if maybe_path.exists():
                paths.append(maybe_path)

    unique: List[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Переносит переводы предметов из dnd.su в item_*.rpg.json")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./item_*.rpg.json")
    parser.add_argument("--out-dir", default="translated", help="Куда сохранить результат")
    parser.add_argument("--in-place", action="store_true", help="Перезаписать исходные файлы")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR), help="Каталог кеша HTML")
    parser.add_argument("--translator-url", default=DEFAULT_TRANSLATOR_URL, help="URL fallback-переводчика")
    parser.add_argument("--translator-timeout", type=int, default=DEFAULT_TRANSLATOR_TIMEOUT, help="Таймаут fallback-переводчика")
    parser.add_argument("--translator-batch-size", type=int, default=5, help="Размер батча для fallback-переводчика")
    parser.add_argument("--no-translator", action="store_true", help="Отключить fallback-переводчик")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_files = expand_input_patterns(args.inputs)
    if not input_files:
        log("Не найдено ни одного входного файла.")
        return 2

    client = DndSuItemClient(cache_dir=Path(args.cache_dir))
    translator = None if args.no_translator else TranslatorClient(
        url=args.translator_url,
        timeout=args.translator_timeout,
        batch_size=args.translator_batch_size,
        enabled=not args.no_translator,
    )

    out_dir = Path(args.out_dir)
    if not args.in_place:
        out_dir.mkdir(parents=True, exist_ok=True)

    had_errors = False
    total_remaining: Dict[str, List[Tuple[str, str]]] = {}

    for path in input_files:
        try:
            out_path = path if args.in_place else out_dir / path.name
            missing, remaining = translate_file(path, out_path, client, translator)
            log(f"[OK] {path.name} -> {out_path}")
            if missing:
                log(f"  Не всё удалось привязать автоматически ({len(missing)}):")
                for item in missing[:30]:
                    log(f"    - {item}")
            if remaining:
                total_remaining[path.name] = remaining
        except Exception as exc:
            had_errors = True
            log(f"[ERR] {path.name}: {exc}")

    if total_remaining:
        log("\nОстались строки с английским текстом, которые скрипт не смог уверенно перевести:")
        for filename, items in total_remaining.items():
            log(f"  {filename}:")
            for item_path, value in items[:30]:
                log(f"    - {item_path} = {value}")
            if len(items) > 30:
                log(f"    ... и ещё {len(items) - 30}")

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
