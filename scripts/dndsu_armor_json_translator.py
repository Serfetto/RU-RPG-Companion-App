#!/usr/bin/env python3
"""
Translate armor_*.rpg.json resources using dnd.su first and a fallback
translator for anything that cannot be sourced from the site.

Notes:
- magical armor is indexed from https://dnd.su/piece/items/index-list/;
- mundane armor is indexed from https://next.dnd.su/equipment/;
- some local armor files are variants stored as a single generic page on the
  site (for example Dragon Scale Mail colors and Armor of Vulnerability damage
  variants), so the script resolves and adapts those parent pages as needed.
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
from bs4 import BeautifulSoup

ITEMS_BASE_URL = "https://dnd.su"
ITEMS_INDEX_URL = urljoin(ITEMS_BASE_URL, "/piece/items/index-list/")
EQUIPMENT_BASE_URL = "https://next.dnd.su"
EQUIPMENT_INDEX_URL = urljoin(EQUIPMENT_BASE_URL, "/equipment/")
CACHE_DIR = Path(".dndsu_cache") / "armor"
TIMEOUT = 240
DEFAULT_TRANSLATOR_TIMEOUT = 240
REQUEST_PAUSE_SEC = 0.2
TRANSLATOR_RETRY_ATTEMPTS = 5
TRANSLATOR_RETRY_DELAY_SEC = 2.0
DEFAULT_TRANSLATOR_URL = "http://localhost:8000/chat"

PAGE_STOP_MARKERS = ("комментар", "галере")

ITEM_METADATA_PREFIXES = (
    "Распечатать",
    "Рекомендованная стоимость:",
    "Доспех (",
    "Оружие (",
    "Чудесный предмет",
    "Посох",
    "Кольцо",
    "Жезл",
    "Свиток",
    "Зелье",
)

EQUIPMENT_METADATA_PREFIXES = (
    "Стоимость:",
    "Вес:",
    "Класс защиты:",
    "Контейнер:",
    "Количество:",
    "Оружие:",
)

EQUIPMENT_CATEGORY_LINES = {
    "Лёгкий доспех",
    "Средний доспех",
    "Тяжёлый доспех",
    "Щит",
}

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
    ".rarity.value",
    ".modifier.value",
    ".recharge_type.value",
    ".recharge_event.value",
    ".stat.value",
    ".advantage_stat.value",
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
    "Charges": "Заряды",
}

DRAGON_SCALE_VARIANTS: Dict[str, Dict[str, str]] = {
    "black": {"dragon_genitive": "чёрного дракона", "damage_phrase": "урону кислотой"},
    "blue": {"dragon_genitive": "синего дракона", "damage_phrase": "урону электричеством"},
    "brass": {"dragon_genitive": "латунного дракона", "damage_phrase": "урону огнём"},
    "bronze": {"dragon_genitive": "бронзового дракона", "damage_phrase": "урону электричеством"},
    "copper": {"dragon_genitive": "медного дракона", "damage_phrase": "урону кислотой"},
    "gold": {"dragon_genitive": "золотого дракона", "damage_phrase": "урону огнём"},
    "green": {"dragon_genitive": "зелёного дракона", "damage_phrase": "урону ядом"},
    "red": {"dragon_genitive": "красного дракона", "damage_phrase": "урону огнём"},
    "silver": {"dragon_genitive": "серебряного дракона", "damage_phrase": "урону холодом"},
    "white": {"dragon_genitive": "белого дракона", "damage_phrase": "урону холодом"},
}

DAMAGE_VARIANTS: Dict[str, Dict[str, str]] = {
    "bludgeoning": {
        "title": "дробящий урон",
        "resistance": "дробящему урону",
        "vulnerabilities": "колющему и рубящему урону",
    },
    "piercing": {
        "title": "колющий урон",
        "resistance": "колющему урону",
        "vulnerabilities": "дробящему и рубящему урону",
    },
    "slashing": {
        "title": "рубящий урон",
        "resistance": "рубящему урону",
        "vulnerabilities": "дробящему и колющему урону",
    },
}

DEFAULT_DRAGON_SCALE_INTRO = (
    "Этот доспех изготавливается из чешуи определённого дракона. Иногда драконы "
    "сами собирают сброшенные чешуйки и дарят их гуманоидам. В других случаях "
    "успешные охотники тщательно выделывают и хранят шкуры убитых драконов. В "
    "любом случае, доспехи из чешуи драконов высоко ценятся."
)

DEFAULT_ARMOR_OF_VULNERABILITY_CURSE = (
    "Проклятье. Этот доспех проклят, но это становится понятно только когда на "
    "него используется заклинание опознание или когда вы настраиваетесь на него. "
    "Настройка на доспех проклинает вас до тех пор, пока вы не станете целью "
    "заклинания снятие проклятья или подобной магии; снятие доспеха не оканчивает "
    "проклятье."
)


@dataclass
class StringRef:
    container: Any
    key: Any
    path: str
    value: str


@dataclass
class SiteEntry:
    kind: str
    href: str
    title_ru: str
    title_en: str
    slug: str


@dataclass
class ArmorPageData:
    title_ru: str = ""
    description: str = ""


@dataclass
class ResolvedArmorPage:
    url: str
    kind: str
    expected_en_name: str
    exact_match: bool = True
    variant_family: Optional[str] = None
    variant_key: Optional[str] = None


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
    text = re.sub(r"\s*\[[^\]]+\]\s*[A-Z0-9'. -]*\s*$", "", text)
    match = re.search(r"\s*\(([^()]+)\)\s*$", text)
    if match:
        marker = normalize_spaces(match.group(1))
        upper_count = sum(1 for char in marker if char.isupper())
        if upper_count >= 2 and " " not in marker:
            text = text[: match.start()].strip()
    return text.strip(" -—")


def clean_site_text(text: str) -> str:
    text = normalize_lookup_text(text)
    text = re.sub(r"\s*\[([^\]]*[A-Za-z][^\]]*)\]", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return normalize_spaces(text)


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


def detect_armor_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("armor_"):
        stem = stem[len("armor_") :]

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

    raise RuntimeError(f"Не удалось восстановить английское имя доспеха из файла {path.name!r}")


def build_lookup_candidates(name: str) -> List[str]:
    candidates: List[str] = []

    def add(value: Optional[str]) -> None:
        if not value:
            return
        value = normalize_lookup_text(value)
        key = canonical_key(value)
        if not key:
            return
        if any(canonical_key(existing) == key for existing in candidates):
            return
        candidates.append(value)

    normalized = normalize_lookup_text(name)
    add(normalized)
    if not normalized.lower().endswith(" armor"):
        add(f"{normalized} Armor")
    if normalized.lower().endswith(" armor"):
        add(normalized[: -len(" armor")])
    return candidates


def set_ru_name(stats: Dict[str, Any], value: Optional[str]) -> None:
    if not value:
        return
    if "ru_name" in stats and isinstance(stats["ru_name"], dict):
        stats["ru_name"]["value"] = value
        return
    stats["ru_name"] = {"value": value}


def seed_ru_name_from_name(stats: Dict[str, Any]) -> None:
    if "ru_name" in stats and isinstance(stats["ru_name"], dict):
        current = stats["ru_name"].get("value")
        if isinstance(current, str) and current.strip():
            return

    name_value = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if not name_value:
        return

    stats["ru_name"] = {"value": name_value}


def set_description(stats: Dict[str, Any], value: Optional[str]) -> None:
    if not value:
        return
    description = stats.get("description")
    if isinstance(description, dict) and "value" in description:
        description["value"] = value
        return
    stats["description"] = {"value": value}


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
                    raise RuntimeError(
                        f"Переводчик вернул {len(translations)} переводов вместо {len(texts)}"
                    )
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


class DndSuArmorClient:
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
        self._items_index: Optional[List[SiteEntry]] = None
        self._equipment_index: Optional[List[SiteEntry]] = None
        self._page_cache: Dict[str, ArmorPageData] = {}

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

    def build_items_index(self) -> List[SiteEntry]:
        if self._items_index is not None:
            return self._items_index
        self._items_index = self._build_index(ITEMS_INDEX_URL, ITEMS_BASE_URL, kind="item")
        return self._items_index

    def build_equipment_index(self) -> List[SiteEntry]:
        if self._equipment_index is not None:
            return self._equipment_index
        self._equipment_index = self._build_index(EQUIPMENT_INDEX_URL, EQUIPMENT_BASE_URL, kind="equipment")
        return self._equipment_index

    def _build_index(self, url: str, base_url: str, kind: str) -> List[SiteEntry]:
        soup = self.get_soup(url)
        entries: List[SiteEntry] = []
        seen: set[str] = set()

        for div in soup.select("div.for_filter"):
            anchor = div.find("a", href=True)
            if anchor is None:
                continue
            href = urljoin(base_url, anchor["href"])
            if kind == "item" and "/items/" not in href:
                continue
            if kind == "equipment" and "/equipment/" not in href:
                continue
            if "/homebrew/" in href:
                continue
            if href in seen:
                continue
            seen.add(href)

            search = normalize_spaces(div.get("data-search", ""))
            parts = [normalize_lookup_text(part) for part in search.split(",") if normalize_lookup_text(part)]
            title_ru = parts[0] if parts else normalize_spaces(anchor.get_text(" ", strip=True))
            title_en = parts[1] if len(parts) > 1 else ""
            slug = re.sub(r"^\d+-", "", href.rstrip("/").split("/")[-1])
            entries.append(SiteEntry(kind=kind, href=href, title_ru=title_ru, title_en=title_en, slug=slug))

        return entries

    def _find_exact_entry(self, entries: Sequence[SiteEntry], lookup_candidates: Sequence[str]) -> Optional[SiteEntry]:
        slug_candidates = {slugify_name(candidate) for candidate in lookup_candidates if candidate}
        key_candidates = {canonical_key(candidate) for candidate in lookup_candidates if candidate}

        for entry in entries:
            if entry.slug in slug_candidates:
                return entry
            if entry.title_en and canonical_key(entry.title_en) in key_candidates:
                return entry
        return None

    def resolve_armor_page(self, armor_en_name: str) -> Optional[ResolvedArmorPage]:
        lookup_candidates = build_lookup_candidates(armor_en_name)

        item_entry = self._find_exact_entry(self.build_items_index(), lookup_candidates)
        if item_entry:
            return ResolvedArmorPage(
                url=item_entry.href,
                kind=item_entry.kind,
                expected_en_name=item_entry.title_en or armor_en_name,
                exact_match=True,
            )

        equipment_entry = self._find_exact_entry(self.build_equipment_index(), lookup_candidates)
        if equipment_entry:
            return ResolvedArmorPage(
                url=equipment_entry.href,
                kind=equipment_entry.kind,
                expected_en_name=equipment_entry.title_en or armor_en_name,
                exact_match=True,
            )

        normalized_slug = slugify_name(armor_en_name)
        dragon_match = re.fullmatch(
            r"(black|blue|brass|bronze|copper|gold|green|red|silver|white)-dragon-scale-mail",
            normalized_slug,
        )
        if dragon_match:
            parent = self._find_exact_entry(self.build_items_index(), ["Dragon Scale Mail"])
            if parent:
                return ResolvedArmorPage(
                    url=parent.href,
                    kind="item",
                    expected_en_name=parent.title_en or "Dragon Scale Mail",
                    exact_match=False,
                    variant_family="dragon_scale_mail",
                    variant_key=dragon_match.group(1),
                )

        vulnerability_match = re.fullmatch(
            r"armor-of-vulnerability-(bludgeoning|piercing|slashing)",
            normalized_slug,
        )
        if vulnerability_match:
            parent = self._find_exact_entry(self.build_items_index(), ["Armor of Vulnerability"])
            if parent:
                return ResolvedArmorPage(
                    url=parent.href,
                    kind="item",
                    expected_en_name=parent.title_en or "Armor of Vulnerability",
                    exact_match=False,
                    variant_family="armor_of_vulnerability",
                    variant_key=vulnerability_match.group(1),
                )

        return None

    def parse_page(self, url: str, kind: str, expected_en_name: str) -> ArmorPageData:
        cache_key = f"{kind}::{url}::{expected_en_name}"
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
        title_ru = ""
        for index, (tag_name, text) in enumerate(elements):
            if tag_name != "h2":
                continue
            lowered = text.lower()
            if f"[{expected_en_name.lower()}]" in lowered or canonical_key(expected_en_name) in canonical_key(text):
                start_index = index
                title_ru = strip_brackets_title(text)
                break

        if start_index is None:
            raise RuntimeError(f"Не удалось найти начало страницы доспеха {expected_en_name!r}")

        end_index = len(elements)
        for index in range(start_index + 1, len(elements)):
            tag_name, text = elements[index]
            if tag_name == "h2" and any(marker in text.lower() for marker in PAGE_STOP_MARKERS):
                end_index = index
                break

        if kind == "equipment":
            description = self._collect_equipment_description(elements, start_index + 1, end_index)
        else:
            description = self._collect_item_description(elements, start_index + 1, end_index)

        page = ArmorPageData(title_ru=title_ru, description=description)
        self._page_cache[cache_key] = page
        return page

    @staticmethod
    def _collect_item_description(
        elements: Sequence[Tuple[str, str]],
        start_index: int,
        end_index: int,
    ) -> str:
        paragraph_parts: List[str] = []
        fallback_parts: List[str] = []
        for index in range(start_index, end_index):
            tag_name, text = elements[index]
            clean = clean_site_text(text)
            if not clean:
                continue
            if tag_name == "p":
                paragraph_parts.append(clean)
                continue
            if tag_name == "li" and not clean.startswith(ITEM_METADATA_PREFIXES):
                fallback_parts.append(clean)

        parts = paragraph_parts or fallback_parts
        return "\n\n".join(dict.fromkeys(parts))

    @staticmethod
    def _collect_equipment_description(
        elements: Sequence[Tuple[str, str]],
        start_index: int,
        end_index: int,
    ) -> str:
        paragraph_parts: List[str] = []
        fallback_parts: List[str] = []
        for index in range(start_index, end_index):
            tag_name, text = elements[index]
            if tag_name not in {"li", "p"}:
                continue
            clean = clean_site_text(text)
            if not clean:
                continue
            if clean in EQUIPMENT_CATEGORY_LINES:
                continue
            if clean.startswith(EQUIPMENT_METADATA_PREFIXES):
                continue
            if tag_name == "p":
                paragraph_parts.append(clean)
            else:
                fallback_parts.append(clean)
        parts = paragraph_parts or fallback_parts
        return "\n\n".join(dict.fromkeys(parts))


def apply_dragon_scale_variant(stats: Dict[str, Any], page: ArmorPageData, variant_key: str) -> None:
    variant = DRAGON_SCALE_VARIANTS.get(variant_key)
    if not variant:
        return

    intro = page.description.split("\n\n", 1)[0] if page.description else DEFAULT_DRAGON_SCALE_INTRO
    title_ru = f"Доспех из чешуи {variant['dragon_genitive']}"
    description = "\n\n".join(
        [
            intro,
            (
                "Пока вы носите этот доспех, вы получаете бонус +1 к КД, совершаете "
                "с преимуществом спасброски от Ужасающей внешности и оружия дыхания "
                f"драконов, а также обладаете сопротивлением {variant['damage_phrase']}."
            ),
            (
                "Кроме того, вы можете действием сосредоточиться, чтобы с помощью "
                "магии определить расстояние и направление до ближайшего "
                f"{variant['dragon_genitive']} в пределах 30 миль от вас. Это особое "
                "действие нельзя использовать повторно до следующего рассвета."
            ),
        ]
    )

    set_ru_name(stats, title_ru)
    set_description(stats, description)


def apply_armor_of_vulnerability_variant(stats: Dict[str, Any], page: ArmorPageData, variant_key: str) -> None:
    variant = DAMAGE_VARIANTS.get(variant_key)
    if not variant:
        return

    base_name = page.title_ru or "Доспех уязвимости"
    title_ru = f"{base_name} ({variant['title']})"
    description = "\n\n".join(
        [
            f"Пока вы носите этот доспех, вы получаете сопротивление {variant['resistance']}.",
            (
                f"{DEFAULT_ARMOR_OF_VULNERABILITY_CURSE} Пока вы прокляты, вы "
                f"обладаете уязвимостью к {variant['vulnerabilities']}."
            ),
        ]
    )

    set_ru_name(stats, title_ru)
    set_description(stats, description)


def is_translatable_string_path(path: str) -> bool:
    if path == "stats.name.value":
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
    client: DndSuArmorClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    stats = get_top_stats(doc)
    armor_en_name = detect_armor_lookup_name(path, stats)
    missing: List[str] = []

    resolved = client.resolve_armor_page(armor_en_name)
    page: Optional[ArmorPageData] = None
    if resolved is None:
        missing.append(f"armor_page:not_found:{armor_en_name}")
    else:
        try:
            page = client.parse_page(resolved.url, resolved.kind, resolved.expected_en_name)
        except Exception as exc:
            missing.append(f"armor_page:{armor_en_name}:{exc}")

    if page is not None:
        if resolved and resolved.variant_family == "dragon_scale_mail" and resolved.variant_key:
            apply_dragon_scale_variant(stats, page, resolved.variant_key)
        elif resolved and resolved.variant_family == "armor_of_vulnerability" and resolved.variant_key:
            apply_armor_of_vulnerability_variant(stats, page, resolved.variant_key)
        else:
            if page.title_ru:
                set_ru_name(stats, page.title_ru)
            current_description = normalize_lookup_text(stats.get("description", {}).get("value", ""))
            if page.description and (not current_description or looks_english(current_description)):
                set_description(stats, page.description)

    seed_ru_name_from_name(stats)
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
    parser = argparse.ArgumentParser(description="Переносит переводы доспехов из dnd.su в armor_*.json")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./armor_*.json")
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

    client = DndSuArmorClient(cache_dir=Path(args.cache_dir))
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
