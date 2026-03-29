#!/usr/bin/env python3
"""
Translate class_*.json resources using dnd.su class pages first and a fallback
translator for anything that could not be matched confidently.
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
CACHE_DIR = APP_ROOT / ".dndsu_cache"
TIMEOUT = 240
DEFAULT_TRANSLATOR_TIMEOUT = 240
REQUEST_PAUSE_SEC = 0.2
TRANSLATOR_RETRY_ATTEMPTS = 5
TRANSLATOR_RETRY_DELAY_SEC = 2.0
DEFAULT_TRANSLATOR_URL = "http://localhost:8000/chat"

SOURCE_ALIASES: Dict[str, List[str]] = {
    "phb": ["PHB", "PH14", "PH24", "Player's Handbook", "Players Handbook"],
    "eb": ["ERLW", "RLW", "Eberron", "Rising from the Last War", "Tasha's Cauldron of Everything", "TCE"],
    "egw": ["EGW", "Explorer's Guide to Wildemount", "Critical Role", "Wildemount"],
    "ua": ["UA", "Unearthed Arcana"],
    "xge": ["XGE", "Xanathar's Guide to Everything"],
    "tce": ["TCE", "Tasha's Cauldron of Everything"],
    "scag": ["SCAG", "Sword Coast Adventurer's Guide", "Sword Coast Adventurers Guide"],
    "bgg": ["BGG", "Bigby Presents", "Bigby"],
    "dmg": ["DMG", "Dungeon Master's Guide", "Dungeon Masters Guide"],
    "psa": ["PSA", "Amonkhet", "Plane Shift"],
    "tdcsr": ["TDCSR", "Tal'Dorei Campaign Setting Reborn", "TalDorei Campaign Setting Reborn", "Tal'Dorei", "TalDorei"],
}

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
    "10 feet": "10 футов",
    "30 feet": "30 футов",
    "60 feet": "60 футов",
    "120 feet": "120 футов",
    "a firefly or phosphorescent moss": "светлячок или фосфоресцирующий мох",
    "a small amount of makeup applied to the face as this spell is cast": "небольшое количество косметики, наносимой на лицо во время накладывания заклинания",
    "Uses": "Использования",
}

TITLE_NAME_OVERRIDES: Dict[str, str] = {
    "Unarmored Defense": "ЗАЩИТА БЕЗ ДОСПЕХОВ",
    "Primal Path": "ПУТЬ ДИКОСТИ",
    "Fast Movement": "БЫСТРОЕ ПЕРЕДВИЖЕНИЕ",
    "Feral Instinct": "ДИКИЙ ИНСТИНКТ",
    "Brutal Critical": "СИЛЬНЫЙ КРИТИЧЕСКИЙ УДАР",
    "Relentless Rage": "НЕПРЕКЛОННАЯ ЯРОСТЬ",
    "Persistent Rage": "НЕПРЕРЫВНАЯ ЯРОСТЬ",
    "Indominable Might": "НЕУКРОТИМАЯ МОЩЬ",
    "Primal Champion": "ДИКИЙ ЧЕМПИОН",
    "Spellcasting": "Использование заклинаний",
    "Arcane Recovery": "МАГИЧЕСКОЕ ВОССТАНОВЛЕНИЕ",
    "Arcane Tradition": "МАГИЧЕСКИЕ ТРАДИЦИИ",
    "Spell Mastery": "МАСТЕРСТВО ЗАКЛИНАТЕЛЯ",
    "Signature Spells": "ФИРМЕННОЕ ЗАКЛИНАНИЕ",
    "Path of the Totem Warrior": "Путь тотемного воина",
    "Frenzy": "БЕШЕНСТВО",
    "Intimidating Presence": "ПУГАЮЩЕЕ ПРИСУТСТВИЕ",
    "Retaliation": "ОТВЕТНЫЙ УДАР",
    "Spirit Walker": "ГУЛЯЮЩИЙ С ДУХАМИ",
    "Path of the Ancestral Guardian": "Путь предка-хранителя",
    "Path of the Storm Herald": "Путь буревестника",
    "Path of the Zealot": "Путь фанатика",
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
    ".unit.value",
    ".level.value",
    ".spellcasting_ability.value",
    ".effective_caster_level.value",
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
)

EN_BRACKET_RE = re.compile(r"\[([^\]]*[A-Za-z][^\]]*)\]")
SLUG_SPLIT_RE = re.compile(r"^\d+-(.+)$")
LEVEL_LINE_RE = re.compile(r"^(\d+)(?:-[\w.]+)?\s+уровень\b", re.I)
SOURCE_LINE_RE = re.compile(r"^источник\s*:\s*(.+)$", re.I)
CLASS_NAME_PATH_RE = re.compile(r"^stats(?:\.archetypes\.value\[\d+\]\.stats)?\.name\.value$")
PAGE_STOP_MARKERS = ("комментар", "галере")

CORE_CLASS_NAMES = [
    "Artificer",
    "Barbarian",
    "Bard",
    "Cleric",
    "Druid",
    "Fighter",
    "Monk",
    "Paladin",
    "Ranger",
    "Rogue",
    "Sorcerer",
    "Warlock",
    "Wizard",
    "Blood Hunter",
    "Mystic",
]


@dataclass
class FeatureEntry:
    title_ru: str
    description: str
    level: Optional[int] = None


@dataclass
class ClassSection:
    title_ru: str
    source_text: str = ""
    category_text: str = ""
    features: List[FeatureEntry] = field(default_factory=list)


@dataclass
class ClassPageData:
    title_ru: str = ""
    features: List[FeatureEntry] = field(default_factory=list)
    archetypes: List[ClassSection] = field(default_factory=list)
    spell_links: Dict[str, str] = field(default_factory=dict)


@dataclass
class ResolvedClassPage:
    url: str
    expected_en_name: str
    exact_match: bool = True


@dataclass
class SpellData:
    title_ru: str = ""
    description: str = ""
    casting_time: str = ""
    range: str = ""
    duration: str = ""
    material_components: Optional[str] = None


@dataclass
class StringRef:
    container: Any
    key: Any
    path: str
    value: str


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
    for bad in ("’", "‘", "´", "`", "ʻ", "ʼ", "′", "ʹ"):
        text = text.replace(bad, "'")
    for bad in ("“", "”", "„"):
        text = text.replace(bad, '"')
    return normalize_spaces(text)


def canonical_key(text: str) -> str:
    text = normalize_lookup_text(text)
    text = text.replace("ё", "е")
    text = re.sub(r"\s*\((?:\d+\s*фт\.?|\d+\s*ft\.?)\)\s*", "", text, flags=re.I)
    text = re.sub(r"[^\w\s'-]+", " ", text, flags=re.U)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .-_\n\t").casefold()


def strip_brackets_title(text: str) -> str:
    text = normalize_spaces(text)
    text = re.sub(r"\s*\[[^\]]+\]\s*$", "", text)
    match = re.search(r"\s*\(([^()]+)\)\s*$", text)
    if match:
        marker = normalize_spaces(match.group(1))
        upper_count = sum(1 for ch in marker if ch.isupper())
        if upper_count >= 2 and " " not in marker:
            text = text[: match.start()].strip()
    return text.strip(" -—")


def slugify_name(name: str) -> str:
    name = normalize_lookup_text(name).lower().strip()
    name = name.replace("'", "")
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def stem_title_word(word: str) -> str:
    word = canonical_key(word)
    for suffix in (
        "орожденные",
        "орождённые",
        "орожденный",
        "орождённый",
        "иями",
        "ями",
        "ами",
        "ого",
        "ему",
        "ому",
        "ых",
        "их",
        "ый",
        "ий",
        "ой",
        "ая",
        "яя",
        "ое",
        "ее",
        "ые",
        "ие",
        "ую",
        "юю",
        "ым",
        "им",
        "ом",
        "ем",
        "es",
        "s",
        "ы",
        "и",
        "а",
        "я",
        "о",
        "е",
        "у",
        "ю",
    ):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def fuzzy_title_key(text: str) -> str:
    return " ".join(stem_title_word(part) for part in canonical_key(text).split())


def titles_match(left: str, right: str) -> bool:
    if canonical_key(left) == canonical_key(right):
        return True
    return fuzzy_title_key(left) == fuzzy_title_key(right)


def extract_slug_from_href(href: str) -> str:
    last = href.rstrip("/").split("/")[-1]
    match = SLUG_SPLIT_RE.match(last)
    return match.group(1) if match else last


def looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_lookup_text(text or "")))


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


def text_contains_alias(text: str, alias: str) -> bool:
    return canonical_key(alias) in canonical_key(text)


def extract_spell_lookup_name(text: str) -> str:
    norm = normalize_lookup_text(text)
    match = EN_BRACKET_RE.search(norm)
    if match:
        inner = normalize_lookup_text(match.group(1))
        if looks_english(inner):
            return inner
    return norm


def detect_class_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    stem = path.name
    if stem.endswith(".rpg.json"):
        stem = stem[:-9]
    if stem.startswith("class_"):
        stem = stem[6:]

    stem = re.sub(r"__[^_]+_?$", "", stem)
    parts = [part for part in stem.split("_") if part]

    candidates: List[str] = []
    if len(parts) >= 2 and len(parts) % 2 == 0:
        half = len(parts) // 2
        if parts[:half] == parts[half:]:
            candidates.append(" ".join(parts[:half]))
    if parts:
        candidates.extend([" ".join(parts), parts[-1], parts[0]])
    candidates.append(stem.replace("_", " "))

    seen: set[str] = set()
    for candidate in candidates:
        candidate = normalize_lookup_text(candidate)
        key = canonical_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if looks_english(candidate):
            return candidate

    raise RuntimeError(
        f"Не удалось восстановить английское имя класса для dnd.su из файла {path.name!r}"
    )


def build_parent_class_candidates(class_en_name: str) -> List[str]:
    class_en_name = normalize_lookup_text(class_en_name)
    class_key = canonical_key(class_en_name)
    candidates: List[str] = []

    stripped = normalize_spaces(re.sub(r"\s*\([^)]*\)\s*$", "", class_en_name))
    if stripped and stripped != class_en_name:
        candidates.append(stripped)

    for core_name in CORE_CLASS_NAMES:
        core_key = canonical_key(core_name)
        if core_key and core_key != class_key and re.search(rf"\b{re.escape(core_key)}\b", class_key):
            candidates.append(core_name)

    words = [part for part in stripped.split() if part]
    if len(words) >= 2:
        candidates.append(words[-1])

    result: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = canonical_key(candidate)
        if not key or key == class_key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


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
        pending = list(
            dict.fromkeys(
                norm for _, norm in normalized_pairs if norm and norm not in self._cache
            )
        )

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
        print(json.dumps(payload, ensure_ascii=False))
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = self.session.post(self.url, json=payload, timeout=(10, self.timeout))
                response.raise_for_status()
                data = response.json()
                print(json.dumps(data, ensure_ascii=False))
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
        return isinstance(
            exc,
            (
                requests.exceptions.RequestException,
                json.JSONDecodeError,
                RuntimeError,
            ),
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


class DndSuClient:
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
        self._class_index: Optional[List[Dict[str, Any]]] = None
        self._spell_index: Optional[List[Dict[str, Any]]] = None

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

    def build_class_index(self) -> List[Dict[str, Any]]:
        if self._class_index is not None:
            return self._class_index

        soup = self.get_soup(urljoin(BASE_URL, "/class/"))
        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, anchor["href"])
            if "/class/" not in href:
                continue
            if not href.startswith(BASE_URL):
                continue
            text = normalize_spaces(anchor.get_text(" ", strip=True))
            if not text or href in seen:
                continue
            seen.add(href)
            entries.append(
                {
                    "href": href,
                    "text": text,
                    "slug": extract_slug_from_href(href),
                }
            )

        self._class_index = entries
        return entries

    def _filter_candidates_by_source(
        self,
        candidates: List[Dict[str, Any]],
        source_code: str,
    ) -> List[Dict[str, Any]]:
        aliases = SOURCE_ALIASES.get(source_code.lower(), [source_code])
        matched = [
            candidate
            for candidate in candidates
            if any(text_contains_alias(candidate["text"], alias) for alias in aliases)
        ]
        return matched or candidates

    def resolve_class_page(
        self,
        class_en_name: str,
        source_code: str,
        parent_class_candidates: Optional[Sequence[str]] = None,
    ) -> ResolvedClassPage:
        expected_slug = slugify_name(class_en_name)
        entries = self.build_class_index()

        exact_candidates = [entry for entry in entries if entry["slug"] == expected_slug]
        exact_candidates = self._filter_candidates_by_source(exact_candidates, source_code)
        if exact_candidates:
            return ResolvedClassPage(
                url=exact_candidates[0]["href"],
                expected_en_name=class_en_name,
                exact_match=True,
            )

        exact_by_text = [
            entry
            for entry in entries
            if text_contains_alias(entry["text"], class_en_name)
        ]
        exact_by_text = self._filter_candidates_by_source(exact_by_text, source_code)
        if exact_by_text:
            return ResolvedClassPage(
                url=exact_by_text[0]["href"],
                expected_en_name=class_en_name,
                exact_match=True,
            )

        for parent_name in parent_class_candidates or []:
            parent_slug = slugify_name(parent_name)
            parent_candidates = [entry for entry in entries if entry["slug"] == parent_slug]
            if parent_candidates:
                return ResolvedClassPage(
                    url=parent_candidates[0]["href"],
                    expected_en_name=parent_name,
                    exact_match=False,
                )

        raise RuntimeError(f"Не найден URL dnd.su для класса {class_en_name!r}")

    def build_spell_index(self) -> List[Dict[str, Any]]:
        if self._spell_index is not None:
            return self._spell_index

        soup = self.get_soup(urljoin(BASE_URL, "/spells/"))
        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, anchor["href"])
            if "/spells/" not in href:
                continue
            if "/homebrew/" in href:
                continue
            if not href.startswith(BASE_URL):
                continue
            text = normalize_spaces(anchor.get_text(" ", strip=True))
            if not text or href in seen:
                continue
            seen.add(href)
            en_name = ""
            match = EN_BRACKET_RE.search(text)
            if match:
                en_name = normalize_lookup_text(match.group(1))
            entries.append(
                {
                    "href": href,
                    "text": text,
                    "slug": extract_slug_from_href(href),
                    "en_name": en_name,
                }
            )

        self._spell_index = entries
        return entries

    def resolve_spell_url(self, spell_name: str) -> Optional[str]:
        lookup = extract_spell_lookup_name(spell_name)
        if not lookup:
            return None

        entries = self.build_spell_index()
        lookup_key = canonical_key(lookup)
        slug = slugify_name(lookup)

        for entry in entries:
            if entry.get("en_name") and canonical_key(entry["en_name"]) == lookup_key:
                return entry["href"]
        for entry in entries:
            if entry.get("slug") == slug:
                return entry["href"]
        return None

    def parse_class_page(self, url: str, expected_en_name: str) -> ClassPageData:
        soup = self.get_soup(url)
        result = ClassPageData()
        result.spell_links = self._extract_spell_links(soup)

        elements: List[Tuple[str, str]] = []
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            text = normalize_spaces(tag.get_text(" ", strip=True))
            if text:
                elements.append((tag.name.lower(), text))

        start_index: Optional[int] = None
        expected_lower = expected_en_name.lower()
        for index, (tag_name, text) in enumerate(elements):
            if tag_name != "h2":
                continue
            lower = text.lower()
            if f"[{expected_lower}]" in lower or canonical_key(expected_en_name) in canonical_key(text):
                start_index = index
                result.title_ru = strip_brackets_title(text)
                break

        if start_index is None:
            raise RuntimeError(f"Не удалось найти начало страницы класса {expected_en_name!r}")

        class_features_index: Optional[int] = None
        for index in range(start_index + 1, len(elements)):
            tag_name, text = elements[index]
            if tag_name == "h2" and "классовые умения" in text.lower():
                class_features_index = index
                break

        if class_features_index is None:
            return result

        next_h2 = len(elements)
        for index in range(class_features_index + 1, len(elements)):
            tag_name, _ = elements[index]
            if tag_name == "h2":
                next_h2 = index
                break
        result.features = self._collect_feature_entries(elements, class_features_index + 1, next_h2)

        current_group_title = ""
        index = next_h2
        while index < len(elements):
            tag_name, text = elements[index]
            lower = text.lower()
            if tag_name != "h2":
                index += 1
                continue
            if any(marker in lower for marker in PAGE_STOP_MARKERS):
                break

            section_end = len(elements)
            for inner in range(index + 1, len(elements)):
                if elements[inner][0] == "h2":
                    section_end = inner
                    break

            title_ru = strip_brackets_title(text)
            source_text = self._extract_section_source(elements, index + 1, section_end)
            features = self._collect_feature_entries(elements, index + 1, section_end)

            if features:
                result.archetypes.append(
                    ClassSection(
                        title_ru=title_ru,
                        source_text=source_text,
                        category_text=current_group_title,
                        features=features,
                    )
                )
            else:
                current_group_title = title_ru

            index = section_end

        return result

    def _extract_section_source(
        self,
        elements: Sequence[Tuple[str, str]],
        start: int,
        end: int,
    ) -> str:
        for index in range(start, end):
            tag_name, text = elements[index]
            if tag_name == "h3":
                break
            if tag_name not in {"p", "li"}:
                continue
            match = SOURCE_LINE_RE.match(text)
            if match:
                return normalize_spaces(match.group(1))
        return ""

    def _collect_feature_entries(
        self,
        elements: Sequence[Tuple[str, str]],
        start: int,
        end: int,
    ) -> List[FeatureEntry]:
        entries: List[FeatureEntry] = []
        current_title: Optional[str] = None
        current_lines: List[str] = []
        current_level: Optional[int] = None

        def flush() -> None:
            nonlocal current_title, current_lines, current_level
            if current_title and current_lines:
                entries.append(
                    FeatureEntry(
                        title_ru=current_title,
                        description=normalize_spaces("\n\n".join(line for line in current_lines if line)),
                        level=current_level,
                    )
                )
            current_title = None
            current_lines = []
            current_level = None

        def find_following_level(index: int) -> Optional[int]:
            for probe in range(index + 1, end):
                tag_name, text = elements[probe]
                if tag_name == "p":
                    return parse_level_line(text)
                if tag_name in {"h2", "h3"}:
                    return None
            return None

        for index in range(start, end):
            tag_name, text = elements[index]

            if tag_name == "h3":
                heading_level = find_following_level(index)
                if heading_level is not None:
                    flush()
                    current_title = normalize_spaces(text)
                    current_level = heading_level
                    continue
                if current_title:
                    current_lines.append(text)
                continue

            if tag_name == "h4":
                if current_title:
                    current_lines.append(text)
                continue

            if tag_name in {"p", "li"}:
                if not current_title:
                    continue
                if not current_lines and tag_name == "p" and parse_level_line(text) is not None:
                    if current_level is None:
                        current_level = parse_level_line(text)
                    continue
                current_lines.append(text)

        flush()
        return entries

    def _extract_spell_links(self, soup: BeautifulSoup) -> Dict[str, str]:
        links: Dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "/spells/" not in href:
                continue
            text = normalize_spaces(anchor.get_text(" ", strip=True))
            match = EN_BRACKET_RE.search(text)
            if not match:
                continue
            en_name = normalize_lookup_text(match.group(1)).lower()
            links[en_name] = urljoin(BASE_URL, href)
        return links

    def parse_spell_page(self, url: str, expected_en_name: Optional[str] = None) -> SpellData:
        soup = self.get_soup(url)
        result = SpellData()

        elements: List[Tuple[str, str]] = []
        for tag in soup.find_all(["h2", "p", "li"]):
            text = normalize_spaces(tag.get_text(" ", strip=True))
            if text:
                elements.append((tag.name.lower(), text))

        started = False
        desc_lines: List[str] = []
        for tag_name, text in elements:
            lower = text.lower()
            if not started:
                if tag_name == "h2":
                    if expected_en_name is None or f"[{expected_en_name.lower()}]" in lower:
                        result.title_ru = strip_brackets_title(text)
                        started = True
                continue

            if tag_name == "h2" and "комментар" in lower:
                break

            if tag_name == "li":
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
                    result.material_components = self._extract_material_components(
                        normalize_spaces(text.split(":", 1)[1])
                    )
                    continue
                if text.startswith("Классы:") or text.startswith("Подклассы:"):
                    continue
                if re.match(r"^(Заговор|\d+\s+уровень)", text):
                    continue
                desc_lines.append(text)
                continue

            if tag_name == "p":
                desc_lines.append(text)

        if desc_lines:
            result.description = normalize_spaces("\n\n".join(desc_lines))
        return result

    @staticmethod
    def _extract_material_components(components_line: str) -> Optional[str]:
        match = re.search(r"\((.+)\)", components_line)
        if match:
            return normalize_spaces(match.group(1))
        return None


def parse_level_line(text: str) -> Optional[int]:
    match = LEVEL_LINE_RE.match(normalize_lookup_text(text).lower())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
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


def extract_feature_primary_level(stats: Dict[str, Any]) -> Optional[int]:
    levels: List[int] = []
    for desc in stats.get("descriptions", {}).get("value", []):
        level_value = desc.get("stats", {}).get("level", {}).get("value")
        if isinstance(level_value, int):
            levels.append(level_value)
    for effect in stats.get("effects", {}).get("value", []):
        level_value = effect.get("stats", {}).get("level", {}).get("value")
        if isinstance(level_value, int):
            levels.append(level_value)
    return min(levels) if levels else None


def patch_effect_names(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "name" and isinstance(value, dict) and isinstance(value.get("value"), str):
                translated = translate_static_text(value["value"])
                if translated:
                    value["value"] = translated
            else:
                patch_effect_names(value)
    elif isinstance(obj, list):
        for item in obj:
            patch_effect_names(item)


def patch_choice_texts(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "options" and isinstance(value, dict) and isinstance(value.get("value"), str):
                translated = translate_static_text(value["value"])
                if translated:
                    value["value"] = translated
            else:
                patch_choice_texts(value)
    elif isinstance(obj, list):
        for item in obj:
            patch_choice_texts(item)


def patch_known_leaf_translations(obj: Any) -> None:
    translatable_leaf_keys = {
        "name",
        "description",
        "higher_level_description",
        "ru_name",
        "options",
        "components",
        "casting_time",
        "range",
        "duration",
    }
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in translatable_leaf_keys and isinstance(value, dict) and isinstance(value.get("value"), str):
                translated = translate_static_text(value["value"])
                if translated:
                    value["value"] = translated
            else:
                patch_known_leaf_translations(value)
    elif isinstance(obj, list):
        for item in obj:
            patch_known_leaf_translations(item)


def collect_class_title_texts(stats: Dict[str, Any]) -> List[str]:
    texts: List[str] = []

    def add_name(value: Optional[str]) -> None:
        if not value:
            return
        value = normalize_lookup_text(value)
        if looks_english(value) and value not in texts:
            texts.append(value)

    add_name(stats.get("name", {}).get("value"))

    for feature in stats.get("features", {}).get("value", []):
        add_name(feature.get("stats", {}).get("name", {}).get("value"))

    for archetype in stats.get("archetypes", {}).get("value", []):
        archetype_stats = archetype.get("stats", {})
        add_name(archetype_stats.get("name", {}).get("value"))
        for feature in archetype_stats.get("features", {}).get("value", []):
            add_name(feature.get("stats", {}).get("name", {}).get("value"))

    return texts


def set_ru_name(stats: Dict[str, Any], value: Optional[str]) -> None:
    if not value:
        return
    stats["ru_name"] = {"value": normalize_lookup_text(value)}


def seed_class_ru_name(
    stats: Dict[str, Any],
    title_translation_map: Dict[str, str],
) -> None:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if not current_name:
        return
    translated = TITLE_NAME_OVERRIDES.get(current_name) or title_translation_map.get(current_name) or current_name
    set_ru_name(stats, translated)


def build_title_translation_map(
    stats: Dict[str, Any],
    translator: Optional[TranslatorClient],
) -> Dict[str, str]:
    texts = collect_class_title_texts(stats)
    if not texts or not translator or not translator.enabled:
        return {}
    translated = translator.translate_many(texts)
    return {
        normalize_lookup_text(source): normalize_spaces(target)
        for source, target in translated.items()
        if isinstance(target, str) and target.strip()
    }


def resolve_title_candidates(
    name: str,
    title_translation_map: Dict[str, str],
) -> List[str]:
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

    norm = normalize_lookup_text(name)
    add(TITLE_NAME_OVERRIDES.get(norm))
    add(translate_static_text(norm))
    add(title_translation_map.get(norm))
    stripped = strip_brackets_title(norm)
    if stripped != norm:
        add(title_translation_map.get(stripped))
        add(stripped)
    add(norm)
    return candidates


def find_feature_entry(
    entries: Sequence[FeatureEntry],
    candidate_names: Sequence[str],
    primary_level: Optional[int] = None,
) -> Optional[FeatureEntry]:
    matches = [
        entry
        for entry in entries
        if any(titles_match(entry.title_ru, candidate) for candidate in candidate_names)
    ]
    if not matches:
        return None
    if primary_level is not None:
        level_matches = [entry for entry in matches if entry.level == primary_level]
        if level_matches:
            return level_matches[0]
    return matches[0]


def find_feature_entry_index(
    entries: Sequence[FeatureEntry],
    candidate_names: Sequence[str],
    primary_level: Optional[int] = None,
    skip_indices: Optional[set[int]] = None,
) -> Optional[int]:
    matches: List[Tuple[int, FeatureEntry]] = [
        (index, entry)
        for index, entry in enumerate(entries)
        if (skip_indices is None or index not in skip_indices)
        and any(titles_match(entry.title_ru, candidate) for candidate in candidate_names)
    ]
    if not matches:
        return None
    if primary_level is not None:
        level_matches = [index for index, entry in matches if entry.level == primary_level]
        if level_matches:
            return level_matches[0]
    return matches[0][0]


def is_order_match_candidate(stats: Dict[str, Any]) -> bool:
    name = normalize_lookup_text(stats.get("name", {}).get("value", "")).lower()
    if "optional feature" in name or "optional features" in name:
        return False

    selectable = stats.get("selectable_features", {}).get("value", [])
    descriptions = stats.get("descriptions", {}).get("value", [])

    if selectable and not descriptions:
        return False
    if len(descriptions) > 1 and extract_feature_primary_level(stats) is None:
        return False
    return True


def apply_feature_match(stats: Dict[str, Any], entry: FeatureEntry) -> None:
    if "name" in stats and isinstance(stats["name"], dict) and "value" in stats["name"]:
        stats["name"]["value"] = entry.title_ru
    desc_list = stats.get("descriptions", {}).get("value", [])
    if desc_list:
        first_stats = desc_list[0].get("stats", {})
        if "description" in first_stats and isinstance(first_stats["description"], dict):
            first_stats["description"]["value"] = entry.description


def filter_sections_by_source(
    sections: Sequence[ClassSection],
    source_code: str,
) -> List[ClassSection]:
    aliases = SOURCE_ALIASES.get(source_code.lower(), [source_code])
    filtered: List[ClassSection] = []
    for section in sections:
        haystack = normalize_lookup_text(f"{section.source_text} {section.category_text}")
        if any(text_contains_alias(haystack, alias) for alias in aliases):
            filtered.append(section)
    return filtered or list(sections)


def find_archetype_section(
    archetype_stats: Dict[str, Any],
    sections: Sequence[ClassSection],
    title_translation_map: Dict[str, str],
) -> Optional[ClassSection]:
    current_name = normalize_lookup_text(archetype_stats.get("name", {}).get("value", ""))
    source_code = normalize_lookup_text(archetype_stats.get("source", {}).get("value", "")).lower()
    candidate_names = resolve_title_candidates(current_name, title_translation_map)

    matches = [
        section
        for section in sections
        if any(titles_match(section.title_ru, candidate) for candidate in candidate_names)
    ]
    if not matches:
        return None

    if source_code:
        matches = filter_sections_by_source(matches, source_code)
    return matches[0] if matches else None


def sync_features_from_page(
    features: List[Dict[str, Any]],
    page_entries: Sequence[FeatureEntry],
    title_translation_map: Dict[str, str],
    missing: List[str],
    fallback_entries: Optional[Sequence[FeatureEntry]] = None,
) -> None:
    unresolved: List[Tuple[Dict[str, Any], Dict[str, Any], List[str], Optional[int]]] = []
    used_primary_indices: set[int] = set()

    for feature in features:
        stats = feature.get("stats", {})
        if not stats:
            continue

        current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
        if not current_name:
            continue

        candidates = resolve_title_candidates(current_name, title_translation_map)
        primary_level = extract_feature_primary_level(stats)
        found_index = find_feature_entry_index(
            page_entries,
            candidates,
            primary_level=primary_level,
            skip_indices=used_primary_indices,
        )
        if found_index is not None:
            used_primary_indices.add(found_index)
            apply_feature_match(stats, page_entries[found_index])
        else:
            unresolved.append((feature, stats, candidates, primary_level))

        patch_effect_names(stats)

    remaining_primary = [
        (index, entry)
        for index, entry in enumerate(page_entries)
        if index not in used_primary_indices
    ]
    order_pointer = 0
    still_unresolved: List[Tuple[Dict[str, Any], Dict[str, Any], List[str], Optional[int]]] = []

    for feature, stats, candidates, primary_level in unresolved:
        if is_order_match_candidate(stats) and order_pointer < len(remaining_primary):
            _, entry = remaining_primary[order_pointer]
            order_pointer += 1
            apply_feature_match(stats, entry)
            patch_effect_names(stats)
            continue
        still_unresolved.append((feature, stats, candidates, primary_level))

    unresolved = still_unresolved

    if fallback_entries and unresolved:
        used_fallback_indices: set[int] = set()
        fallback_remaining: List[Tuple[Dict[str, Any], Dict[str, Any], List[str], Optional[int]]] = []
        for feature, stats, candidates, primary_level in unresolved:
            found_index = find_feature_entry_index(
                fallback_entries,
                candidates,
                primary_level=primary_level,
                skip_indices=used_fallback_indices,
            )
            if found_index is not None:
                used_fallback_indices.add(found_index)
                apply_feature_match(stats, fallback_entries[found_index])
            else:
                fallback_remaining.append((feature, stats, candidates, primary_level))
            patch_effect_names(stats)
        unresolved = fallback_remaining

    for _, stats, candidates, _ in unresolved:
        current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
        translated_candidate = next((value for value in candidates if not looks_english(value)), None)
        if translated_candidate and isinstance(stats.get("name"), dict) and "value" in stats["name"]:
            stats["name"]["value"] = translated_candidate
        missing.append(f"feature:{stats.get('id', current_name)}:{current_name}")
        patch_effect_names(stats)


def collect_spell_stat_blocks(obj: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        if set(obj.keys()) >= {"name", "casting_time", "range", "duration", "description"} and all(
            isinstance(obj.get(key), dict)
            for key in ("name", "casting_time", "range", "duration", "description")
        ):
            found.append(obj)
        for value in obj.values():
            found.extend(collect_spell_stat_blocks(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(collect_spell_stat_blocks(item))
    return found


def patch_spells_from_class_page(
    stats: Dict[str, Any],
    page: ClassPageData,
    client: DndSuClient,
    missing: List[str],
) -> None:
    for spell_stats in collect_spell_stat_blocks(stats):
        raw_spell_name = normalize_lookup_text(spell_stats.get("name", {}).get("value", ""))
        if not raw_spell_name:
            continue

        spell_lookup_name = extract_spell_lookup_name(raw_spell_name)

        url = page.spell_links.get(spell_lookup_name.lower())
        if not url:
            url = client.resolve_spell_url(spell_lookup_name)

        if not url:
            translated_static = translate_static_text(raw_spell_name) or translate_static_text(spell_lookup_name)
            if translated_static:
                spell_stats["name"]["value"] = translated_static
            else:
                missing.append(f"spell:{raw_spell_name}")
            for field_name in ("casting_time", "range", "duration", "components"):
                if field_name in spell_stats and isinstance(spell_stats[field_name], dict):
                    current_value = spell_stats[field_name].get("value")
                    if isinstance(current_value, str):
                        translated = translate_static_text(current_value)
                        if translated:
                            spell_stats[field_name]["value"] = translated
            continue

        try:
            expected_name = spell_lookup_name if looks_english(spell_lookup_name) else None
            spell_data = client.parse_spell_page(url, expected_en_name=expected_name)
        except Exception as exc:
            missing.append(f"spell_fetch:{raw_spell_name}:{exc}")
            continue

        if spell_data.title_ru and "name" in spell_stats and "value" in spell_stats["name"]:
            spell_stats["name"]["value"] = spell_data.title_ru
        if spell_data.casting_time and "casting_time" in spell_stats:
            spell_stats["casting_time"]["value"] = spell_data.casting_time
        if spell_data.range and "range" in spell_stats:
            spell_stats["range"]["value"] = spell_data.range
        if spell_data.duration and "duration" in spell_stats:
            spell_stats["duration"]["value"] = spell_data.duration
        if spell_data.material_components and "components" in spell_stats:
            spell_stats["components"]["value"] = spell_data.material_components
        if spell_data.description and "description" in spell_stats:
            spell_stats["description"]["value"] = spell_data.description


def is_translatable_string_path(path: str) -> bool:
    if CLASS_NAME_PATH_RE.match(path):
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
    client: DndSuClient,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_json(path)
    doc = copy.deepcopy(original)
    stats = get_top_stats(doc)

    source_code = normalize_lookup_text(stats.get("source", {}).get("value", "")).lower()
    if not source_code:
        raise RuntimeError(f"В {path.name} не удалось определить source")

    class_en_name = detect_class_lookup_name(path, stats)
    parent_class_candidates = build_parent_class_candidates(class_en_name)
    missing: List[str] = []

    patch_choice_texts(stats)
    patch_known_leaf_translations(stats)

    title_translation_map = build_title_translation_map(stats, translator)
    seed_class_ru_name(stats, title_translation_map)
    for archetype in stats.get("archetypes", {}).get("value", []):
        seed_class_ru_name(archetype.get("stats", {}), title_translation_map)

    page: Optional[ClassPageData] = None
    resolved_page: Optional[ResolvedClassPage] = None
    try:
        resolved_page = client.resolve_class_page(
            class_en_name,
            source_code,
            parent_class_candidates=parent_class_candidates,
        )
        page = client.parse_class_page(
            resolved_page.url,
            expected_en_name=resolved_page.expected_en_name,
        )
    except Exception as exc:
        missing.append(f"class_page:{class_en_name}:{exc}")

    if page is not None:
        if resolved_page and resolved_page.exact_match and page.title_ru:
            set_ru_name(stats, page.title_ru)

        sync_features_from_page(
            stats.get("features", {}).get("value", []),
            page.features,
            title_translation_map,
            missing,
        )

        for archetype in stats.get("archetypes", {}).get("value", []):
            archetype_stats = archetype.get("stats", {})
            section = find_archetype_section(archetype_stats, page.archetypes, title_translation_map)
            if section:
                set_ru_name(archetype_stats, section.title_ru)
            sync_features_from_page(
                archetype_stats.get("features", {}).get("value", []),
                section.features if section else [],
                title_translation_map,
                missing,
                fallback_entries=page.features,
            )
            patch_effect_names(archetype_stats)

        patch_spells_from_class_page(stats, page, client, missing)

    patch_known_leaf_translations(stats)
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
    parser = argparse.ArgumentParser(description="Переносит переводы классов из dnd.su в class_*.json")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./class_*.json")
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

    client = DndSuClient(cache_dir=Path(args.cache_dir))
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
