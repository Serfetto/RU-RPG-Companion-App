#!/usr/bin/env python3
"""
Translate background_*.rpg.json resources using dnd.su background pages first
and a fallback translator for anything that cannot be sourced from the site.

Notes:
- direct background pages are preferred when they exist;
- some backgrounds on dnd.su exist only as variants inside another page
  (for example Entertainer -> Gladiator, Noble -> Knight, Criminal -> Spy);
- for such variant-only pages the script can still reuse the parent page to
  seed the Russian background name and, when it is safe, the alternative
  feature text.
"""

from __future__ import annotations

import argparse
import copy
import glob
import html
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://dnd.su"
APP_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = APP_ROOT / ".dndsu_cache" / "backgrounds"
TIMEOUT = 240
DEFAULT_TRANSLATOR_TIMEOUT = 240
REQUEST_PAUSE_SEC = 0.2
TRANSLATOR_RETRY_ATTEMPTS = 5
TRANSLATOR_RETRY_DELAY_SEC = 2.0
DEFAULT_TRANSLATOR_URL = "http://localhost:8000/chat"

IGNORE_REMAINING_ENGLISH_PATH_PARTS = {
    ".id",
    ".resource_id",
    ".source.value",
    ".type.value",
    ".attack_ability.value",
    ".damage_type.value",
    ".trigger_type.value",
    ".charge_type.value",
    ".school.value",
    ".level.value",
    ".unit.value",
    ".rarity",
}

TRANSLATABLE_SUFFIXES = (
    ".name.value",
    ".description.value",
    ".higher_level_description.value",
    ".ru_name.value",
    ".options.value",
    ".components.value",
    ".casting_time.value",
    ".range.value",
    ".duration.value",
    ".prerequisites",
    ".prerequisites.value",
)

PAGE_STOP_MARKERS = ("комментарии", "галерея")
VARIANT_HEADING_RE = re.compile(r"^разновидность [^:]+:\s*(.+)$", re.I)
FEATURE_HEADING_RE = re.compile(r"^(?:альтернативное )?умение:\s*(.+)$", re.I)
DIRECT_BACKGROUND_URL_RE = re.compile(r"/backgrounds/\d+-")
BACKGROUND_NAME_PATH_RE = re.compile(r"^stats\.name\.value$")

STATIC_TEXT_MAP: Dict[str, str] = {
    "Touch": "Касание",
    "Self": "На себя",
    "1 action": "1 действие",
    "1 bonus action": "1 бонусное действие",
    "1 reaction": "1 реакция",
    "1 minute": "1 минута",
    "10 minutes": "10 минут",
    "1 hour": "1 час",
    "8 hours": "8 часов",
    "Instantaneous": "Мгновенная",
    "Concentration, up to 1 minute": "Концентрация, вплоть до 1 минуты",
    "Concentration, up to 10 minutes": "Концентрация, вплоть до 10 минут",
    "Concentration, up to 1 hour": "Концентрация, вплоть до 1 часа",
    "Up to 1 hour": "Вплоть до 1 часа",
    "10 feet": "10 футов",
    "30 feet": "30 футов",
    "60 feet": "60 футов",
    "120 feet": "120 футов",
    "Uses": "Использования",
}


@dataclass
class StringRef:
    container: Any
    key: Any
    path: str
    value: str


@dataclass
class BackgroundFeature:
    title_ru: str
    description: str


@dataclass
class BackgroundVariant:
    title_ru: str
    description: str = ""
    features: List[BackgroundFeature] = field(default_factory=list)


@dataclass
class BackgroundPageData:
    title_ru: str = ""
    features: List[BackgroundFeature] = field(default_factory=list)
    variants: List[BackgroundVariant] = field(default_factory=list)


@dataclass(frozen=True)
class VariantHint:
    parent_lookup_name: str
    ru_title: str
    allow_feature_sync: bool = False


@dataclass
class ResolvedBackgroundPage:
    url: str
    expected_en_name: str
    exact_match: bool = True
    variant_title_ru: Optional[str] = None
    allow_feature_sync: bool = True


VARIANT_HINTS: Dict[str, VariantHint] = {
    "gladiator": VariantHint(parent_lookup_name="Entertainer", ru_title="Гладиатор", allow_feature_sync=False),
    "knight": VariantHint(parent_lookup_name="Noble", ru_title="Рыцарь", allow_feature_sync=True),
    "spy": VariantHint(parent_lookup_name="Criminal", ru_title="Шпион", allow_feature_sync=False),
}


