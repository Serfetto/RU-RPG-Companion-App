#!/usr/bin/env python3
"""
Translate weapon JSON resources using dnd.su first and a fallback translator
for anything that cannot be sourced from the site.

Notes:
- works with standalone weapon_*.rpg.json files;
- also works with any nested `resource_id == "weapon"` blocks inside other JSON;
- keeps `stats.name.value` untouched and writes the Russian title to
  `stats.ru_name.value`;
- uses the PHB arms article for base weapons, item pages for magic weapons,
  and Yandex site-search on dnd.su as an extra fallback before the translator;
- also reuses the spell translator for any nested `resource_id == "spell"`
  blocks found inside weapons.
"""

from __future__ import annotations

import argparse
import copy
import glob
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urljoin, urlparse

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
    load_json,
    log,
    looks_english,
    normalize_lookup_text,
    normalize_spaces,
    save_json,
    translate_static_text as translate_feat_static_text,
)
from dndsu_item_json_translator import (
    DndSuItemClient,
    ItemPageData,
    VariantInfo,
    build_bonus_variant_title,
    build_family_variant_title,
    build_lookup_candidates,
    build_stage_variant_description,
    build_stage_variant_title,
    candidate_match_score,
    extract_variant_info,
)
from dndsu_spell_json_translator import (
    DndSuSpellClient,
    apply_site_translations_to_spells,
    ensure_spell_ru_names,
    translate_static_text as translate_spell_static_text,
)

BASE_URL = "https://dnd.su"
ARMS_URL = urljoin(BASE_URL, "/articles/inventory/96-arms/")
SITE_SEARCH_URL = "https://yandex.ru/search/site/"
SITE_SEARCH_ID = "2294916"
CACHE_DIR = Path(".dndsu_cache") / "weapons"


@dataclass
class WeaponStatsRef:
    stats: Dict[str, Any]
    path: str


@dataclass
class ArmsWeaponEntry:
    slug: str
    title_ru: str


@dataclass
class SearchResultEntry:
    href: str
    title: str


@dataclass
class ResolvedWeaponPage:
    source_kind: str
    expected_en_name: str
    title_ru: str = ""
    url: str = ""
    exact_match: bool = True
    matched_candidate_kind: str = "original"
    matched_candidate_value: str = ""
    variant: Optional[VariantInfo] = None


