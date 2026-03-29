#!/usr/bin/env python3
"""
Переводчик race_*.json по формулировкам с dnd.su.

Что делает:
- не трогает id, resource_id, source, enum'ы и числовые поля;
- подтягивает русское название расы/подрасы и тексты особенностей с dnd.su;
- переводит вложенные заклинания, если на странице расы есть ссылка на страницу заклинания;
- если строку не удалось уверенно сопоставить с dnd.su, использует fallback-переводчик;
- нормализует проблемные апострофы и shell-экранирование вроде Jorasco'\''s.

Зависимости:
    pip install requests beautifulsoup4

Пример:
    python dndsu_race_json_translator.py ./race_*.json --out-dir ./translated
    python dndsu_race_json_translator.py ./race_aasimar_aasimar.rpg.json --in-place
"""

from __future__ import annotations

import argparse
import copy
import glob
import html
import json
import os
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
DEFAULT_TRANSLATOR_URL = "http://localhost:8000/chat/"

# Источники в JSON -> аббревиатуры/алиасы на dnd.su
SOURCE_ALIASES: Dict[str, List[str]] = {
    "phb": ["PH14", "PH24", "PHB"],
    "vgm": ["VGM"],
    "mom": ["MPMM"],
    "aag": ["SAS"],  # Spelljammer: Adventures in Space
    "eepc": ["POA", "EEPC"],  # Aarakocra на dnd.su лежит в Princes of the Apocalypse
    "erlw": ["ERLW", "RLW", "VGM"],
    "eb": ["RLW", "ERLW", "EB"],
    "mtf": ["MTF"],
    "ggr": ["GGR"],
    "psa": ["PSA", "Amonkhet"],
}

MULTIVERSE_SOURCES = {"mom", "aag"}

# Безопасные точечные переводы для строк, которых нет в явном виде как отдельного блока на странице.
STATIC_TEXT_MAP: Dict[str, str] = {
    "Common": "Общий",
    "Celestial": "Небесный",
    "Dwarvish": "Дварфийский",
    "Goblin": "Гоблинский",
    "Auran": "Ауран",
    "Aarakocra": "Ааракокр",
    "Abyssal": "Бездны",
    "Halfling": "Полурослик",
    "Minotaur": "Минотавр",
    "Horns": "Рога",
    "Uncommon": "Редкий",
    "Uses left": "Осталось использований",
    "Unarmed Strike": "Безоружный удар",
    "Touch": "Касание",
    "Self": "На себя",
    "1 action": "1 действие",
    "1 hour": "1 час",
    "Instantaneous": "Мгновенная",
    "Concentration, up to 1 minute": "Концентрация, вплоть до 1 минуты",
    "10 feet": "10 футов",
    "Up to 1 hour": "Вплоть до 1 часа",
    "a firefly or phosphorescent moss": "светлячок или фосфоресцирующий мох",
    "a small amount of makeup applied to the face as this spell is cast": "небольшое количество косметики, наносимой на лицо во время накладывания заклинания",
    "Smith's supplies, Brewer's supplies, Mason's supplies": "Инструменты кузнеца, пивовара или каменщика",
}