def log(message: str) -> None:
    print(message, file=sys.stderr)


def normalize_spaces(text: str) -> str:
    text = str(text)
    text = text.replace("\xa0", " ")
    text = text.replace("\u2009", " ")
    text = text.replace("\u202f", " ")
    text = text.replace("\u200b", "")
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_lookup_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n")
    text = text.replace("'\\''", "'")
    text = text.replace("\\'", "'")
    for bad in ("вЂ™", "вЂ", "Вґ", "`", "К»", "Кј", "вЂІ", "К№"):
        text = text.replace(bad, "'")
    for bad in ("вЂњ", "вЂќ", "вЂћ"):
        text = text.replace(bad, '"')
    return normalize_spaces(text)


def canonical_key(text: str) -> str:
    text = normalize_lookup_text(text)
    text = text.replace("ё", "е")
    text = re.sub(r"[^\w\s'-]+", " ", text, flags=re.U)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .-_\n\t").casefold()


def titles_match(left: str, right: str) -> bool:
    return canonical_key(left) == canonical_key(right)


def looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_lookup_text(text or "")))


def slugify_name(name: str) -> str:
    name = normalize_lookup_text(name).lower().strip()
    name = name.replace("'", "")
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def strip_brackets_title(text: str) -> str:
    text = normalize_spaces(text)
    text = re.sub(r"\s*\[[^\]]+\]\s*[A-Z0-9'’.-]*\s*$", "", text)
    match = re.search(r"\s*\(([^()]+)\)\s*$", text)
    if match:
        marker = normalize_spaces(match.group(1))
        upper_count = sum(1 for ch in marker if ch.isupper())
        if upper_count >= 2 and " " not in marker:
            text = text[: match.start()].strip()
    return text.strip(" -—")


def text_contains_alias(text: str, alias: str) -> bool:
    return canonical_key(alias) in canonical_key(text)


def choose_translation_placeholder(text: str) -> Optional[str]:
    match = re.fullmatch(r"Choose\s+(\d+)", normalize_lookup_text(text).strip(), flags=re.I)
    if match:
        return f"Выберите {match.group(1)}"
    return None


def translate_static_text(text: str) -> Optional[str]:
    norm = normalize_lookup_text(text)
    if norm in STATIC_TEXT_MAP:
        return STATIC_TEXT_MAP[norm]
    placeholder = choose_translation_placeholder(norm)
    if placeholder:
        return placeholder
    return None


