#!/usr/bin/env python3
"""
Translate spell JSON resources using dnd.su spell pages first and a fallback
translator for anything that cannot be sourced from the site.

Notes:
- works with standalone spell_*.rpg.json files;
- also works with any nested `resource_id == "spell"` blocks inside other JSON;
- keeps `stats.name.value` untouched and writes the Russian title to `stats.ru_name.value`.
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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://dnd.su"
SPELL_INDEX_URL = urljoin(BASE_URL, "/piece/spells/index-list/")
CACHE_DIR = Path(".dndsu_cache") / "spells"
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
    ".components.value",
    ".casting_time.value",
    ".range.value",
    ".duration.value",
)

PAGE_STOP_MARKERS = ("комментар", "галере")
EN_BRACKET_RE = re.compile(r"\[([^\]]*[A-Za-z][^\]]*)\]")
SLUG_SPLIT_RE = re.compile(r"^\d+-(.+)$")

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
    "Concentration, up to 8 hours": "Концентрация, вплоть до 8 часов",
    "Concentration, up to 24 hours": "Концентрация, вплоть до 24 часов",
    "Up to 1 hour": "Вплоть до 1 часа",
    "10 feet": "10 футов",
    "30 feet": "30 футов",
    "60 feet": "60 футов",
    "90 feet": "90 футов",
    "120 feet": "120 футов",
    "150 feet": "150 футов",
    "300 feet": "300 футов",
    "500 feet": "500 футов",
    "1 mile": "1 миля",
    "Self (10-foot radius)": "На себя (радиус 10 футов)",
    "Self (15-foot cone)": "На себя (конус 15 футов)",
    "Self (15-foot cube)": "На себя (куб 15 футов)",
    "Self (15-foot radius)": "На себя (радиус 15 футов)",
    "Self (30-foot cone)": "На себя (конус 30 футов)",
    "Self (30-foot line)": "На себя (линия 30 футов)",
    "Self (30-foot radius)": "На себя (радиус 30 футов)",
    "Self (60-foot cone)": "На себя (конус 60 футов)",
}


@dataclass
class StringRef:
    container: Any
    key: Any
    path: str
    value: str


@dataclass
class SpellStatsRef:
    stats: Dict[str, Any]
    path: str


@dataclass
class SpellIndexEntry:
    href: str
    title_ru: str
    title_en: str
    slug: str


@dataclass
class ResolvedSpellPage:
    url: str
    expected_en_name: str
    index_title_ru: str = ""
    exact_match: bool = True


@dataclass
class SpellPageData:
    title_ru: str = ""
    description: str = ""
    higher_level_description: str = ""
    casting_time: str = ""
    range: str = ""
    duration: str = ""
    material_components: Optional[str] = None


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
    text = re.sub(r"\s*\[[^\]]+\]\s*[A-Z0-9'’./ -]*\s*$", "", text)
    return text.strip(" -—")


def clean_site_text(text: str) -> str:
    text = normalize_lookup_text(text)
    text = re.sub(r"\s*\[([^\]]*[A-Za-z][^\]]*)\]", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+([»])", r"\1", text)
    text = re.sub(r"([«])\s+", r"\1", text)
    return normalize_spaces(text)


def extract_slug_from_href(href: str) -> str:
    last = href.rstrip("/").split("/")[-1]
    match = SLUG_SPLIT_RE.match(last)
    return match.group(1) if match else last


def looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_lookup_text(text or "")))


def english_title_key(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9']+", normalize_lookup_text(text).lower())
    return " ".join(tokens)


def titles_match(left: str, right: str) -> bool:
    if canonical_key(left) == canonical_key(right):
        return True
    left_en = english_title_key(left)
    right_en = english_title_key(right)
    return bool(left_en and right_en and left_en == right_en)


def slugify_name(name: str) -> str:
    name = normalize_lookup_text(name).lower().strip()
    name = name.replace("'", "")
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def extract_spell_lookup_name(text: str) -> str:
    norm = normalize_lookup_text(text)
    match = EN_BRACKET_RE.search(norm)
    if match:
        inner = normalize_lookup_text(match.group(1))
        if looks_english(inner):
            return inner
    return norm


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


def detect_spell_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = extract_spell_lookup_name(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("spell_"):
        stem = stem[len("spell_") :]

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

    raise RuntimeError(f"Не удалось восстановить английское имя заклинания из файла {path.name!r}")


def looks_like_higher_level_paragraph(text: str) -> bool:
    key = canonical_key(text)
    if not key:
        return False

    if key.startswith("на больших уровнях"):
        return True
    if key.startswith("на более высоких уровнях"):
        return True
    if "урон этого заклинания увеличивается" in key:
        return True
    if "когда вы достигаете 5 го уровня" in key:
        return True
    if "когда вы достигаете 5 уровня" in key:
        return True
    if "используя ячейку" in key and "уров" in key:
        return True
    if "урон увеличивается на" in key and "уров" in key:
        return True
    if "за каждый уровень ячейки выше" in key:
        return True
    if "целью этого заклинания может стать" in key and "дополнительно" in key:
        return True
    return False


def paragraph_has_higher_level_marker(tag: Tag, text: str) -> bool:
    strong_texts = [normalize_lookup_text(node.get_text(" ", strip=True)) for node in tag.find_all(["strong", "em"])]
    if any("На больших уровнях" in item for item in strong_texts):
        return True
    return looks_like_higher_level_paragraph(text)


def split_embedded_higher_level_text(text: str) -> Tuple[str, str]:
    norm = normalize_lookup_text(text)
    markers = ["На больших уровнях.", "На более высоких уровнях."]
    for marker in markers:
        index = norm.find(marker)
        if index > 0:
            return normalize_spaces(norm[:index]), normalize_spaces(norm[index:])
    return norm, ""


def join_unique_texts(parts: Sequence[str]) -> str:
    ordered: List[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = clean_site_text(part)
        key = canonical_key(clean)
        if not clean or key in seen:
            continue
        seen.add(key)
        ordered.append(clean)
    return "\n\n".join(ordered)


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


class DndSuSpellClient:
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
        self._spell_index: Optional[List[SpellIndexEntry]] = None
        self._spell_index_by_en: Dict[str, SpellIndexEntry] = {}
        self._spell_index_by_slug: Dict[str, SpellIndexEntry] = {}
        self._resolved_cache: Dict[str, Optional[ResolvedSpellPage]] = {}
        self._page_cache: Dict[str, SpellPageData] = {}

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

    def build_spell_index(self) -> List[SpellIndexEntry]:
        if self._spell_index is not None:
            return self._spell_index

        html_text = self.get_html(SPELL_INDEX_URL)
        marker = "window.LIST ="
        start = html_text.find(marker)
        if start < 0:
            raise RuntimeError("Не удалось найти window.LIST на странице индекса заклинаний dnd.su")
        start += len(marker)
        end = html_text.find(";</script>", start)
        if end < 0:
            raise RuntimeError("Не удалось выделить JSON индекса заклинаний dnd.su")

        payload = html_text[start:end].strip()
        data = json.loads(payload)
        cards = data.get("cards")
        if not isinstance(cards, list):
            raise RuntimeError("Индекс заклинаний dnd.su имеет неожиданный формат")

        entries: List[SpellIndexEntry] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            title_ru = normalize_lookup_text(card.get("title", ""))
            title_en = normalize_lookup_text(card.get("title_en", ""))
            href = urljoin(BASE_URL, str(card.get("link", "")))
            if not href or not title_en:
                continue

            entry = SpellIndexEntry(
                href=href,
                title_ru=title_ru,
                title_en=title_en,
                slug=extract_slug_from_href(href),
            )
            entries.append(entry)

            en_key = canonical_key(title_en)
            if en_key and en_key not in self._spell_index_by_en:
                self._spell_index_by_en[en_key] = entry

            slug_key = slugify_name(title_en) or entry.slug
            if slug_key and slug_key not in self._spell_index_by_slug:
                self._spell_index_by_slug[slug_key] = entry

        self._spell_index = entries
        return entries

    def resolve_spell_page(self, spell_name: str) -> Optional[ResolvedSpellPage]:
        lookup = extract_spell_lookup_name(spell_name)
        if not lookup:
            return None

        cache_key = canonical_key(lookup)
        if cache_key in self._resolved_cache:
            return self._resolved_cache[cache_key]

        self.build_spell_index()

        exact = self._spell_index_by_en.get(cache_key)
        if exact is not None:
            resolved = ResolvedSpellPage(
                url=exact.href,
                expected_en_name=exact.title_en,
                index_title_ru=exact.title_ru,
                exact_match=True,
            )
            self._resolved_cache[cache_key] = resolved
            return resolved

        slug_key = slugify_name(lookup)
        by_slug = self._spell_index_by_slug.get(slug_key)
        if by_slug is not None:
            resolved = ResolvedSpellPage(
                url=by_slug.href,
                expected_en_name=by_slug.title_en,
                index_title_ru=by_slug.title_ru,
                exact_match=titles_match(by_slug.title_en, lookup),
            )
            self._resolved_cache[cache_key] = resolved
            return resolved

        for entry in self._spell_index or []:
            if titles_match(entry.title_en, lookup):
                resolved = ResolvedSpellPage(
                    url=entry.href,
                    expected_en_name=entry.title_en,
                    index_title_ru=entry.title_ru,
                    exact_match=False,
                )
                self._resolved_cache[cache_key] = resolved
                return resolved

        self._resolved_cache[cache_key] = None
        return None

    def parse_spell_page(
        self,
        url: str,
        expected_en_name: Optional[str] = None,
        fallback_title_ru: str = "",
        split_higher_level: bool = True,
    ) -> SpellPageData:
        cache_key = f"{url}::split_higher_level={int(split_higher_level)}"
        cached = self._page_cache.get(cache_key)
        if cached is not None:
            return cached

        soup = self.get_soup(url)
        result = SpellPageData(title_ru=fallback_title_ru)

        elements: List[Tuple[str, str, Tag]] = []
        for tag in soup.find_all(["h2", "p", "li"]):
            text = normalize_spaces(tag.get_text(" ", strip=True))
            if text:
                elements.append((tag.name.lower(), text, tag))

        started = False
        description_paragraphs: List[str] = []
        higher_level_paragraphs: List[str] = []
        li_fallback: List[str] = []

        for tag_name, text, tag in elements:
            if not started:
                if tag_name != "h2":
                    continue
                match = EN_BRACKET_RE.search(text)
                if not match:
                    continue
                heading_en_name = normalize_lookup_text(match.group(1))
                if expected_en_name is not None and not titles_match(heading_en_name, expected_en_name):
                    continue
                result.title_ru = strip_brackets_title(text) or fallback_title_ru
                started = True
                continue

            if tag_name == "h2" and any(marker in canonical_key(text) for marker in PAGE_STOP_MARKERS):
                break

            if tag_name == "li":
                if text == "Распечатать":
                    continue
                if text.startswith("Время накладывания:"):
                    result.casting_time = normalize_spaces(text.split(":", 1)[1])
                    continue
                if text.startswith("Дистанция:"):
                    result.range = normalize_spaces(text.split(":", 1)[1])
                    continue
                if text.startswith("Длительность:"):
                    result.duration = normalize_spaces(text.split(":", 1)[1])
                    continue
                if text.startswith("Компоненты:"):
                    result.material_components = self._extract_material_components(normalize_spaces(text.split(":", 1)[1]))
                    continue
                if text.startswith("Классы:") or text.startswith("Подклассы:"):
                    continue
                if re.match(r"^(Заговор|\d+\s+уровень)", text):
                    continue
                li_fallback.append(text)
                continue

            if tag_name == "p":
                clean = clean_site_text(text)
                if not clean:
                    continue
                if split_higher_level and paragraph_has_higher_level_marker(tag, clean):
                    higher_level_paragraphs.append(clean)
                elif split_higher_level and higher_level_paragraphs:
                    higher_level_paragraphs.append(clean)
                else:
                    description_paragraphs.append(clean)

        if not description_paragraphs and li_fallback:
            if split_higher_level:
                for item in li_fallback:
                    main_text, higher_text = split_embedded_higher_level_text(clean_site_text(item))
                    if main_text:
                        description_paragraphs.append(main_text)
                    if higher_text:
                        higher_level_paragraphs.append(higher_text)
            else:
                description_paragraphs.extend(clean_site_text(item) for item in li_fallback if clean_site_text(item))

        if split_higher_level and not higher_level_paragraphs and len(description_paragraphs) > 1 and looks_like_higher_level_paragraph(description_paragraphs[-1]):
            higher_level_paragraphs.append(description_paragraphs.pop())

        result.description = join_unique_texts(description_paragraphs)
        result.higher_level_description = join_unique_texts(higher_level_paragraphs) if split_higher_level else ""

        if not result.title_ru:
            result.title_ru = fallback_title_ru

        self._page_cache[cache_key] = result
        return result

    @staticmethod
    def _extract_material_components(components_line: str) -> Optional[str]:
        match = re.search(r"\((.+)\)", components_line)
        if match:
            return normalize_spaces(match.group(1))
        return None


def ensure_spell_ru_names(obj: Any) -> None:
    if isinstance(obj, dict):
        if obj.get("resource_id") == "spell" and isinstance(obj.get("stats"), dict):
            stats = obj["stats"]
            name_value = normalize_lookup_text(stats.get("name", {}).get("value", ""))
            ru_name = stats.get("ru_name")
            if isinstance(ru_name, dict):
                if "value" not in ru_name or not ru_name.get("value"):
                    ru_name["value"] = name_value
            elif name_value:
                stats["ru_name"] = {"value": name_value}
        for value in obj.values():
            ensure_spell_ru_names(value)
    elif isinstance(obj, list):
        for item in obj:
            ensure_spell_ru_names(item)


def collect_spell_stats_blocks(obj: Any, path: str = "") -> List[SpellStatsRef]:
    found: List[SpellStatsRef] = []
    if isinstance(obj, dict):
        if obj.get("resource_id") == "spell" and isinstance(obj.get("stats"), dict):
            found.append(SpellStatsRef(stats=obj["stats"], path=path))
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(collect_spell_stats_blocks(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child_path = f"{path}[{index}]"
            found.extend(collect_spell_stats_blocks(value, child_path))
    return found


def set_spell_ru_name(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    ru_name = stats.get("ru_name")
    if isinstance(ru_name, dict):
        ru_name["value"] = normalize_lookup_text(value)
    else:
        stats["ru_name"] = {"value": normalize_lookup_text(value)}


def set_spell_higher_level_description(stats: Dict[str, Any], value: str) -> None:
    if not value:
        return
    higher = stats.get("higher_level_description")
    if isinstance(higher, dict):
        higher["value"] = value
    elif "higher_level_description" in stats:
        stats["higher_level_description"] = {"value": value}


def apply_site_translations_to_spells(
    doc: Dict[str, Any],
    path: Path,
    client: DndSuSpellClient,
    missing: List[str],
) -> None:
    spell_refs = collect_spell_stats_blocks(doc)
    top_stats = get_top_stats(doc)

    for ref in spell_refs:
        stats = ref.stats
        spell_en_name = extract_spell_lookup_name(stats.get("name", {}).get("value", ""))
        if not spell_en_name or not looks_english(spell_en_name):
            if stats is top_stats:
                try:
                    spell_en_name = detect_spell_lookup_name(path, stats)
                except Exception as exc:
                    missing.append(f"spell_lookup_name:{exc}")
                    continue
            else:
                continue

        resolved = client.resolve_spell_page(spell_en_name)
        if resolved is None:
            missing.append(f"spell_page:not_found:{spell_en_name}")
            continue

        try:
            page = client.parse_spell_page(
                resolved.url,
                expected_en_name=resolved.expected_en_name,
                fallback_title_ru=resolved.index_title_ru,
                split_higher_level=isinstance(stats.get("higher_level_description"), dict),
            )
        except Exception as exc:
            missing.append(f"spell_page:{spell_en_name}:{exc}")
            continue

        if page.title_ru:
            set_spell_ru_name(stats, page.title_ru)
        if page.casting_time and isinstance(stats.get("casting_time"), dict):
            stats["casting_time"]["value"] = page.casting_time
        if page.range and isinstance(stats.get("range"), dict):
            stats["range"]["value"] = page.range
        if page.duration and isinstance(stats.get("duration"), dict):
            stats["duration"]["value"] = page.duration
        if page.material_components and isinstance(stats.get("components"), dict):
            stats["components"]["value"] = page.material_components
        if page.description and isinstance(stats.get("description"), dict):
            stats["description"]["value"] = page.description
        if page.higher_level_description:
            set_spell_higher_level_description(stats, page.higher_level_description)


def is_translatable_string_path(path: str, resource_type: Optional[str]) -> bool:
    if resource_type == "spell" and path.endswith(".name.value"):
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
                key in {"name", "description", "higher_level_description", "ru_name", "components", "casting_time", "range", "duration"}
                and isinstance(value, dict)
                and isinstance(value.get("value"), str)
                and not (current_resource_type == "spell" and key == "name")
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
    client: DndSuSpellClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    missing: List[str] = []

    ensure_spell_ru_names(doc)
    apply_site_translations_to_spells(doc, path, client, missing)
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
    parser = argparse.ArgumentParser(description="Переносит переводы заклинаний из dnd.su в spell JSON-ресурсы")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./spell_*.json")
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

    client = DndSuSpellClient(cache_dir=Path(args.cache_dir))
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