# Если в JSON поле ещё на английском, но id известен, можем сопоставить с названием на dnd.su.
ID_NAME_OVERRIDES: Dict[str, str] = {
    "flight": "Полёт",
    "talons": "Когти",
    "wind_caller": "Зовущий ветер",
    "breath_weapon": "Оружие дыхания",
    "darkvision": "Тёмное зрение",
    "darkvision__60_ft._": "Тёмное зрение (60 фт.)",
    "superior_darkvision__120_ft._": "Превосходное тёмное зрение (120 фт.)",
    "celestial_resistance": "Небесное сопротивление",
    "healing_hands": "Исцеляющие руки",
    "light_bearer": "Несущий свет",
    "light_bearer_": "Несущий свет",
    "celestial_revelation": "Небесное откровение",
    "radiant_soul": "Сияющая душа",
    "radiant_consumption": "Испускание сияния",
    "necrotic_shroud": "Саван смерти",
    "creature_type": "Вид существа",
    "construct": "Вид существа",
    "fey_ancestry": "Наследие фей",
    "starlight_step": "Звёздный шаг",
    "astral_trance": "Астральный транс",
    "built_for_success": "Создан для успеха",
    "healing_machine": "Лечащая машина",
    "mechanical_nature": "Механическая природа",
    "sentrys_rest": "Отдых стража",
    "long-limbed": "Длинные конечности",
    "powerful_build": "Мощное телосложение",
    "sneaky": "Скрытность",
    "surprise_attack": "Внезапное нападение",
    "dwarven_resilience": "Дварфийская устойчивость",
    "dwarven_combat_training": "Дварфийская боевая тренировка",
    "tool_proficiency": "Владение инструментами",
    "stonecunning": "Знание камня",
    "dwarven_toughness": "Дварфийская выдержка",
    "dwarven_armor_training": "Владение доспехами дварфов",
    "master_of_locks": "Мастер замков",
    "wards_and_seals": "Обереги и печати",
    "duergar_resilience": "Дуэргарская устойчивость",
    "duergar_magic": "Дуэргарская магия",
    "sunlight_sensitivity": "Чувствительность к солнечному свету",
    "warders_intuition": "Интуиция надзирателя",
    "spells_of_the_mark": "Заклинания метки",
    "protector_aasimar": "Аасимар-защитник",
    "scourge_aasimar": "Аасимар-каратель",
    "fallen_aasimar": "Падший аасимар",
    "monstrous_reputation": "Чудовищная репутация",
    "gore": "Бодание",
    "lucky": "Везучий",
    "brave": "Храбрый",
    "hafling_nimbleness": "Проворство полурослика",
    "halfling_nimbleness": "Проворство полурослика",
    "silent_speech": "Безмолвная речь",
    "naturally_stealthy": "Природная скрытность",
    "stout_resilience": "Устойчивость коренастых",
    "medical_intuition": "Медицинская интуиция",
    "healing_touch": "Исцеляющее прикосновение",
    "jorascos_blessing": "Благословение Джораско",
    "innkeepers_charms": "Чары трактирщика",
    "ever_hospitable": "Неизменно гостеприимный",
    "child_of_the_wood": "Дитя леса",
    "timberwalk": "Лесная поступь",
    "forceful_presence": "Довлеющее присутствие",
    "vengeful_assault": "Мстительное нападение",
    "ability_score_increase": "Увеличение характеристик",
    "chromatic_breath_weapon": "Оружие дыхания",
    "chromatic_resistance": "Сопротивление дракона",
    "chromatic_warding": "Защита цветного дракона",
    "gem_breath_weapon": "Оружие дыхания",
    "gem_resistance": "Сопротивление дракона",
    "gem_psionic_mind": "Псионический разум",
    "gem_flight": "Полёт самоцветного дракона",
    "metallic_resistance": "Сопротивление дракона",
}

EN_NAME_OVERRIDES: Dict[str, str] = {
    "Darkvision (60 ft.)": "Тёмное зрение (60 фт.)",
    "Celestial Resistance": "Небесное сопротивление",
    "Healing Hands": "Исцеляющие руки",
    "Light Bearer": "Несущий свет",
    "Protector Aasimar": "Аасимар-защитник",
    "Scourge Aasimar": "Аасимар-каратель",
    "Fallen Aasimar": "Падший аасимар",
    "Radiant Soul": "Сияющая душа",
    "Radiant Consumption": "Испускание сияния",
    "Necrotic Shroud": "Саван смерти",
    "Monstrous Reputation": "Чудовищная репутация",
    "Gore": "Бодание",
    "Lucky": "Везучий",
    "Brave": "Храбрый",
    "Hafling Nimbleness": "Проворство полурослика",
    "Halfling Nimbleness": "Проворство полурослика",
    "Silent Speech": "Безмолвная речь",
    "Naturally Stealthy": "Природная скрытность",
    "Stout Resilience": "Устойчивость коренастых",
    "Medical Intuition": "Медицинская интуиция",
    "Healing Touch": "Исцеляющее прикосновение",
    "Jorasco's Blessing": "Благословение Джораско",
    "Innkeeper's Charms": "Чары трактирщика",
    "Ever Hospitable": "Неизменно гостеприимный",
    "Forceful Presence": "Довлеющее присутствие",
    "Vengeful Assault": "Мстительное нападение",
    "Dragonborn (Draconblood)": "Драконокровный",
    "Dragonborn (Ravenite)": "Равенит",
    "Metallic Dragonborn": "Металлические драконорождённые",
    "Gem Dragonborn": "Самоцветные драконорождённые",
    "Chromatic Dragonborn": "Цветные драконорождённые",
    "Chromatic Warding": "Защита цветного дракона",
    "Gem Flight": "Полёт самоцветного дракона",
    "Gem Psionic Mind": "Псионический разум",
    "Brass Ancestry": "Латунное наследие",
    "Bronze Ancestry": "Бронзовое наследие",
    "Copper Ancestry": "Медное наследие",
    "Gold Ancestry": "Золотое наследие",
    "Silver Ancestry": "Серебряное наследие",
    "Black Ancestry": "Чёрное наследие",
    "Blue Ancestry": "Синее наследие",
    "Green Ancestry": "Зелёное наследие",
    "Red Ancestry": "Красное наследие",
    "White Ancestry": "Белое наследие",
    "Amethyst": "Аметист",
    "Crystal": "Кристалл",
    "Emerald": "Изумруд",
    "Sapphire": "Сапфир",
    "Topaz": "Топаз",
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
}

