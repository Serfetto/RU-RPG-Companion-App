#!/usr/bin/env python3
"""
Translate character sheet section .rpgs resources using a static D&D-oriented
mapping first and a fallback translator for anything that remains in English.

The script intentionally skips technical identifiers such as id/stat/resource_id
and only translates user-facing string literals for keys like text, label,
statName, pop_up_title, sheet_title, and similar UI captions.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

DEFAULT_TRANSLATOR_TIMEOUT = 240
REQUEST_PAUSE_SEC = 0.2
TRANSLATOR_RETRY_ATTEMPTS = 5
TRANSLATOR_RETRY_DELAY_SEC = 2.0
DEFAULT_TRANSLATOR_URL = "http://localhost:8000/chat"

TRANSLATABLE_KEYS = {
    "text",
    "label",
    "statName",
    "pop_up_title",
    "sheet_title",
    "edit_bottom_sheet_title",
    "bottom_left_button_title",
}

ABILITY_NAMES: Dict[str, str] = {
    "Strength": "Сила",
    "Dexterity": "Ловкость",
    "Constitution": "Телосложение",
    "Intelligence": "Интеллект",
    "Wisdom": "Мудрость",
    "Charisma": "Харизма",
}
ABILITY_GENITIVE: Dict[str, str] = {
    "Strength": "Силы",
    "Dexterity": "Ловкости",
    "Constitution": "Телосложения",
    "Intelligence": "Интеллекта",
    "Wisdom": "Мудрости",
    "Charisma": "Харизмы",
}
SKILLS: Dict[str, str] = {
    "Acrobatics": "Акробатика",
    "Animal Handling": "Уход за животными",
    "Arcana": "Магия",
    "Athletics": "Атлетика",
    "Deception": "Обман",
    "History": "История",
    "Insight": "Проницательность",
    "Intimidation": "Запугивание",
    "Investigation": "Анализ",
    "Medicine": "Медицина",
    "Nature": "Природа",
    "Perception": "Восприятие",
    "Performance": "Выступление",
    "Persuasion": "Убеждение",
    "Religion": "Религия",
    "Sleight of Hand": "Ловкость рук",
    "Stealth": "Скрытность",
    "Survival": "Выживание",
}
CONDITIONS: Dict[str, str] = {
    "Blinded": "Ослеплённый",
    "Charmed": "Очарованный",
    "Deafened": "Оглохший",
    "Frightened": "Испуганный",
    "Grappled": "Схваченный",
    "Incapacitated": "Недееспособный",
    "Invisible": "Невидимый",
    "Paralyzed": "Парализованный",
    "Petrified": "Окаменевший",
    "Poisoned": "Отравленный",
    "Prone": "Сбитый с ног",
    "Restrained": "Опутанный",
    "Stunned": "Ошеломлённый",
    "Unconscious": "Бессознательный",
    "Exhausted": "Истощённый",
}
STATIC_TEXT_MAP: Dict[str, str] = {
    "INFORMATION": "ИНФОРМАЦИЯ",
    "notes": "Заметки",
    "Conditions": "Состояния",
    "Manage conditions": "Управление состояниями",
    "Death Saves": "Спасброски от смерти",
    "Successes": "Успехи",
    "Failures": "Провалы",
    "Hit points": "Хиты",
    "Max hit points": "Макс. хиты",
    "HP": " ОЗ",
    "Base HP +": "Базовые хиты + ",
    "mods": " мод.",
    "Initiative": "Инициатива",
    "Manage speeds": "Управление скоростями",
    "Manage hit dice": "Управление костями хитов",
    "Ability Modifiers": "Модификаторы характеристик",
    "Saving Throws": "Спасброски",
    "Skills": "Навыки",
    "Traits & Features": "Черты и особенности",
    "Racial traits": "Расовые черты",
    "Background": "Предыстория",
    "Armor": "Доспехи",
    "Weapons": "Оружие",
    "Attacks per action: ": "Атак за действие: ",
    "Attacks per action:": "Атак за действие:",
    "Equipment": "Снаряжение",
    "Copper": "Медь",
    "Silver": "Серебро",
    "Electrum": "Электрум",
    "Gold": "Золото",
    "Platinum": "Платина",
    "Attuned items": "Настроенные предметы",
    "Spell Slots": "Ячейки заклинаний",
    "Spells": "Заклинания",
    "Cantrips": "Заговоры",
}
STATIC_TEXT_MAP.update(ABILITY_NAMES)
STATIC_TEXT_MAP.update(SKILLS)
STATIC_TEXT_MAP.update(CONDITIONS)

ASSIGNMENT_RE = re.compile(
    r'(?P<prefix>\b(?P<key>' + "|".join(sorted(map(re.escape, TRANSLATABLE_KEYS), key=len, reverse=True)) + r')\s*=\s*)"(?P<value>(?:\\.|[^"\\])*)"',
)
CONCAT_ASSIGNMENT_RE = re.compile(
    r'(?P<prefix>\b(?P<key>' + "|".join(sorted(map(re.escape, TRANSLATABLE_KEYS), key=len, reverse=True)) + r')\s*=\s*concat\(\[)(?P<body>.*?)(?P<suffix>\]\))',
    flags=re.DOTALL,
)
STRING_LITERAL_RE = re.compile(r'"(?P<value>(?:\\.|[^"\\])*)"')
LEVEL_RE = re.compile(r"^Level\s+(\d+)$", flags=re.I)
SAVING_THROW_RE = re.compile(
    r"^(Strength|Dexterity|Constitution|Intelligence|Wisdom|Charisma) saving throw$",
    flags=re.I,
)


@dataclass
class StringRef:
    value: str
    count: int = 0


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


def looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_lookup_text(text or "")))


def choose_level_translation(text: str) -> Optional[str]:
    match = LEVEL_RE.fullmatch(normalize_lookup_text(text))
    if match:
        return f"{match.group(1)}-й уровень"
    return None


def choose_saving_throw_translation(text: str) -> Optional[str]:
    match = SAVING_THROW_RE.fullmatch(normalize_lookup_text(text))
    if not match:
        return None
    ability = match.group(1).title()
    genitive = ABILITY_GENITIVE.get(ability)
    if not genitive:
        return None
    return f"Спасбросок {genitive}"


def translate_static_text(text: str) -> Optional[str]:
    normalized = normalize_lookup_text(text)
    if normalized in STATIC_TEXT_MAP:
        return STATIC_TEXT_MAP[normalized]

    saving_throw = choose_saving_throw_translation(normalized)
    if saving_throw:
        return saving_throw

    level = choose_level_translation(normalized)
    if level:
        return level

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
    def _extract_translation(data: object) -> str:
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
    def _extract_translations(data: object, expected_count: int) -> List[str]:
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


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def collect_translatable_strings(text: str) -> List[str]:
    values: List[str] = []
    for match in ASSIGNMENT_RE.finditer(text):
        value = match.group("value")
        if value and looks_english(value):
            values.append(value)
    for match in CONCAT_ASSIGNMENT_RE.finditer(text):
        for literal in STRING_LITERAL_RE.finditer(match.group("body")):
            value = literal.group("value")
            if value and looks_english(value):
                values.append(value)
    return values


def collect_remaining_english_strings(text: str) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    for match in ASSIGNMENT_RE.finditer(text):
        key = match.group("key")
        value = match.group("value")
        if value and looks_english(value):
            found.append((key, value))
    for match in CONCAT_ASSIGNMENT_RE.finditer(text):
        key = match.group("key")
        for literal in STRING_LITERAL_RE.finditer(match.group("body")):
            value = literal.group("value")
            if value and looks_english(value):
                found.append((key, value))
    return found


def translate_text_content(
    text: str,
    translator: Optional[TranslatorClient],
    missing: List[str],
) -> str:
    grouped: Dict[str, StringRef] = {}
    for value in collect_translatable_strings(text):
        if translate_static_text(value):
            continue
        normalized = normalize_lookup_text(value)
        item = grouped.get(normalized)
        if item is None:
            grouped[normalized] = StringRef(value=value, count=1)
        else:
            item.count += 1

    translated_map: Dict[str, str] = {}
    if grouped:
        if not translator or not translator.enabled:
            missing.append(f"translator:disabled:{len(grouped)}")
        else:
            translated_map = translator.translate_many([item.value for item in grouped.values()])

    def translate_literal(original: str) -> str:
        translated = translate_static_text(original)
        if not translated:
            translated = translated_map.get(original, original)
        if not translated:
            translated = original
        return translated.replace("\\", r"\\").replace('"', r'\"')

    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        original = match.group("value")
        return f'{prefix}"{translate_literal(original)}"'

    def replace_concat(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        body = match.group("body")
        suffix = match.group("suffix")
        body = STRING_LITERAL_RE.sub(lambda item: f'"{translate_literal(item.group("value"))}"', body)
        return f"{prefix}{body}{suffix}"

    text = CONCAT_ASSIGNMENT_RE.sub(replace_concat, text)
    return ASSIGNMENT_RE.sub(replace, text)


def translate_file(
    path: Path,
    out_path: Path,
    translator: Optional[TranslatorClient],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    original = load_text(path)
    missing: List[str] = []
    translated = translate_text_content(original, translator, missing)
    remaining = collect_remaining_english_strings(translated)
    save_text(out_path, translated)
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
    parser = argparse.ArgumentParser(
        description="Переводит пользовательские UI-строки в character_sheet_sections/*.rpgs"
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help=r"Файлы или glob-паттерны, например ./systems-ru/5e/system/character_sheet_sections/*.rpgs",
    )
    parser.add_argument("--out-dir", default="translated", help="Куда сохранить результат")
    parser.add_argument("--in-place", action="store_true", help="Перезаписать исходные файлы")
    parser.add_argument(
        "--translator-url",
        default=DEFAULT_TRANSLATOR_URL,
        help="URL fallback-переводчика",
    )
    parser.add_argument(
        "--translator-timeout",
        type=int,
        default=DEFAULT_TRANSLATOR_TIMEOUT,
        help="Таймаут fallback-переводчика",
    )
    parser.add_argument(
        "--translator-batch-size",
        type=int,
        default=5,
        help="Размер батча для fallback-переводчика",
    )
    parser.add_argument(
        "--no-translator",
        action="store_true",
        help="Отключить fallback-переводчик",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_files = expand_input_patterns(args.inputs)
    if not input_files:
        log("Не найдено ни одного входного файла.")
        return 2

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
            missing, remaining = translate_file(path, out_path, translator)
            log(f"[OK] {path.name} -> {out_path}")
            if missing:
                log(f"  Fallback не использован для части строк ({len(missing)}):")
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
            for key, value in items[:30]:
                log(f"    - {key} = {value}")
            if len(items) > 30:
                log(f"    ... и ещё {len(items) - 30}")

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
