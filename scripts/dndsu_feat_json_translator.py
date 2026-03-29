#!/usr/bin/env python3
"""
Translate feat_*.rpg.json resources using dnd.su feat pages first and a
fallback translator for anything that cannot be sourced from the site.
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
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://dnd.su"
CACHE_DIR = Path(".dndsu_cache") / "feats"
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

PAGE_STOP_MARKERS = ("комментар", "галере")
DIRECT_FEAT_URL_RE = re.compile(r"/feats/\d+-")
HEADING_RE = re.compile(r"^(.*?)\s*\[([^\]]+)\](?:\s+.*)?$")
EN_STOPWORDS = {"a", "an", "the"}

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
    "Uses left": "Осталось использований",
}

ABILITY_GENITIVE: Dict[str, str] = {
    "str": "Силы",
    "dex": "Ловкости",
    "con": "Телосложения",
    "int": "Интеллекта",
    "wis": "Мудрости",
    "cha": "Харизмы",
}

DAMAGE_RESISTANCE_MAP: Dict[str, str] = {
    "acid": "Сопротивление кислоте",
    "cold": "Сопротивление холоду",
    "fire": "Сопротивление огню",
    "lightning": "Сопротивление электричеству",
    "thunder": "Сопротивление звуку",
}

SLUG_OVERRIDES: Dict[str, str] = {
    "ember-of-the-fire-giant": "embar-of-the-fire-giant",
}


@dataclass
class StringRef:
    container: Any
    key: Any
    path: str
    value: str


@dataclass
class FeatStatsRef:
    stats: Dict[str, Any]
    path: str


@dataclass
class FeatPageData:
    title_ru: str = ""
    title_en: str = ""
    prerequisites: str = ""
    description: str = ""


@dataclass
class ResolvedFeatPage:
    url: str
    expected_en_name: str
    exact_match: bool = True


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


def strip_brackets_title(text: str) -> str:
    text = normalize_spaces(text)
    match = HEADING_RE.match(text)
    if match:
        return normalize_spaces(match.group(1))
    text = re.sub(r"\s*\[[^\]]+\]\s*$", "", text)
    return text.strip(" -")


def slugify_name(name: str) -> str:
    name = normalize_lookup_text(name).lower().strip()
    name = name.replace("'", "")
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def normalize_slug_for_compare(slug: str) -> str:
    parts = [part for part in slug.split("-") if part and part not in EN_STOPWORDS]
    return "-".join(parts)


def english_title_key(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9']+", normalize_lookup_text(text).lower())
    filtered = [token for token in tokens if token not in EN_STOPWORDS]
    return " ".join(filtered)


def slug_similarity(left: str, right: str) -> float:
    left_norm = normalize_slug_for_compare(slugify_name(left) or left)
    right_norm = normalize_slug_for_compare(slugify_name(right) or right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = set(filter(None, normalize_slug_for_compare(slugify_name(left) or left).split("-")))
    right_tokens = set(filter(None, normalize_slug_for_compare(slugify_name(right) or right).split("-")))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def titles_match(left: str, right: str) -> bool:
    if canonical_key(left) == canonical_key(right):
        return True

    left_en = english_title_key(left)
    right_en = english_title_key(right)
    if left_en and right_en and left_en == right_en:
        return True

    ratio = slug_similarity(left, right)
    overlap = token_overlap_ratio(left, right)
    return ratio >= 0.94 or (ratio >= 0.86 and overlap >= 0.75)


def looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_lookup_text(text or "")))


def choose_translation_placeholder(text: str) -> Optional[str]:
    match = re.fullmatch(r"Choose\s+(\d+)", normalize_lookup_text(text).strip(), flags=re.I)
    if match:
        return f"Выберите {match.group(1)}"
    return None


def translate_ability_variant(text: str) -> Optional[str]:
    norm = normalize_lookup_text(text)
    compact = re.sub(r"\s+", " ", norm).strip()
    match = re.match(r"^(Str|Dex|Con|Int|Wis|Cha)\b\s*(.*)$", compact, flags=re.I)
    if not match:
        return None

    ability_key = match.group(1).lower()
    if ability_key not in ABILITY_GENITIVE:
        return None

    rest = canonical_key(match.group(2))
    if not rest:
        return None

    if any(marker in rest for marker in ("increase", "increased", "imcrease", "incredible")):
        base = f"Увеличение {ABILITY_GENITIVE[ability_key]}"
        if "saving throw proficiency" in rest:
            return f"{base} и владение спасброском"
        return base
    return None


def translate_damage_resistance_variant(text: str) -> Optional[str]:
    norm = normalize_lookup_text(text)
    match = re.match(r"^(Acid|Cold|Fire|Lightning|Thunder)\s+Resistance$", norm, flags=re.I)
    if not match:
        return None
    return DAMAGE_RESISTANCE_MAP.get(match.group(1).lower())


def translate_static_text(text: str) -> Optional[str]:
    norm = normalize_lookup_text(text)
    if norm in STATIC_TEXT_MAP:
        return STATIC_TEXT_MAP[norm]

    placeholder = choose_translation_placeholder(norm)
    if placeholder:
        return placeholder

    ability = translate_ability_variant(norm)
    if ability:
        return ability

    resistance = translate_damage_resistance_variant(norm)
    if resistance:
        return resistance

    return None


def clean_site_text(text: str) -> str:
    text = normalize_lookup_text(text)
    text = re.sub(r"\s*\[([^\]]*[A-Za-z][^\]]*)\]", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+([»])", r"\1", text)
    text = re.sub(r"([«])\s+", r"\1", text)
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


def detect_feat_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("feat_"):
        stem = stem[len("feat_") :]

    candidates = [
        stem.replace("_", " "),
        stem.replace("-", " "),
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

    raise RuntimeError(f"Не удалось восстановить английское имя черты из файла {path.name!r}")


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


class DndSuFeatClient:
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
        self._feat_index: Optional[List[Dict[str, str]]] = None
        self._page_cache: Dict[str, FeatPageData] = {}

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

    def build_feat_index(self) -> List[Dict[str, str]]:
        if self._feat_index is not None:
            return self._feat_index

        soup = self.get_soup(urljoin(BASE_URL, "/feats/"))
        entries: List[Dict[str, str]] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, anchor["href"])
            if not href.startswith(BASE_URL):
                continue
            if not DIRECT_FEAT_URL_RE.search(href):
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
            entries.append(
                {
                    "href": href,
                    "text": text,
                    "slug": match.group(1),
                }
            )

        self._feat_index = entries
        return entries

    def resolve_feat_page(self, feat_en_name: str) -> Optional[ResolvedFeatPage]:
        expected_slug = slugify_name(feat_en_name)
        entries = self.build_feat_index()

        direct_slug = SLUG_OVERRIDES.get(expected_slug, expected_slug)
        exact_candidates = [entry for entry in entries if entry["slug"] == direct_slug]
        if exact_candidates:
            return ResolvedFeatPage(url=exact_candidates[0]["href"], expected_en_name=feat_en_name, exact_match=True)

        normalized_expected = normalize_slug_for_compare(expected_slug)
        normalized_candidates = [
            entry
            for entry in entries
            if normalize_slug_for_compare(entry["slug"]) == normalized_expected
        ]
        for entry in normalized_candidates:
            page = self.parse_feat_page(entry["href"])
            if page.title_en and titles_match(page.title_en, feat_en_name):
                return ResolvedFeatPage(url=entry["href"], expected_en_name=page.title_en, exact_match=False)

        scored: List[Tuple[float, Dict[str, str]]] = []
        for entry in entries:
            ratio = SequenceMatcher(None, normalized_expected, normalize_slug_for_compare(entry["slug"])).ratio()
            overlap = token_overlap_ratio(expected_slug, entry["slug"])
            score = max(ratio, overlap)
            if score >= 0.70:
                scored.append((score, entry))
        scored.sort(key=lambda item: item[0], reverse=True)

        for _, entry in scored[:12]:
            page = self.parse_feat_page(entry["href"])
            if page.title_en and titles_match(page.title_en, feat_en_name):
                return ResolvedFeatPage(url=entry["href"], expected_en_name=page.title_en, exact_match=False)
        return None

    def parse_feat_page(self, url: str) -> FeatPageData:
        cached = self._page_cache.get(url)
        if cached is not None:
            return cached

        soup = self.get_soup(url)
        elements: List[Tuple[str, str]] = []
        for tag in soup.find_all(["h2", "h3", "p", "li"]):
            text = normalize_spaces(tag.get_text(" ", strip=True))
            if text:
                elements.append((tag.name.lower(), text))

        result = FeatPageData()
        start_index: Optional[int] = None

        for index, (tag_name, text) in enumerate(elements):
            if tag_name != "h2":
                continue
            match = HEADING_RE.match(text)
            if not match:
                continue
            result.title_ru = clean_site_text(match.group(1))
            result.title_en = normalize_spaces(match.group(2))
            start_index = index
            break

        if start_index is None:
            raise RuntimeError(f"Не удалось найти начало страницы черты: {url}")

        end_index = len(elements)
        for index in range(start_index + 1, len(elements)):
            tag_name, text = elements[index]
            if tag_name == "h2" and any(marker in canonical_key(text) for marker in PAGE_STOP_MARKERS):
                end_index = index
                break

        paragraphs: List[str] = []
        list_items: List[str] = []
        fallback_summary = ""
        for index in range(start_index + 1, end_index):
            tag_name, text = elements[index]
            text_key = canonical_key(text)

            if tag_name == "h3":
                break

            if tag_name == "li":
                if text_key == "распечатать":
                    continue
                if text_key.startswith("требование"):
                    value = text.split(":", 1)[1] if ":" in text else text
                    result.prerequisites = clean_site_text(value)
                    continue
                clean = clean_site_text(text)
                if not clean:
                    continue
                if paragraphs:
                    list_items.append(clean)
                elif not fallback_summary:
                    fallback_summary = clean
                else:
                    list_items.append(clean)
                continue

            if tag_name == "p":
                clean = clean_site_text(text)
                if clean and not clean.startswith("На данный момент в галерее"):
                    paragraphs.append(clean)

        description_parts = list(dict.fromkeys(paragraphs))
        if not description_parts and fallback_summary:
            description_parts.append(fallback_summary)

        for item in list_items:
            item_key = canonical_key(item)
            if not item_key:
                continue
            combined = " ".join(canonical_key(part) for part in description_parts)
            if item_key in combined:
                continue
            description_parts.append(item)

        if description_parts:
            result.description = "\n\n".join(description_parts)

        self._page_cache[url] = result
        return result


def ensure_feat_ru_names(obj: Any) -> None:
    if isinstance(obj, dict):
        if obj.get("resource_id") == "feat" and isinstance(obj.get("stats"), dict):
            stats = obj["stats"]
            name_value = normalize_lookup_text(stats.get("name", {}).get("value", ""))
            ru_name = stats.get("ru_name")
            if isinstance(ru_name, dict):
                if "value" not in ru_name or not ru_name.get("value"):
                    ru_name["value"] = name_value
            elif name_value:
                stats["ru_name"] = {"value": name_value}
        for value in obj.values():
            ensure_feat_ru_names(value)
    elif isinstance(obj, list):
        for item in obj:
            ensure_feat_ru_names(item)


def collect_feat_stats_blocks(obj: Any, path: str = "") -> List[FeatStatsRef]:
    found: List[FeatStatsRef] = []
    if isinstance(obj, dict):
        if obj.get("resource_id") == "feat" and isinstance(obj.get("stats"), dict):
            found.append(FeatStatsRef(stats=obj["stats"], path=path))
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(collect_feat_stats_blocks(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            found.extend(collect_feat_stats_blocks(value, child_path))
    return found


def set_feat_ru_name(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    ru_name = stats.get("ru_name")
    if isinstance(ru_name, dict):
        ru_name["value"] = value
    else:
        stats["ru_name"] = {"value": value}


def set_feat_description(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return

    descriptions = stats.get("descriptions")
    if isinstance(descriptions, dict):
        description_values = descriptions.get("value")
        if isinstance(description_values, list) and description_values:
            first_stats = description_values[0].get("stats", {})
            if isinstance(first_stats, dict):
                description = first_stats.get("description")
                if isinstance(description, dict) and "value" in description:
                    description["value"] = value
                    return

    description = stats.get("description")
    if isinstance(description, dict) and "value" in description:
        description["value"] = value


def set_feat_prerequisites(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return

    prerequisites = stats.get("prerequisites")
    if isinstance(prerequisites, dict):
        prerequisites["value"] = value


def apply_site_translations_to_feats(
    doc: Dict[str, Any],
    path: Path,
    client: DndSuFeatClient,
    missing: List[str],
) -> None:
    feat_refs = collect_feat_stats_blocks(doc)
    top_stats = get_top_stats(doc)
    if feat_refs and feat_refs[0].stats is top_stats:
        top_name = normalize_lookup_text(top_stats.get("name", {}).get("value", ""))
        if not top_name or not looks_english(top_name):
            try:
                inferred_name = detect_feat_lookup_name(path, top_stats)
                if "name" not in top_stats or not isinstance(top_stats["name"], dict):
                    top_stats["name"] = {"value": inferred_name}
            except Exception as exc:
                missing.append(f"feat_lookup_name:{exc}")

    for ref in feat_refs:
        stats = ref.stats
        feat_en_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
        if not feat_en_name or not looks_english(feat_en_name):
            continue

        resolved = client.resolve_feat_page(feat_en_name)
        if resolved is None:
            if ref.stats is top_stats:
                missing.append(f"feat_page:not_found:{feat_en_name}")
            continue

        try:
            page = client.parse_feat_page(resolved.url)
        except Exception as exc:
            if ref.stats is top_stats:
                missing.append(f"feat_page:{feat_en_name}:{exc}")
            continue

        if page.title_ru:
            set_feat_ru_name(stats, page.title_ru)
        if page.prerequisites:
            set_feat_prerequisites(stats, page.prerequisites)
        if page.description:
            set_feat_description(stats, page.description)


def is_translatable_string_path(path: str, resource_type: Optional[str]) -> bool:
    if resource_type == "feat" and path.endswith(".name.value"):
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
                and not (current_resource_type == "feat" and key == "name")
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
    client: DndSuFeatClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    missing: List[str] = []

    ensure_feat_ru_names(doc)
    apply_site_translations_to_feats(doc, path, client, missing)
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
    parser = argparse.ArgumentParser(description="Переносит переводы черт из dnd.su в feat_*.rpg.json")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./feat_*.rpg.json")
    parser.add_argument("--out-dir", default="translated", help="Куда сохранить результат")
    parser.add_argument("--in-place", action="store_true", help="Перезаписать исходные файлы")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR), help="Каталог кэша HTML")
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

    client = DndSuFeatClient(cache_dir=Path(args.cache_dir))
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