TRANSLATABLE_SUFFIXES = (
    ".name.value",
    ".description.value",
    ".options.value",
    ".components.value",
    ".casting_time.value",
    ".range.value",
    ".duration.value",
    ".ru_name.value",
)

FEATURE_START_RE = re.compile(r"^([А-ЯЁA-Z][^.]{0,120})\.\s*(.+)$")
EN_BRACKET_RE = re.compile(r"\[([^\]]*[A-Za-z][^\]]*)\]")
SLUG_SPLIT_RE = re.compile(r"^\d+-(.+)$")


@dataclass
class PageFeatureSet:
    title_ru: str = ""
    features: Dict[str, str] = field(default_factory=dict)
    subraces: Dict[str, Dict[str, str]] = field(default_factory=dict)
    spell_links: Dict[str, str] = field(default_factory=dict)


@dataclass
class ResolvedRacePage:
    url: str
    expected_en_name: str


@dataclass
class SpellData:
    title_ru: str = ""
    casting_time: Optional[str] = None
    range: Optional[str] = None
    duration: Optional[str] = None
    material_components: Optional[str] = None
    description: Optional[str] = None


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

    # bash/shell-представление одинарной кавычки: '\'' -> '
    text = text.replace("'\\''", "'")
    text = text.replace("\\'", "'")

    # типографские и похожие апострофы
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("´", "'")
        .replace("`", "'")
        .replace("ʼ", "'")
    )

    text = text.replace("“", '"').replace("”", '"')
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
        "ыми",
        "ими",
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


def build_parent_race_candidates(race_en_name: str) -> List[str]:
    race_en_name = normalize_lookup_text(race_en_name)
    candidates: List[str] = []

    stripped = normalize_spaces(re.sub(r"\s*\([^)]*\)\s*$", "", race_en_name))
    if stripped and stripped != race_en_name:
        candidates.append(stripped)

    words = [part for part in stripped.split() if part] if stripped else []
    if len(words) >= 2:
        candidates.append(words[-1])

    result: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = canonical_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def build_embedded_subrace_candidates(
    path: Path,
    stats: Dict[str, Any],
    race_en_name: str,
) -> List[str]:
    candidates: List[str] = []

    ru_name = normalize_lookup_text(stats.get("ru_name", {}).get("value", ""))
    if ru_name:
        candidates.append(ru_name)

    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name:
        candidates.append(current_name)

    parenthetical = re.search(r"\(([^()]+)\)\s*$", race_en_name)
    if parenthetical:
        candidates.append(normalize_lookup_text(parenthetical.group(1)))

    stem = path.stem
    if stem.endswith(".rpg"):
        stem = stem[:-4]
    if stem.startswith("race_"):
        stem = stem[5:]
    stem = re.sub(r"__[^_]+_?$", "", stem)
    stem_candidate = normalize_lookup_text(stem.replace("_", " "))
    if stem_candidate:
        candidates.append(stem_candidate)

    result: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = canonical_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result



