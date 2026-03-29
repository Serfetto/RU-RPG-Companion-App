#!/usr/bin/env python3
"""
Translate system/character_stats.rpgs labels to Russian.

The script is intentionally deterministic:
- it edits only stat metadata in the RPGS DSL (`name = "..."`, `abbreviation = "..."`)
- it does not touch stat ids or formula identifiers
- it also patches a few user-visible inline string literals used by this file
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_INPUT = Path("rpg-companion-app/systems-ru/5e/system/character_stats.rpgs")

ABILITY_NAMES = {
    "strength": "Сила",
    "dexterity": "Ловкость",
    "constitution": "Телосложение",
    "intelligence": "Интеллект",
    "wisdom": "Мудрость",
    "charisma": "Харизма",
}
ABILITY_ABBRS = {
    "strength": "Сил",
    "dexterity": "Лов",
    "constitution": "Тел",
    "intelligence": "Инт",
    "wisdom": "Мдр",
    "charisma": "Хар",
}
ABILITY_GENITIVE = {
    "strength": "Силы",
    "dexterity": "Ловкости",
    "constitution": "Телосложения",
    "intelligence": "Интеллекта",
    "wisdom": "Мудрости",
    "charisma": "Харизмы",
}
SHORT_ABILITY_TO_LONG = {
    "str": "strength",
    "dex": "dexterity",
    "con": "constitution",
    "int": "intelligence",
    "wis": "wisdom",
    "cha": "charisma",
}

SKILLS = {
    "acrobatics": "Акробатика",
    "animal_handling": "Уход за животными",
    "arcana": "Магия",
    "athletics": "Атлетика",
    "deception": "Обман",
    "history": "История",
    "insight": "Проницательность",
    "intimidation": "Запугивание",
    "investigation": "Анализ",
    "medicine": "Медицина",
    "nature": "Природа",
    "perception": "Восприятие",
    "performance": "Выступление",
    "persuasion": "Убеждение",
    "religion": "Религия",
    "sleight_of_hand": "Ловкость рук",
    "stealth": "Скрытность",
    "survival": "Выживание",
}

CONDITIONS = {
    "blinded": "Ослеплён",
    "charmed": "Очарован",
    "deafened": "Оглохший",
    "frightened": "Испуган",
    "grappled": "Схвачен",
    "incapacitated": "Недееспособен",
    "invisible": "Невидим",
    "paralyzed": "Парализован",
    "petrified": "Окаменел",
    "poisoned": "Отравлен",
    "prone": "Сбит с ног",
    "restrained": "Опутан",
    "stunned": "Ошеломлён",
    "unconscious": "Без сознания",
    "exhausted": "Истощение",
}

FIXED_NAMES = {
    "created_at_display": "Дата создания",
    "photo": "Фото",
    "name": "Имя",
    "alignment": "Мировоззрение",
    "personality_traits": "Черты характера",
    "ideals": "Идеалы",
    "bonds": "Привязанности",
    "flaws": "Недостатки",
    "about": "О персонаже",
    "notes": "Заметки",
    "base_hp": "Базовые хиты",
    "current_hp": "Хиты",
    "max_hp": "Макс. хиты",
    "hit_point_mode": "Режим хитов",
    "classes_hp_mods": "Модификаторы хитов от классов",
    "total_hit_dice": "Кости хитов (всего)",
    "description": "Описание",
    "classes": "Классы",
    "background": "Предыстория",
    "race": "Раса",
    "subrace_id": "ID подрасы",
    "racial_traits": "Расовые черты",
    "advantages": "Преимущества",
    "disadvantages": "Помехи",
    "is_not_wearing_inproficient_armor": "Персонаж не носит доспех, которым не владеет",
    "armor_disadvantages": "Помехи от доспеха",
    "equipped_armor_imposes_stealth_disadvantage": "Надетый доспех даёт помеху на Скрытность",
    "all_disadvantages": "Все помехи",
    "language_proficiencies": "Владение языками",
    "armor_proficiencies": "Владение доспехами",
    "weapon_proficiencies": "Владение оружием",
    "tool_proficiencies": "Владение инструментами",
    "sort_skills_by_name": "Сортировать навыки по имени",
    "equipped_armor_id": "ID надетого доспеха",
    "equipped_shield_id": "ID надетого щита",
    "speed": "Скорость",
    "burrow_speed": "Скорость копания",
    "climb_speed": "Скорость лазания",
    "fly_speed": "Скорость полёта",
    "swim_speed": "Скорость плавания",
    "worn_armor_speed_modifier": "Модификатор скорости от доспеха (штраф)",
    "level": "Уровень",
    "armor_class": "КД",
    "race_ac": "КД от расы",
    "classes_ac": "КД от классов",
    "equipped_armor_ac": "КД надетого доспеха",
    "equipped_armor": "Надетый доспех",
    "equipped_shield": "Надетый щит",
    "passive_perception": "Пассивное восприятие",
    "initiative": "Инициатива",
    "proficiency_bonus": "Бонус мастерства",
    "display_proficiency_bonus": "Отображаемый бонус мастерства",
    "attacks_per_action": "Атак за действие",
    "all_weapons": "Всё оружие (включая расовое и классовое)",
    "weapons": "Оружие",
    "armors": "Доспехи",
    "equipment": "Снаряжение",
    "sorted_spells": "Отсортированные заклинания",
    "cantrips": "Заговоры",
    "spells": "Заклинания",
    "remaining_point_buy_points": "Оставшиеся очки поинт-бая",
    "creator_selected_subrace": "Выбранная подраса при создании",
    "creator_ability_score_increases": "Выборы увеличения характеристик при создании",
    "old_creator_ability_score_increases": "Старые выборы увеличения характеристик при создании",
    "ability_score_mode": "Режим характеристик",
    "creator_language_proficiency_choices": "Выборы языков при создании",
    "creator_skill_proficiency_choices": "Выборы навыков при создании",
    "creator_armor_proficiency_choices": "Выборы владения доспехами при создании",
    "creator_weapon_proficiency_choices": "Выборы владения оружием при создании",
    "creator_tool_proficiency_choices": "Выборы владения инструментами при создании",
    "creator_starting_equipment_choices": "Выборы стартового снаряжения при создании",
    "creator_starting_armor_choices": "Выборы стартовых доспехов при создании",
    "creator_starting_weapon_choices": "Выборы стартового оружия при создании",
    "spell_attack_extra_bonus": "Доп. бонус к атаке заклинанием",
    "spell_dc_extra_bonus": "Доп. бонус к Сл спасброска заклинаний",
    "class_hit_dice_formatted": "Кости хитов класса (например, 1d10)",
    "selected_selectable_feature_ids": "ID выбранных выбираемых особенностей",
    "race_traits": "Черты расы",
    "background_features": "Особенности предыстории",
    "feats": "Черты",
    "is_single_half_level_caster": "Есть только один полу-заклинатель",
    "first_half_level_caster_class": "Первый класс полу-заклинателя",
    "is_single_third_level_caster": "Есть только один третичный заклинатель",
    "first_third_level_caster_class": "Первый класс третичного заклинателя",
    "spellcaster_level": "Уровень заклинателя",
    "custom_spell_slot_classes": "Классы с пользовательскими ячейками",
    "level_up_selected_class_details": "Выбранный существующий класс при повышении уровня",
    "level_up_available_archetypes": "Доступные архетипы при повышении уровня",
    "level_up_has_to_select_archetype": "Нужно выбрать архетип при повышении уровня для текущего класса",
    "temp_level_up_class": "Временный класс для выбора среди существующих",
    "temp_level_up_archetype": "Временный архетип для существующего класса",
    "temp_level_up_multiclass": "Временный выбор мультикласса",
    "temp_level_up_new_class": "Временный новый класс при повышении уровня",
    "level_up_language_proficiency_choices": "Выборы языков при повышении уровня",
    "level_up_skill_proficiency_choices": "Выборы навыков при повышении уровня",
    "level_up_armor_proficiency_choices": "Выборы владения доспехами при повышении уровня",
    "level_up_weapon_proficiency_choices": "Выборы владения оружием при повышении уровня",
    "level_up_tool_proficiency_choices": "Выборы владения инструментами при повышении уровня",
    "temp_last_level_up_class_id": "ID последнего класса при повышении уровня",
    "last_level_up_class_details": "Последний класс при повышении уровня",
    "should_show_asi": "Нужно показать УХ",
    "temp_asi_selection_1": "Временный выбор УХ 1",
    "temp_asi_selection_2": "Временный выбор УХ 2",
    "temp_asi_choose_feat": "Временный выбор черты вместо УХ",
    "temp_asi_feat_selection": "Временный выбор черты для УХ",
    "features_with_new_descriptions_in_new_level": "Особенности с новыми описаниями на новом уровне",
    "currently_levelling_up": "Сейчас повышает уровень",
    "selectable_features_with_new_amounts_in_new_level": "Выбираемые особенности с новым количеством на новом уровне",
    "hair_color": "Цвет волос",
    "eye_color": "Цвет глаз",
    "outfit": "Наряд",
    "appearance_weapon": "Внешний вид оружия",
    "sex": "Пол",
    "photo_prompt": "Промпт для фото",
    "attuned_armors": "Настроенные доспехи",
    "attuned_weapons": "Настроенное оружие",
    "attuned_items": "Настроенные предметы",
    "attuned_resources": "Настроенные ресурсы",
    "attuned_resources_count_message": "Сообщение о числе настроенных ресурсов",
    "selecting": "Игрок сейчас делает выборы для этого персонажа",
    "should_show_controls_for_choices": "Показывать элементы управления для выборов",
    "all_features": "Все особенности",
    "has_jack_of_all_trades": "Есть «На все руки мастер»",
    "is_new_and_should_long_rest": "Новый персонаж и должен пройти длительный отдых",
    "companions": "Спутники",
    "companion_section_title": "Заголовок секции спутников",
}

FIXED_ABBREVIATIONS = {
    "level": "Ур.",
    "armor_class": "КД",
    "passive_perception": "ПВ",
    "initiative": "Иниц.",
    "proficiency_bonus": "БМ",
}

INLINE_STRING_REPLACEMENTS = {
    '"Created "': '"Создан "',
    '"Attuned to "': '"Настроено: "',
    '" / 3 items"': '" / 3 предметов"',
    '"Wild shape"': '"Дикая форма"',
    '"Companions / familiars"': '"Спутники / фамильяры"',
}

HEADER_RE = re.compile(
    r"(^\s*(?:base|calc)\s+[^\n=]*?\s+(?P<stat_id>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<attrs>.*?)\)\s*=)",
    re.MULTILINE | re.DOTALL,
)


def base_label_from_id(stat_id: str) -> Optional[str]:
    for ability_key, translated in ABILITY_NAMES.items():
        if stat_id in {
            f"{ability_key}_score",
            f"{ability_key}_modifier",
            f"{ability_key}_saving_throw",
            f"{ability_key}_saving_throw_proficiency",
        }:
            return translated

    if stat_id in SKILLS:
        return SKILLS[stat_id]
    if stat_id in CONDITIONS:
        return CONDITIONS[stat_id]
    return FIXED_NAMES.get(stat_id)


def name_for_stat(stat_id: str) -> Optional[str]:
    if stat_id in FIXED_NAMES:
        return FIXED_NAMES[stat_id]

    if stat_id in SKILLS:
        return SKILLS[stat_id]

    if stat_id in CONDITIONS:
        return CONDITIONS[stat_id]

    match = re.fullmatch(r"current_(d\d+)_hit_dice", stat_id)
    if match:
        return f"Кости хитов ({match.group(1)})"

    match = re.fullmatch(r"max_(d\d+)_hit_dice", stat_id)
    if match:
        return f"Макс. кости хитов ({match.group(1)})"

    match = re.fullmatch(r"level_(\d+)_spells", stat_id)
    if match:
        return f"Заклинания {match.group(1)}-го уровня"

    match = re.fullmatch(r"max_spell_slots_(\d+)_bonus", stat_id)
    if match:
        return f"Бонус к макс. ячейкам {match.group(1)}-го уровня"

    match = re.fullmatch(r"spell_slots_(\d+)", stat_id)
    if match:
        return f"Ячейки заклинаний {match.group(1)}-го уровня"

    match = re.fullmatch(r"(str|dex|con|int|wis|cha)_point_buy_point_worth", stat_id)
    if match:
        ability_key = SHORT_ABILITY_TO_LONG[match.group(1)]
        return f"Стоимость {ABILITY_GENITIVE[ability_key]} в поинт-бае"

    match = re.fullmatch(
        r"(strength|dexterity|constitution|intelligence|wisdom|charisma)_(score|modifier|saving_throw|saving_throw_proficiency)",
        stat_id,
    )
    if match:
        return ABILITY_NAMES[match.group(1)]

    match = re.fullmatch(
        r"(acrobatics|animal_handling|arcana|athletics|deception|history|insight|intimidation|investigation|medicine|nature|perception|performance|persuasion|religion|sleight_of_hand|stealth|survival)_proficiency",
        stat_id,
    )
    if match:
        return f"Владение навыком «{SKILLS[match.group(1)]}»"

    match = re.fullmatch(r"(.+)_formatted", stat_id)
    if match:
        base_label = base_label_from_id(match.group(1))
        if base_label:
            return f"{base_label} (формат.)"

    match = re.fullmatch(r"(.+)_label_color", stat_id)
    if match:
        base_label = base_label_from_id(match.group(1))
        if base_label:
            return f"{base_label}: цвет подписи"

    match = re.fullmatch(r"(.+)_color", stat_id)
    if match:
        base_label = base_label_from_id(match.group(1))
        if base_label:
            return f"{base_label}: цвет числа"

    match = re.fullmatch(r"temp_short_rest_hit_dice_(d\d+)", stat_id)
    if match:
        return match.group(1)

    match = re.fullmatch(r"(copper|silver|electrum|gold|platinum)_pieces", stat_id)
    if match:
        return {
            "copper": "Медные монеты",
            "silver": "Серебряные монеты",
            "electrum": "Электрумовые монеты",
            "gold": "Золотые монеты",
            "platinum": "Платиновые монеты",
        }[match.group(1)]

    match = re.fullmatch(r"death_saving_throws_success_(\d+)", stat_id)
    if match:
        return f"Успешный спасбросок от смерти {match.group(1)}"

    match = re.fullmatch(r"death_saving_throws_failure_(\d+)", stat_id)
    if match:
        return f"Проваленный спасбросок от смерти {match.group(1)}"

    return None


def abbreviation_for_stat(stat_id: str) -> Optional[str]:
    if stat_id in FIXED_ABBREVIATIONS:
        return FIXED_ABBREVIATIONS[stat_id]

    for ability_key, translated in ABILITY_ABBRS.items():
        if stat_id in {
            f"{ability_key}_score",
            f"{ability_key}_modifier",
            f"{ability_key}_saving_throw",
            f"{ability_key}_saving_throw_proficiency",
        }:
            return translated

    return None


def replace_attr_value(attrs: str, attr_name: str, new_value: str) -> str:
    pattern = re.compile(rf'({re.escape(attr_name)}\s*=\s*")([^"]*)(")')
    if not pattern.search(attrs):
        return attrs
    return pattern.sub(rf'\g<1>{new_value}\g<3>', attrs, count=1)


def translate_text(text: str) -> tuple[str, List[str]]:
    unknown_ids: List[str] = []

    def patch_header(match: re.Match[str]) -> str:
        header = match.group(1)
        stat_id = match.group("stat_id")
        attrs = match.group("attrs")

        new_name = name_for_stat(stat_id)
        if new_name is None:
            unknown_ids.append(stat_id)
        else:
            attrs = replace_attr_value(attrs, "name", new_name)

        new_abbr = abbreviation_for_stat(stat_id)
        if new_abbr is not None:
            attrs = replace_attr_value(attrs, "abbreviation", new_abbr)

        return header.replace(match.group("attrs"), attrs, 1)

    translated = HEADER_RE.sub(patch_header, text)

    for old, new in INLINE_STRING_REPLACEMENTS.items():
        translated = translated.replace(old, new)

    return translated, sorted(set(unknown_ids))


def expand_inputs(patterns: Iterable[str]) -> List[Path]:
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
        description="Переводит labels в system/character_stats.rpgs, не трогая ids и формулы."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[str(DEFAULT_INPUT)],
        help="Файл или glob-паттерн. По умолчанию: systems-ru/5e/system/character_stats.rpgs",
    )
    parser.add_argument(
        "--out-dir",
        default="translated",
        help="Куда сохранять результат, если не указан --in-place.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Перезаписать исходный файл.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_paths = expand_inputs(args.inputs)
    if not input_paths:
        print("Не найдено ни одного входного файла.", file=sys.stderr)
        return 1

    for input_path in input_paths:
        source_text = input_path.read_text(encoding="utf-8")
        translated_text, unknown_ids = translate_text(source_text)

        if args.in_place:
            out_path = input_path
        else:
            out_path = Path(args.out_dir) / input_path.name
            out_path.parent.mkdir(parents=True, exist_ok=True)

        out_path.write_text(translated_text, encoding="utf-8")

        print(f"[ok] {input_path} -> {out_path}")
        if unknown_ids:
            print(
                f"[warn] Не удалось автоматически перевести stat ids: {', '.join(unknown_ids)}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