def looks_all_caps(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    upper = [char for char in letters if char.isupper()]
    return len(upper) / len(letters) >= 0.8


def prettify_heading_title(text: str) -> str:
    text = normalize_spaces(text)
    if looks_all_caps(text):
        lowered = text.lower()
        return lowered[:1].upper() + lowered[1:]
    return text


def clean_site_text(text: str) -> str:
    text = normalize_lookup_text(text)
    text = re.sub(r"\s*\[([^\]]*[A-Za-z][^\]]*)\]", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return normalize_spaces(text)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def get_top_stats(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc.get("stats", {})


def detect_background_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("background_"):
        stem = stem[len("background_") :]

    candidates = [
        stem.replace("_", " "),
        stem.replace("-", " "),
        re.sub(r"[()]+", " ", stem).replace("_", " "),
    ]

    seen: set[str] = set()
    for candidate in candidates:
        candidate = normalize_lookup_text(candidate)
        key = canonical_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if looks_english(candidate):
            return candidate

    raise RuntimeError(f"Не удалось восстановить английское имя предыстории из файла {path.name!r}")


class TranslatorClient:
    def __init__(
        self,
        url: str,
        timeout: int = DEFAULT_TRANSLATOR_TIMEOUT,
        batch_size: int = 100,
        enabled: bool = True,
        retry_attempts: int = TRANSLATOR_RETRY_ATTEMPTS,
        retry_delay_sec: float = TRANSLATOR_RETRY_DELAY_SEC,
    ) -> None:
        self.url = url.strip()
        self.timeout = timeout
        self.batch_size = batch_size
        self.enabled = enabled and bool(self.url)
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay_sec = max(0.0, retry_delay_sec)
        self._cache: Dict[str, str] = {}
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
                ),
            }
        )
        return session

    def _reset_session(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._build_session()

    def translate_many(self, texts: Sequence[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        normalized_pairs = [(text, normalize_lookup_text(text)) for text in texts]
        pending = list(dict.fromkeys(norm for _, norm in normalized_pairs if norm and norm not in self._cache))

        for index in range(0, len(pending), self.batch_size):
            batch = pending[index : index + self.batch_size]
            translated_batch = self._translate_batch(batch)
            for source_text, translated in zip(batch, translated_batch):
                final = normalize_spaces(translated) if isinstance(translated, str) and translated.strip() else source_text
                self._cache[source_text] = final
            time.sleep(REQUEST_PAUSE_SEC)

        for original, normalized in normalized_pairs:
            result[original] = self._cache.get(normalized, original)
        return result

    def _translate_batch(self, texts: Sequence[str]) -> List[str]:
        if not self.enabled:
            return list(texts)
        return self._translate_batch_resilient(list(texts))

    def _translate_batch_resilient(self, texts: List[str]) -> List[str]:
        payload = {"texts": texts}
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = self.session.post(self.url, json=payload, timeout=(10, self.timeout))
                response.raise_for_status()
                data = response.json()
                translations = self._extract_translations(data, expected_count=len(texts))
                if len(translations) != len(texts):
                    raise RuntimeError(f"Переводчик вернул {len(translations)} переводов вместо {len(texts)}")
                return [
                    normalize_spaces(str(translation).replace("**", ""))
                    if isinstance(translation, str) and translation.strip()
                    else source_text
                    for source_text, translation in zip(texts, translations)
                ]
            except Exception as exc:
                if self._is_connection_refused(exc):
                    log(f"[translator] unreachable, fallback to source text for batch of {len(texts)} items: {exc}")
                    return list(texts)
                if self._should_retry(exc) and attempt < self.retry_attempts:
                    log(f"[translator] retry {attempt}/{self.retry_attempts - 1} after error: {exc}")
                    self._reset_session()
                    time.sleep(self.retry_delay_sec * attempt)
                    continue
                log(f"[translator] fallback to source text for batch of {len(texts)} items: {exc}")
                return list(texts)
        return list(texts)

    @staticmethod
    def _should_retry(exc: Exception) -> bool:
        if isinstance(exc, requests.exceptions.HTTPError):
            status_code = exc.response.status_code if exc.response is not None else None
            return status_code is None or status_code == 429 or status_code >= 500
        return isinstance(exc, (requests.exceptions.RequestException, json.JSONDecodeError, RuntimeError))

    @staticmethod
    def _is_connection_refused(exc: Exception) -> bool:
        if not isinstance(exc, requests.exceptions.ConnectionError):
            return False
        message = str(exc).lower()
        return (
            "connection refused" in message
            or "actively refused" in message
            or "подключение не установлено" in message
            or "failed to establish a new connection" in message
        )

    @staticmethod
    def _extract_translation(data: Any) -> str:
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("text", "translation", "result"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            texts = data.get("texts")
            if isinstance(texts, list) and len(texts) == 1:
                return str(texts[0])
            nested = data.get("data")
            if isinstance(nested, dict):
                return TranslatorClient._extract_translation(nested)
        raise RuntimeError(f"Неожиданный формат ответа переводчика: {data!r}")

    @staticmethod
    def _extract_translations(data: Any, expected_count: int) -> List[str]:
        if isinstance(data, dict):
            texts = data.get("texts")
            if isinstance(texts, list):
                return [str(item) for item in texts]
            for key in ("translations", "results"):
                values = data.get(key)
                if isinstance(values, list):
                    return [str(item) for item in values]
            nested = data.get("data")
            if isinstance(nested, dict):
                return TranslatorClient._extract_translations(nested, expected_count=expected_count)
        if expected_count == 1:
            return [TranslatorClient._extract_translation(data)]
        raise RuntimeError(f"Неожиданный формат batch-ответа переводчика: {data!r}")


class DndSuBackgroundClient:
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
        self._background_index: Optional[List[Dict[str, str]]] = None
        self._page_cache: Dict[str, BackgroundPageData] = {}

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

    def build_background_index(self) -> List[Dict[str, str]]:
        if self._background_index is not None:
            return self._background_index

        soup = self.get_soup(urljoin(BASE_URL, "/backgrounds/"))
        entries: List[Dict[str, str]] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, anchor["href"])
            if not href.startswith(BASE_URL):
                continue
            if not DIRECT_BACKGROUND_URL_RE.search(href):
                continue
            if "/homebrew/" in href or "next.dnd.su" in href:
                continue
            if href in seen:
                continue
            seen.add(href)

            last = href.rstrip("/").split("/")[-1]
            match = re.match(r"^\d+-(.+)$", last)
            if not match:
                continue

            text = normalize_spaces(anchor.get_text(" ", strip=True))
            if not text:
                continue

            entries.append(
                {
                    "href": href,
                    "text": text,
                    "slug": match.group(1),
                }
            )

        self._background_index = entries
        return entries

    def resolve_background_page(self, background_en_name: str) -> Optional[ResolvedBackgroundPage]:
        expected_slug = slugify_name(background_en_name)
        entries = self.build_background_index()

        exact_candidates = [entry for entry in entries if entry["slug"] == expected_slug]
        if exact_candidates:
            return ResolvedBackgroundPage(
                url=exact_candidates[0]["href"],
                expected_en_name=background_en_name,
                exact_match=True,
                allow_feature_sync=True,
            )

        alias_candidates = [entry for entry in entries if text_contains_alias(entry["text"], background_en_name)]
        if alias_candidates:
            return ResolvedBackgroundPage(
                url=alias_candidates[0]["href"],
                expected_en_name=background_en_name,
                exact_match=True,
                allow_feature_sync=True,
            )

        hint = VARIANT_HINTS.get(expected_slug)
        if hint:
            parent_slug = slugify_name(hint.parent_lookup_name)
            parent_candidates = [entry for entry in entries if entry["slug"] == parent_slug]
            if parent_candidates:
                return ResolvedBackgroundPage(
                    url=parent_candidates[0]["href"],
                    expected_en_name=hint.parent_lookup_name,
                    exact_match=False,
                    variant_title_ru=hint.ru_title,
                    allow_feature_sync=hint.allow_feature_sync,
                )
        return None

    def parse_background_page(self, url: str, expected_en_name: str) -> BackgroundPageData:
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

        start_index: Optional[int] = None
        expected_lower = expected_en_name.lower()
        result = BackgroundPageData()

        for index, (tag_name, text) in enumerate(elements):
            if tag_name != "h2":
                continue
            lower = text.lower()
            if f"[{expected_lower}]" in lower or canonical_key(expected_en_name) in canonical_key(text):
                start_index = index
                result.title_ru = strip_brackets_title(text)
                break

        if start_index is None:
            raise RuntimeError(f"Не удалось найти начало страницы предыстории {expected_en_name!r}")

        end_index = len(elements)
        for index in range(start_index + 1, len(elements)):
            tag_name, text = elements[index]
            if tag_name == "h2" and any(marker in text.lower() for marker in PAGE_STOP_MARKERS):
                end_index = index
                break

        index = start_index + 1
        current_variant: Optional[BackgroundVariant] = None
        while index < end_index:
            tag_name, text = elements[index]
            lower = text.lower()

            if tag_name != "h3":
                index += 1
                continue

            variant_match = VARIANT_HEADING_RE.match(text)
            if variant_match:
                block_text, next_index = self._collect_text_block(elements, index + 1, end_index)
                current_variant = BackgroundVariant(
                    title_ru=prettify_heading_title(clean_site_text(variant_match.group(1))),
                    description=block_text,
                )
                result.variants.append(current_variant)
                index = next_index
                continue

            feature_match = FEATURE_HEADING_RE.match(text)
            if feature_match:
                block_text, next_index = self._collect_text_block(elements, index + 1, end_index)
                feature = BackgroundFeature(
                    title_ru=prettify_heading_title(clean_site_text(feature_match.group(1))),
                    description=block_text,
                )
                if current_variant is not None and lower.startswith("альтернативное умение:"):
                    current_variant.features.append(feature)
                else:
                    result.features.append(feature)
                    current_variant = None
                index = next_index
                continue

            current_variant = None
            index += 1

        self._page_cache[cache_key] = result
        return result

    @staticmethod
    def _collect_text_block(
        elements: Sequence[Tuple[str, str]],
        start_index: int,
        end_index: int,
    ) -> Tuple[str, int]:
        parts: List[str] = []
        index = start_index
        while index < end_index:
            tag_name, text = elements[index]
            if tag_name in {"h2", "h3"}:
                break
            if tag_name in {"p", "li"}:
                clean = clean_site_text(text)
                if clean:
                    parts.append(clean)
            index += 1
        return "\n\n".join(dict.fromkeys(parts)), index


def set_ru_name(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    stats["ru_name"] = {"value": normalize_lookup_text(value)}


def find_variant(page: BackgroundPageData, variant_title_ru: str) -> Optional[BackgroundVariant]:
    for variant in page.variants:
        if titles_match(variant.title_ru, variant_title_ru):
            return variant
    return None


def get_background_features(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    return stats.get("features", {}).get("value", [])


def apply_feature_match(feature_stats: Dict[str, Any], site_feature: BackgroundFeature) -> None:
    if "name" in feature_stats and isinstance(feature_stats["name"], dict) and "value" in feature_stats["name"]:
        feature_stats["name"]["value"] = site_feature.title_ru
    descriptions = feature_stats.get("descriptions", {}).get("value", [])
    if descriptions:
        first = descriptions[0].get("stats", {})
        if "description" in first and isinstance(first["description"], dict):
            first["description"]["value"] = site_feature.description


def sync_features_from_site(
    stats: Dict[str, Any],
    site_features: Sequence[BackgroundFeature],
    missing: List[str],
) -> None:
    local_features = get_background_features(stats)
    if not local_features or not site_features:
        return

    if len(local_features) != len(site_features):
        missing.append(f"feature_count_mismatch:{len(local_features)}:{len(site_features)}")
        return

    for local_feature, site_feature in zip(local_features, site_features):
        apply_feature_match(local_feature.get("stats", {}), site_feature)


def is_translatable_string_path(path: str) -> bool:
    if BACKGROUND_NAME_PATH_RE.match(path):
        return False
    if any(path.endswith(suffix) for suffix in IGNORE_REMAINING_ENGLISH_PATH_PARTS):
        return False
    return any(path.endswith(suffix) for suffix in TRANSLATABLE_SUFFIXES)


def collect_translatable_refs(obj: Any, path: str = "") -> List[StringRef]:
    refs: List[StringRef] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(value, str):
                if is_translatable_string_path(child_path) and looks_english(value):
                    refs.append(StringRef(obj, key, child_path, value))
            else:
                refs.extend(collect_translatable_refs(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            if isinstance(value, str):
                if is_translatable_string_path(child_path) and looks_english(value):
                    refs.append(StringRef(obj, index, child_path, value))
            else:
                refs.extend(collect_translatable_refs(value, child_path))
    return refs


def collect_remaining_english_strings(obj: Any, path: str = "") -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(collect_remaining_english_strings(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            found.extend(collect_remaining_english_strings(value, child_path))
    elif isinstance(obj, str):
        if looks_english(obj) and is_translatable_string_path(path):
            found.append((path, obj))
    return found


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
    client: DndSuBackgroundClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    stats = get_top_stats(doc)
    background_en_name = detect_background_lookup_name(path, stats)
    missing: List[str] = []

    resolved = client.resolve_background_page(background_en_name)
    page: Optional[BackgroundPageData] = None
    if resolved is None:
        missing.append(f"background_page:not_found:{background_en_name}")
    else:
        try:
            page = client.parse_background_page(resolved.url, expected_en_name=resolved.expected_en_name)
        except Exception as exc:
            missing.append(f"background_page:{background_en_name}:{exc}")

    if page is not None:
        target_title = page.title_ru
        site_features = list(page.features)

        if resolved and resolved.variant_title_ru:
            variant = find_variant(page, resolved.variant_title_ru)
            if variant:
                target_title = variant.title_ru
                if resolved.allow_feature_sync:
                    site_features = list(variant.features)
                else:
                    site_features = []
            else:
                missing.append(f"variant:not_found:{background_en_name}:{resolved.variant_title_ru}")
                site_features = []

        if target_title:
            set_ru_name(stats, target_title)

        if site_features:
            sync_features_from_site(stats, site_features, missing)

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
    parser = argparse.ArgumentParser(description="Переносит переводы предысторий из dnd.su в background_*.json")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./background_*.json")
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

    client = DndSuBackgroundClient(cache_dir=Path(args.cache_dir))
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
            for path, value in items[:30]:
                log(f"    - {path} = {value}")
            if len(items) > 30:
                log(f"    ... и ещё {len(items) - 30}")

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