def extract_slug_from_href(href: str) -> str:
    last = href.rstrip("/").split("/")[-1]
    m = SLUG_SPLIT_RE.match(last)
    return m.group(1) if m else last



def extract_spell_lookup_name(text: str) -> str:
    norm = normalize_lookup_text(text)
    m = EN_BRACKET_RE.search(norm)
    if m:
        inner = normalize_lookup_text(m.group(1))
        if looks_english(inner):
            return inner
    return norm



def detect_race_lookup_name(path: Path, stats: Dict[str, Any]) -> str:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    if current_name and looks_english(current_name):
        return current_name

    stem = path.name
    if stem.endswith('.rpg.json'):
        stem = stem[:-9]
    if stem.startswith('race_'):
        stem = stem[5:]

    stem = re.sub(r"__[^_]+_?$", "", stem)
    parts = [p for p in stem.split('_') if p]

    candidates: List[str] = [stem]
    if parts:
        candidates.extend([
            parts[-1],
            parts[0],
            ' '.join(parts),
        ])
        half = len(parts) // 2
        if len(parts) >= 2 and len(parts) % 2 == 0 and parts[:half] == parts[half:]:
            candidates.append(' '.join(parts[:half]))

    seen: set[str] = set()
    for candidate in candidates:
        candidate = normalize_lookup_text(candidate.replace('_', ' '))
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if looks_english(candidate):
            return candidate

    raise RuntimeError(
        f"Не удалось восстановить английское имя расы для dnd.su из файла {path.name!r}"
    )



def looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_lookup_text(text or "")))



def choose_translation_placeholder(text: str) -> Optional[str]:
    m = re.fullmatch(r"Choose\s+(\d+)", normalize_lookup_text(text).strip(), flags=re.I)
    if m:
        return f"Выберите {m.group(1)}"
    return None