def get_top_stats(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc.get("stats", {})


def translate_static_text(text: str) -> Optional[str]:
    return translate_spell_static_text(text) or translate_feat_static_text(text)


def looks_translatable_english(text: str) -> bool:
    normalized = normalize_lookup_text(text or "")
    if not normalized:
        return False
    scrubbed = re.sub(r"\b[dk]\d+\b", " ", normalized, flags=re.I)
    scrubbed = re.sub(r"\b[A-Za-z]{1,2}\b", " ", scrubbed)
    return bool(re.search(r"[A-Za-z]{3,}", scrubbed))


def detect_weapon_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    candidates: List[str] = []

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("weapon_"):
        stem = stem[len("weapon_") :]
    candidates.append(stem.replace("_", " "))
    candidates.append(stem.replace("-", " "))

    stats_id = normalize_lookup_text(stats.get("id", ""))
    if stats_id:
        candidates.append(stats_id.replace("_", " "))

    seen: set[str] = set()
    for candidate in candidates:
        candidate = normalize_lookup_text(candidate)
        key = candidate.casefold()
        if not candidate or key in seen:
            continue
        seen.add(key)
        if looks_english(candidate):
            return candidate

    raise RuntimeError(f"Could not infer an English weapon name from {path.name!r}")


def ensure_weapon_ru_names(obj: Any) -> None:
    if isinstance(obj, dict):
        if obj.get("resource_id") == "weapon" and isinstance(obj.get("stats"), dict):
            stats = obj["stats"]
            name_value = normalize_lookup_text(stats.get("name", {}).get("value", ""))
            ru_name = stats.get("ru_name")
            if isinstance(ru_name, dict):
                if "value" not in ru_name or not ru_name.get("value"):
                    ru_name["value"] = name_value
            elif name_value:
                stats["ru_name"] = {"value": name_value}
        for value in obj.values():
            ensure_weapon_ru_names(value)
    elif isinstance(obj, list):
        for item in obj:
            ensure_weapon_ru_names(item)


def collect_weapon_stats_blocks(obj: Any, path: str = "") -> List[WeaponStatsRef]:
    found: List[WeaponStatsRef] = []
    if isinstance(obj, dict):
        if obj.get("resource_id") == "weapon" and isinstance(obj.get("stats"), dict):
            found.append(WeaponStatsRef(stats=obj["stats"], path=path))
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(collect_weapon_stats_blocks(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            found.extend(collect_weapon_stats_blocks(value, child_path))
    return found


def set_weapon_ru_name(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    normalized = normalize_lookup_text(value)
    ru_name = stats.get("ru_name")
    if isinstance(ru_name, dict):
        ru_name["value"] = normalized
    else:
        stats["ru_name"] = {"value": normalized}


def set_weapon_description(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    description = stats.get("description")
    if isinstance(description, dict):
        description["value"] = value
    elif "description" in stats:
        stats["description"] = {"value": value}


def build_arms_variant_title(title_ru: str, variant: Optional[VariantInfo]) -> Optional[str]:
    title_ru = normalize_lookup_text(title_ru)
    if not title_ru:
        return None
    if variant is None:
        return title_ru
    if variant.kind == "bonus":
        return f"{title_ru} {variant.token}"
    if variant.kind == "comma_family":
        return f"{title_ru}, {normalize_lookup_text(variant.token)}"
    if variant.kind == "parenthetical_family":
        return f"{title_ru} ({normalize_lookup_text(variant.token)})"
    return None


def apply_weapon_variant_translation(
    stats: Dict[str, Any],
    page: ItemPageData,
    variant: VariantInfo,
) -> bool:
    applied = False

    if variant.kind == "bonus":
        title_ru = build_bonus_variant_title(page, variant.token)
        if title_ru:
            set_weapon_ru_name(stats, title_ru)
            applied = True

    elif variant.kind == "stage":
        title_ru = build_stage_variant_title(page, variant)
        if title_ru:
            set_weapon_ru_name(stats, title_ru)
            applied = True

        description_ru = build_stage_variant_description(page, variant)
        if description_ru:
            current_description = normalize_lookup_text(stats.get("description", {}).get("value", ""))
            if not current_description or looks_translatable_english(current_description):
                set_weapon_description(stats, description_ru)
                applied = True

    elif variant.kind in {"comma_family", "parenthetical_family"}:
        title_ru = build_family_variant_title(page, variant)
        if title_ru:
            set_weapon_ru_name(stats, title_ru)
            applied = True

    return applied


def russian_charge_word(value: str) -> str:
    try:
        count = abs(int(value))
    except Exception:
        return "зарядов"
    if count % 10 == 1 and count % 100 != 11:
        return "заряд"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "заряда"
    return "зарядов"


def patch_effect_display_names(obj: Any) -> None:
    if isinstance(obj, dict):
        if obj.get("resource_id") == "effect" and isinstance(obj.get("stats"), dict):
            stats = obj["stats"]
            name_obj = stats.get("name")
            if isinstance(name_obj, dict) and isinstance(name_obj.get("value"), str):
                current_name = normalize_lookup_text(name_obj["value"])
                if current_name == "Charges":
                    name_obj["value"] = "Заряды"
                else:
                    cast_match = re.fullmatch(r"Cast\s+(.+?)\s+\((\d+)\s+charges?\)", current_name, flags=re.I)
                    if cast_match:
                        spell_stats = (
                            stats.get("spell", {})
                            .get("value", {})
                            .get("stats", {})
                        )
                        spell_display = normalize_lookup_text(
                            spell_stats.get("ru_name", {}).get("value", "")
                            or spell_stats.get("name", {}).get("value", "")
                        )
                        if spell_display and not looks_english(spell_display):
                            charges = cast_match.group(2)
                            name_obj["value"] = f"Наложить {spell_display} ({charges} {russian_charge_word(charges)})"
        for value in obj.values():
            patch_effect_display_names(value)
    elif isinstance(obj, list):
        for item in obj:
            patch_effect_display_names(item)


class DndSuWeaponClient:
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
        self.item_client = DndSuItemClient(cache_dir=self.cache_dir / "items", timeout=timeout)
        self._arms_index: Optional[List[ArmsWeaponEntry]] = None
        self._search_cache: Dict[str, List[SearchResultEntry]] = {}

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

    def build_arms_index(self) -> List[ArmsWeaponEntry]:
        if self._arms_index is not None:
            return self._arms_index

        soup = self.get_soup(ARMS_URL)
        entries: List[ArmsWeaponEntry] = []
        seen: set[str] = set()
        for span in soup.select("span[id^='weapon.']"):
            weapon_id = normalize_lookup_text(span.get("id", ""))
            if "." not in weapon_id:
                continue
            slug = weapon_id.split(".", 1)[1]
            title_ru = normalize_lookup_text(span.get_text(" ", strip=True))
            if not slug or not title_ru or slug in seen:
                continue
            seen.add(slug)
            entries.append(ArmsWeaponEntry(slug=slug, title_ru=title_ru))

        self._arms_index = entries
        return entries

    def _find_arms_entry(self, candidates: Sequence[Any]) -> Optional[Tuple[ArmsWeaponEntry, Any]]:
        best_score = 0.0
        best_match: Optional[Tuple[ArmsWeaponEntry, Any]] = None
        for entry in self.build_arms_index():
            for candidate in candidates:
                score = candidate_match_score(candidate.value, "", entry.slug)
                if score > best_score:
                    best_score = score
                    best_match = (entry, candidate)
        if best_match is not None and best_score >= 0.92:
            return best_match
        return None

    def search_site(self, query: str) -> List[SearchResultEntry]:
        normalized_query = normalize_lookup_text(query)
        if not normalized_query:
            return []
        cache_key = normalized_query.casefold()
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached

        url = f"{SITE_SEARCH_URL}?{urlencode({'searchid': SITE_SEARCH_ID, 'web': '0', 'l10n': 'ru', 'reqenc': 'utf-8', 'text': normalized_query})}"
        soup = self.get_soup(url)
        results: List[SearchResultEntry] = []
        seen: set[str] = set()
        for anchor in soup.select("a.b-serp-item__title-link[href]"):
            href = normalize_lookup_text(anchor.get("href", ""))
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.netloc and "dnd.su" not in parsed.netloc:
                continue
            if "/homebrew/" in href:
                continue
            if not ("/items/" in href or href.rstrip("/") == ARMS_URL.rstrip("/")):
                continue
            href = href.split("#", 1)[0]
            if href in seen:
                continue
            seen.add(href)
            title = normalize_lookup_text(anchor.get_text(" ", strip=True))
            results.append(SearchResultEntry(href=href, title=title))

        self._search_cache[cache_key] = results
        return results

    def _wrap_item_resolution(self, resolved: Any) -> ResolvedWeaponPage:
        return ResolvedWeaponPage(
            source_kind="item",
            url=resolved.url,
            expected_en_name=resolved.expected_en_name,
            exact_match=resolved.exact_match,
            matched_candidate_kind=resolved.matched_candidate_kind,
            matched_candidate_value=resolved.matched_candidate_value,
            variant=resolved.variant,
        )

    def resolve_weapon_page(self, weapon_en_name: str) -> Optional[ResolvedWeaponPage]:
        candidates = build_lookup_candidates(weapon_en_name)

        arms_match = self._find_arms_entry(candidates)
        if arms_match:
            entry, candidate = arms_match
            variant = extract_variant_info(weapon_en_name, candidate)
            return ResolvedWeaponPage(
                source_kind="arms",
                url=ARMS_URL,
                expected_en_name=candidate.value,
                title_ru=entry.title_ru,
                exact_match=variant is None and candidate.kind == "original",
                matched_candidate_kind=candidate.kind,
                matched_candidate_value=candidate.value,
                variant=variant,
            )

        item_resolved = self.item_client.resolve_item_page(weapon_en_name)
        if item_resolved is not None:
            return self._wrap_item_resolution(item_resolved)

        seen_queries: set[str] = set()
        for candidate in candidates:
            query_key = normalize_lookup_text(candidate.value).casefold()
            if not query_key or query_key in seen_queries:
                continue
            seen_queries.add(query_key)

            for result in self.search_site(candidate.value):
                if result.href.rstrip("/") == ARMS_URL.rstrip("/"):
                    variant = extract_variant_info(weapon_en_name, candidate)
                    arms_match = self._find_arms_entry([candidate])
                    if arms_match:
                        entry, _ = arms_match
                        return ResolvedWeaponPage(
                            source_kind="arms",
                            url=ARMS_URL,
                            expected_en_name=candidate.value,
                            title_ru=entry.title_ru,
                            exact_match=False,
                            matched_candidate_kind=candidate.kind,
                            matched_candidate_value=candidate.value,
                            variant=variant,
                        )
                    continue

                try:
                    page = self.item_client.parse_item_page(result.href, candidate.value)
                except Exception:
                    continue

                page_name = normalize_lookup_text(page.title_en or candidate.value)
                if (
                    page_name
                    and (
                        candidate_match_score(candidate.value, page_name, result.href.rstrip("/").split("/")[-1]) >= 0.86
                    )
                ):
                    return ResolvedWeaponPage(
                        source_kind="search",
                        url=result.href,
                        expected_en_name=candidate.value,
                        exact_match=False,
                        matched_candidate_kind=candidate.kind,
                        matched_candidate_value=candidate.value,
                        variant=extract_variant_info(weapon_en_name, candidate),
                    )

        return None


def apply_site_translations_to_weapons(
    doc: Dict[str, Any],
    path: Path,
    client: DndSuWeaponClient,
    missing: List[str],
) -> None:
    weapon_refs = collect_weapon_stats_blocks(doc)
    top_stats = get_top_stats(doc)

    for ref in weapon_refs:
        stats = ref.stats
        weapon_en_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
        if not weapon_en_name or not looks_english(weapon_en_name):
            if stats is top_stats:
                try:
                    weapon_en_name = detect_weapon_lookup_name(path, stats)
                except Exception as exc:
                    missing.append(f"weapon_lookup_name:{exc}")
                    continue
            else:
                continue

        resolved = client.resolve_weapon_page(weapon_en_name)
        if resolved is None:
            if stats is top_stats:
                missing.append(f"weapon_page:not_found:{weapon_en_name}")
            continue

        if resolved.source_kind == "arms":
            title_ru = build_arms_variant_title(resolved.title_ru, resolved.variant) or resolved.title_ru
            if title_ru:
                set_weapon_ru_name(stats, title_ru)
            continue

        try:
            page = client.item_client.parse_item_page(resolved.url, resolved.expected_en_name)
        except Exception as exc:
            if stats is top_stats:
                missing.append(f"weapon_page:{weapon_en_name}:{exc}")
            continue

        current_description = normalize_lookup_text(stats.get("description", {}).get("value", ""))
        applied = False

        if resolved.variant is not None:
            applied = apply_weapon_variant_translation(stats, page, resolved.variant)
        elif page.title_ru:
            set_weapon_ru_name(stats, page.title_ru)
            applied = True
            if page.description and (not current_description or looks_translatable_english(current_description)):
                set_weapon_description(stats, page.description)
                applied = True

        if not applied and page.title_ru:
            set_weapon_ru_name(stats, page.title_ru)
            applied = True

        if not applied and stats is top_stats:
            missing.append(f"weapon_variant:fallback:{weapon_en_name}->{resolved.expected_en_name}")


def is_translatable_string_path(path: str, resource_type: Optional[str]) -> bool:
    if resource_type in {"weapon", "spell", "item"} and path.endswith(".name.value"):
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
                if is_translatable_string_path(child_path, current_resource_type) and looks_translatable_english(value):
                    refs.append(StringRef(obj, key, child_path, value))
            else:
                refs.extend(collect_translatable_refs(value, child_path, current_resource_type))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            if isinstance(value, str):
                if is_translatable_string_path(child_path, resource_type) and looks_translatable_english(value):
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
        if looks_translatable_english(obj) and is_translatable_string_path(path, resource_type):
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
                and not (current_resource_type in {"weapon", "spell", "item"} and key == "name")
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
                if isinstance(current_value, str) and looks_translatable_english(current_value):
                    ref.container[ref.key] = translated


def translate_file(
    path: Path,
    out_path: Path,
    weapon_client: DndSuWeaponClient,
    spell_client: DndSuSpellClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    missing: List[str] = []

    ensure_weapon_ru_names(doc)
    ensure_spell_ru_names(doc)
    apply_site_translations_to_weapons(doc, path, weapon_client, missing)
    apply_site_translations_to_spells(doc, path, spell_client, missing)
    patch_effect_display_names(doc)
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
    parser = argparse.ArgumentParser(description="Translate weapon_*.rpg.json files using dnd.su")
    parser.add_argument("inputs", nargs="+", help="Files or glob patterns, for example ./weapon_*.rpg.json")
    parser.add_argument("--out-dir", default="translated", help="Output directory when not using --in-place")
    parser.add_argument("--in-place", action="store_true", help="Overwrite source files")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR), help="Directory for cached HTML pages")
    parser.add_argument("--translator-url", default=DEFAULT_TRANSLATOR_URL, help="Fallback translator URL")
    parser.add_argument("--translator-timeout", type=int, default=DEFAULT_TRANSLATOR_TIMEOUT, help="Fallback translator timeout")
    parser.add_argument("--translator-batch-size", type=int, default=5, help="Fallback translator batch size")
    parser.add_argument("--no-translator", action="store_true", help="Disable the fallback translator")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = expand_input_patterns(args.inputs)
    if not inputs:
        raise SystemExit("No input files matched the provided patterns")

    cache_dir = Path(args.cache_dir)
    weapon_client = DndSuWeaponClient(cache_dir=cache_dir, timeout=TIMEOUT)
    spell_client = DndSuSpellClient(cache_dir=cache_dir / "spells", timeout=TIMEOUT)
    translator = TranslatorClient(
        url=args.translator_url,
        timeout=args.translator_timeout,
        batch_size=args.translator_batch_size,
        enabled=not args.no_translator,
    )

    out_dir = Path(args.out_dir)
    if not args.in_place:
        out_dir.mkdir(parents=True, exist_ok=True)

    total_remaining = 0
    for path in inputs:
        out_path = path if args.in_place else out_dir / path.name
        missing, remaining = translate_file(path, out_path, weapon_client, spell_client, translator)
        total_remaining += len(remaining)
        log(f"[weapon] {path} -> {out_path}")
        if missing:
            for item in missing:
                log(f"  missing: {item}")
        if remaining:
            log(f"  remaining English strings: {len(remaining)}")

    log(f"[weapon] Done. Remaining English strings across all files: {total_remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