def translate_static_text(text: str) -> Optional[str]:
    norm = normalize_lookup_text(text)
    if norm in STATIC_TEXT_MAP:
        return STATIC_TEXT_MAP[norm]

    placeholder = choose_translation_placeholder(norm)
    if placeholder:
        return placeholder

    if norm in EN_NAME_OVERRIDES:
        return EN_NAME_OVERRIDES[norm]

    return None


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

        for idx in range(0, len(pending), self.batch_size):
            batch = pending[idx : idx + self.batch_size]
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
                resp = self.session.post(self.url, json=payload, timeout=(10, self.timeout))
                resp.raise_for_status()
                data = resp.json()
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

        raise RuntimeError(f"Неожиданный формат батч-ответа переводчика: {data!r}")


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
        self._race_index: Optional[List[Dict[str, Any]]] = None
        self._spell_index: Optional[List[Dict[str, Any]]] = None

    def _cache_path(self, url: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", url)
        return self.cache_dir / f"{safe}.html"

    def get_html(self, url: str) -> str:
        cache_path = self._cache_path(url)
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8")
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        html_text = resp.text
        cache_path.write_text(html_text, encoding="utf-8")
        time.sleep(REQUEST_PAUSE_SEC)
        return html_text

    def get_soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.get_html(url), "html.parser")

    def build_race_index(self) -> List[Dict[str, Any]]:
        if self._race_index is not None:
            return self._race_index

        soup = self.get_soup(urljoin(BASE_URL, "/race/"))
        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, a["href"])
            if "/race/" not in href:
                continue
            if "/homebrew/" in href:
                continue
            if not href.startswith(BASE_URL):
                continue
            text = normalize_spaces(a.get_text(" ", strip=True))
            if not text:
                continue
            if href in seen:
                continue
            seen.add(href)
            entries.append(
                {
                    "href": href,
                    "text": text,
                    "slug": extract_slug_from_href(href),
                    "is_multiverse": "/multiverse/" in href,
                }
            )
        self._race_index = entries
        return entries

    def _filter_race_candidates(
        self,
        candidates: List[Dict[str, Any]],
        aliases: Sequence[str],
        wants_multiverse: bool,
    ) -> List[Dict[str, Any]]:
        preferred = [e for e in candidates if e["is_multiverse"] == wants_multiverse]
        if preferred:
            candidates = preferred

        by_alias = [
            e
            for e in candidates
            if any(alias.upper() in e["text"].upper() for alias in aliases)
        ]
        if by_alias:
            candidates = by_alias
        return candidates

    def _page_contains_subrace(
        self,
        entry: Dict[str, Any],
        expected_en_name: str,
        subrace_candidates: Sequence[str],
    ) -> bool:
        if not subrace_candidates:
            return False
        page = self.parse_race_page(entry["href"], expected_en_name=expected_en_name)
        for key in page.subraces:
            if any(titles_match(key, candidate) for candidate in subrace_candidates):
                return True
        return False

    def resolve_race_page(
        self,
        race_en_name: str,
        source_code: str,
        parent_race_candidates: Optional[Sequence[str]] = None,
        subrace_candidates: Optional[Sequence[str]] = None,
    ) -> ResolvedRacePage:
        expected_slug = slugify_name(race_en_name)
        aliases = SOURCE_ALIASES.get(source_code, [source_code.upper()])
        wants_multiverse = source_code in MULTIVERSE_SOURCES

        entries = self.build_race_index()
        exact_candidates = [e for e in entries if e["slug"] == expected_slug]
        exact_candidates = self._filter_race_candidates(exact_candidates, aliases, wants_multiverse)
        if exact_candidates:
            return ResolvedRacePage(
                url=exact_candidates[0]["href"],
                expected_en_name=race_en_name,
            )

        for parent_name in parent_race_candidates or []:
            parent_slug = slugify_name(parent_name)
            parent_candidates = [e for e in entries if e["slug"] == parent_slug]
            parent_candidates = self._filter_race_candidates(parent_candidates, aliases, wants_multiverse)
            for entry in parent_candidates:
                if self._page_contains_subrace(entry, parent_name, subrace_candidates or []):
                    return ResolvedRacePage(
                        url=entry["href"],
                        expected_en_name=parent_name,
                    )

        raise RuntimeError(f"Не найден URL dnd.su для расы {race_en_name!r}")

    def resolve_race_url(self, race_en_name: str, source_code: str) -> str:
        return self.resolve_race_page(race_en_name, source_code).url

    def build_spell_index(self) -> List[Dict[str, Any]]:
        if self._spell_index is not None:
            return self._spell_index

        soup = self.get_soup(urljoin(BASE_URL, "/spells/"))
        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, a["href"])
            if "/spells/" not in href:
                continue
            if "/homebrew/" in href:
                continue
            if not href.startswith(BASE_URL):
                continue
            text = normalize_spaces(a.get_text(" ", strip=True))
            if not text or href in seen:
                continue
            seen.add(href)
            en_name = ""
            m = EN_BRACKET_RE.search(text)
            if m:
                en_name = normalize_lookup_text(m.group(1))
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

    def parse_race_page(self, url: str, expected_en_name: str) -> PageFeatureSet:
        soup = self.get_soup(url)
        result = PageFeatureSet()
        result.spell_links = self._extract_spell_links(soup)

        elements: List[Tuple[str, str]] = []
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            text = normalize_spaces(tag.get_text(" ", strip=True))
            if text:
                elements.append((tag.name.lower(), text))

        started = False
        current_subrace: Optional[str] = None
        in_features = False
        current_feature_name: Optional[str] = None
        current_feature_lines: List[str] = []

        def current_map() -> Dict[str, str]:
            if current_subrace is None:
                return result.features
            return result.subraces.setdefault(current_subrace, {})

        def flush_feature() -> None:
            nonlocal current_feature_name, current_feature_lines
            if current_feature_name:
                desc = normalize_spaces("\n\n".join(line for line in current_feature_lines if line))
                current_map()[current_feature_name] = desc
            current_feature_name = None
            current_feature_lines = []

        for tag_name, text in elements:
            lower = text.lower()

            if not started:
                if tag_name == "h2" and f"[{expected_en_name.lower()}]" in lower:
                    result.title_ru = strip_brackets_title(text)
                    started = True
                continue

            if tag_name in {"h2", "h3"} and ("комментар" in lower or "галере" in lower):
                flush_feature()
                break

            if tag_name == "h2" and "[" not in text and result.title_ru and text != result.title_ru:
                flush_feature()
                current_subrace = strip_brackets_title(text)
                in_features = False
                continue

            if tag_name in {"h2", "h3", "h4"} and "особенности" in lower:
                flush_feature()
                in_features = True
                continue

            if tag_name in {"h2", "h3", "h4"} and in_features and "особенности" not in lower:
                flush_feature()
                in_features = False
                continue

            if (
                current_subrace is not None
                and not in_features
                and tag_name in {"p", "li"}
                and FEATURE_START_RE.match(text)
            ):
                in_features = True

            if not in_features or tag_name not in {"p", "li"}:
                continue

            if text in {
                "Вы обладаете следующими особенностями.",
                "Ваш персонаж обладает следующими расовыми особенностями.",
            }:
                continue
            if text.startswith("Ваш персонаж "):
                continue

            m = FEATURE_START_RE.match(text)
            if m:
                flush_feature()
                current_feature_name = normalize_spaces(m.group(1))
                first_desc = normalize_spaces(m.group(2))
                current_feature_lines = [first_desc] if first_desc else []
            elif current_feature_name:
                current_feature_lines.append(text)

        flush_feature()
        return result

    def _extract_spell_links(self, soup: BeautifulSoup) -> Dict[str, str]:
        links: Dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/spells/" not in href:
                continue
            text = normalize_spaces(a.get_text(" ", strip=True))
            m = EN_BRACKET_RE.search(text)
            if not m:
                continue
            en_name = normalize_lookup_text(m.group(1)).lower()
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
        m = re.search(r"\((.+)\)", components_line)
        if m:
            return normalize_spaces(m.group(1))
        return None



def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)



def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")



def get_top_stats(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc.get("stats", {})



def normalize_feature_key(name: str) -> str:
    return canonical_key(name)



def lookup_page_feature(
    page_features: Dict[str, str],
    target_name: str,
    feat_id: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    direct = page_features.get(target_name)
    if direct is not None:
        return target_name, direct

    normalized_index = {normalize_feature_key(k): (k, v) for k, v in page_features.items()}
    match = normalized_index.get(normalize_feature_key(target_name))
    if match:
        return match

    if feat_id in {"darkvision__60_ft._", "darkvision"}:
        match = normalized_index.get(normalize_feature_key("Тёмное зрение"))
        if match:
            return match
    return None



def get_primary_description_text(stats: Dict[str, Any]) -> str:
    lines: List[str] = []
    for desc in stats.get("descriptions", {}).get("value", []):
        desc_stats = desc.get("stats", {})
        text = normalize_lookup_text(desc_stats.get("description", {}).get("value", ""))
        if text:
            lines.append(text)
    return "\n".join(lines)



def resolve_target_feature_name(stats: Dict[str, Any]) -> Optional[str]:
    names = resolve_target_feature_names(stats)
    if names:
        return names[0]
    return None



def resolve_target_feature_names(stats: Dict[str, Any]) -> List[str]:
    current_name = normalize_lookup_text(stats.get("name", {}).get("value", ""))
    feat_id = stats.get("id")
    description_text = get_primary_description_text(stats).lower()

    names: List[str] = []

    def add(name: Optional[str]) -> None:
        if not name or name in names:
            return
        names.append(name)

    if feat_id == "metallic_breath_weapon":
        if "second breath weapon" in description_text or "magical gas" in description_text:
            add("Металлическое оружие дыхания")
            add("Оружие дыхания")
        else:
            add("Оружие дыхания")
            add("Металлическое оружие дыхания")
    elif feat_id == "breath_weapon":
        add("Оружие дыхания")

    if feat_id in ID_NAME_OVERRIDES:
        add(ID_NAME_OVERRIDES[feat_id])
    if current_name in EN_NAME_OVERRIDES:
        add(EN_NAME_OVERRIDES[current_name])
    if current_name:
        add(current_name)
    return names



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
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"name", "description", "options", "components", "casting_time", "range", "duration", "ru_name"} and isinstance(value, dict) and isinstance(value.get("value"), str):
                translated = translate_static_text(value["value"])
                if translated:
                    value["value"] = translated
            else:
                patch_known_leaf_translations(value)
    elif isinstance(obj, list):
        for item in obj:
            patch_known_leaf_translations(item)



def patch_unarmed_strike(stats: Dict[str, Any]) -> None:
    for item in stats.get("unarmed_strike", {}).get("value", []):
        try:
            weapon_stats = item["stats"]["weapon"]["value"]["stats"]
        except Exception:
            continue
        name_value = normalize_lookup_text(weapon_stats.get("name", {}).get("value", ""))
        translated = translate_static_text(name_value)
        if translated:
            weapon_stats["name"]["value"] = translated



def patch_top_level_ru_name(stats: Dict[str, Any], title_ru: str) -> None:
    ru_name = stats.get("ru_name")
    if isinstance(ru_name, dict) and "value" in ru_name and title_ru:
        ru_name["value"] = title_ru



def restore_top_level_name(original_stats: Dict[str, Any], stats: Dict[str, Any]) -> None:
    original_name = original_stats.get("name")
    current_name = stats.get("name")
    if (
        isinstance(original_name, dict)
        and isinstance(current_name, dict)
        and "value" in original_name
        and "value" in current_name
    ):
        current_name["value"] = original_name["value"]



def patch_subrace_names(stats: Dict[str, Any]) -> None:
    for subrace in stats.get("subraces", {}).get("value", []):
        sub_stats = subrace.get("stats", {})
        sub_id = sub_stats.get("id")
        cur_name = normalize_lookup_text(sub_stats.get("name", {}).get("value", ""))
        target = ID_NAME_OVERRIDES.get(sub_id) or EN_NAME_OVERRIDES.get(cur_name)
        if target and "name" in sub_stats and "value" in sub_stats["name"]:
            sub_stats["name"]["value"] = target



def sync_traits_from_page(
    traits: List[Dict[str, Any]],
    page_features: Dict[str, str],
    missing: List[str],
) -> None:
    for trait in traits:
        stats = trait.get("stats", {})
        if not stats:
            continue

        target_names = resolve_target_feature_names(stats)
        if not target_names:
            continue

        target_name = target_names[0]
        found_feature: Optional[Tuple[str, str]] = None
        for candidate in target_names:
            found_feature = lookup_page_feature(page_features, candidate, stats.get("id"))
            if found_feature:
                break

        if found_feature:
            matched_name, description = found_feature
            if "name" in stats and isinstance(stats["name"], dict) and "value" in stats["name"]:
                stats["name"]["value"] = matched_name
            desc_list = stats.get("descriptions", {}).get("value", [])
            if desc_list:
                first = desc_list[0].get("stats", {})
                if "description" in first and isinstance(first["description"], dict):
                    first["description"]["value"] = description
        else:
            if "name" in stats and isinstance(stats["name"], dict) and "value" in stats["name"] and not looks_english(target_name):
                stats["name"]["value"] = target_name
            current_name = normalize_lookup_text(stats.get("name", {}).get("value", "<без имени>"))
            if looks_english(current_name):
                missing.append(f"feature:{stats.get('id')}:{current_name}")

        patch_effect_names(stats)



def collect_spell_stat_blocks(obj: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        if set(obj.keys()) >= {"name", "casting_time", "range", "duration", "description"} and all(
            isinstance(obj.get(k), dict) for k in ("name", "casting_time", "range", "duration", "description")
        ):
            found.append(obj)
        for value in obj.values():
            found.extend(collect_spell_stat_blocks(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(collect_spell_stat_blocks(item))
    return found



def patch_spells_from_race_page(
    stats: Dict[str, Any],
    page: PageFeatureSet,
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



def find_subrace_page_entry(
    sub_stats: Dict[str, Any],
    page: PageFeatureSet,
    used_keys: Optional[set[str]] = None,
    allow_fallback: bool = True,
) -> Tuple[Optional[str], Dict[str, str]]:
    sub_name = normalize_lookup_text(sub_stats.get("name", {}).get("value", ""))
    sub_ru_name = normalize_lookup_text(sub_stats.get("ru_name", {}).get("value", ""))
    sub_id = sub_stats.get("id")

    candidates = [
        ID_NAME_OVERRIDES.get(sub_id),
        EN_NAME_OVERRIDES.get(sub_name),
        sub_ru_name,
        sub_name,
    ]

    for candidate in filter(None, candidates):
        for key, features in page.subraces.items():
            if used_keys is not None and key in used_keys:
                continue
            if titles_match(key, candidate):
                if used_keys is not None:
                    used_keys.add(key)
                return key, features

    if allow_fallback:
        for key, features in page.subraces.items():
            if used_keys is not None and key in used_keys:
                continue
            if used_keys is not None:
                used_keys.add(key)
            return key, features

    return None, {}


def get_subrace_page_features(sub_stats: Dict[str, Any], page: PageFeatureSet, used_keys: set[str]) -> Dict[str, str]:
    _, features = find_subrace_page_entry(sub_stats, page, used_keys)
    return features



def is_translatable_string_path(path: str) -> bool:
    if path in {"stats.name.value"}:
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
        for idx, value in enumerate(obj):
            child_path = f"{path}[{idx}]"
            if isinstance(value, str):
                if is_translatable_string_path(child_path) and looks_english(value):
                    refs.append(StringRef(obj, idx, child_path, value))
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
        for idx, value in enumerate(obj):
            child_path = f"{path}[{idx}]"
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

    for src_text, ref_list in grouped.items():
        translated = translated_map.get(src_text, src_text)
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
    original_stats = get_top_stats(original)
    doc = copy.deepcopy(original)
    stats = get_top_stats(doc)

    source_code = normalize_lookup_text(stats.get("source", {}).get("value", "")).lower()
    if not source_code:
        raise RuntimeError(f"В {path.name} не удалось определить source")

    race_en_name = detect_race_lookup_name(path, stats)
    parent_race_candidates = build_parent_race_candidates(race_en_name)
    embedded_subrace_candidates = build_embedded_subrace_candidates(path, stats, race_en_name)

    missing: List[str] = []

    resolved_page = client.resolve_race_page(
        race_en_name,
        source_code,
        parent_race_candidates=parent_race_candidates,
        subrace_candidates=embedded_subrace_candidates,
    )
    page = client.parse_race_page(
        resolved_page.url,
        expected_en_name=resolved_page.expected_en_name,
    )

    top_title_ru = page.title_ru
    top_page_features = page.features
    if page.subraces:
        matched_subrace_title, matched_subrace_features = find_subrace_page_entry(
            stats,
            page,
            allow_fallback=False,
        )
        if matched_subrace_title and matched_subrace_features:
            top_title_ru = matched_subrace_title
            top_page_features = matched_subrace_features

    patch_top_level_ru_name(stats, top_title_ru)
    patch_choice_texts(stats)
    patch_unarmed_strike(stats)
    patch_subrace_names(stats)
    patch_known_leaf_translations(stats)

    top_traits = stats.get("traits", {}).get("value", [])
    sync_traits_from_page(top_traits, top_page_features, missing)

    used_subrace_keys: set[str] = set()
    for subrace in stats.get("subraces", {}).get("value", []):
        sub_stats = subrace.get("stats", {})
        sub_features = get_subrace_page_features(sub_stats, page, used_subrace_keys)
        merged_sub_features = dict(page.features)
        merged_sub_features.update(top_page_features)
        merged_sub_features.update(sub_features)
        sync_traits_from_page(sub_stats.get("traits", {}).get("value", []), merged_sub_features, missing)
        patch_effect_names(sub_stats)

    patch_spells_from_race_page(stats, page, client, missing)
    patch_known_leaf_translations(stats)
    translate_remaining_strings(doc, translator, missing)
    restore_top_level_name(original_stats, stats)
    remaining = collect_remaining_english_strings(doc)
    save_json(out_path, doc)
    return missing, remaining



def expand_input_patterns(patterns: Iterable[str]) -> List[Path]:
    paths: List[Path] = []
    for pattern in patterns:
        matches = [Path(p) for p in glob.glob(pattern)]
        if matches:
            paths.extend(matches)
        else:
            p = Path(pattern)
            if p.exists():
                paths.append(p)
    unique: List[Path] = []
    seen: set[Path] = set()
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Переносит переводы рас из dnd.su в race_*.json")
    parser.add_argument("inputs", nargs="+", help="Файлы или glob-паттерны, например ./race_*.json")
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
