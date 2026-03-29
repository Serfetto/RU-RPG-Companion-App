#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, List, Union

PathPart = Union[str, int]

PATH_TRANSLATIONS = {
    "progression_systems.experience_levelling_system.level_up_view_title.components.components[0].value": "Повышение до ",
    "progression_systems.experience_levelling_system.level_up_button_text": "Повысить уровень",
    "progression_systems.experience_levelling_system.level_up_view.subviews[0].subviews[0].text.value": "Повысить уровень как ",
    "progression_systems.experience_levelling_system.level_up_view.subviews[1].label.value": "Я хочу взять мультикласс вместо этого",
    "progression_systems.experience_levelling_system.level_up_view.subviews[2].validation_message.clauses[0].components.value": "Похоже, вы пытаетесь взять мультикласс, но не выбрали новый класс, в который хотите мультиклассироваться",
    "progression_systems.experience_levelling_system.level_up_view.subviews[2].validation_message.clauses[1].components.value": "Похоже, вы пытаетесь взять мультикласс, но выбрали класс, в котором у вас уже есть уровни. Возможно, стоит снять флажок «мультикласс» или выбрать другой класс",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.pop_up_view.subviews[0].left_view.text.value": "Владение языками:",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.pop_up_view.subviews[1].left_view.text.value": "Владение навыками:",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.pop_up_view.subviews[2].left_view.text.value": "Владение доспехами:",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.pop_up_view.subviews[3].left_view.text.value": "Владение оружием:",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.pop_up_view.subviews[4].left_view.text.value": "Владение инструментами:",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.pop_up_title.value": "Выберите дополнительные владения",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[7].clauses[0].effect.extra_buttons[0].text.value": "СОХРАНИТЬ",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[8].clauses[0].effect.pop_up_view.subviews[0].text.value": "Выберите две характеристики, чтобы увеличить их значение на +1 каждую (можно выбрать одну и ту же дважды)",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[8].clauses[0].effect.pop_up_view.subviews[1].subviews[1].text.value": " и ",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[8].clauses[0].effect.pop_up_view.subviews[2].label.value": "Я хочу выбрать черту вместо этого",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[8].clauses[0].effect.pop_up_view.subviews[3].subviews[0].text.value": "Черта: ",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[8].clauses[0].effect.pop_up_title.value": "Увеличение характеристик",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[8].clauses[0].effect.extra_buttons[0].text.value": "СОХРАНИТЬ",
    "progression_systems.experience_levelling_system.on_level_up_confirm.effects[9].apply.cancel_text.value": "ОК",
    "progression_systems.experience_levelling_system.experience_name": "опыт",
    "progression_systems.experience_levelling_system.experience_abbreviation": "опт",
    "progression_systems.experience_levelling_system.level_stat_name": "уровень",
    "progression_systems.experience_levelling_system.milestones_system_name": "Вехи",
    "progression_systems.experience_levelling_system.tables[0].name": "По умолчанию",
    "currencies[0].name": "Платиновая монета",
    "currencies[0].plural": "Платиновые монеты",
    "currencies[0].abbreviation": "пм",
    "currencies[1].name": "Золотая монета",
    "currencies[1].plural": "Золотые монеты",
    "currencies[1].abbreviation": "зм",
    "currencies[2].name": "Электровая монета",
    "currencies[2].plural": "Электровые монеты",
    "currencies[2].abbreviation": "эм",
    "currencies[3].name": "Серебряная монета",
    "currencies[3].plural": "Серебряные монеты",
    "currencies[3].abbreviation": "см",
    "currencies[4].name": "Медная монета",
    "currencies[4].plural": "Медные монеты",
    "currencies[4].abbreviation": "мм",
}


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as handle:
        return json.load(handle)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\\n')


def parse_path(path: str) -> List[PathPart]:
    parts: List[PathPart] = []
    token = ''
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == '.':
            if token:
                parts.append(token)
                token = ''
            i += 1
            continue
        if ch == '[':
            if token:
                parts.append(token)
                token = ''
            j = path.index(']', i)
            parts.append(int(path[i + 1:j]))
            i = j + 1
            continue
        token += ch
        i += 1
    if token:
        parts.append(token)
    return parts


def set_by_path(data: Any, path: str, value: Any) -> None:
    parts = parse_path(path)
    cursor = data
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value


def apply_translations(doc: Any) -> Any:
    result = copy.deepcopy(doc)
    for path, value in PATH_TRANSLATIONS.items():
        set_by_path(result, path, value)
    return result


def build_output_path(src: Path, out_dir: Path | None, in_place: bool) -> Path:
    if in_place:
        return src
    if out_dir is not None:
        return out_dir / src.name
    return src.with_name(src.stem + '.translated' + src.suffix)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Translate systems-ru/5e/system/system.rpg.json user-visible strings to Russian.'
    )
    parser.add_argument('input', help='Path to system.rpg.json')
    parser.add_argument('--out-dir', type=Path, help='Directory for translated file')
    parser.add_argument('--in-place', action='store_true', help='Overwrite source file')
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f'File not found: {src}')

    data = load_json(src)
    translated = apply_translations(data)
    out_path = build_output_path(src, args.out_dir, args.in_place)
    save_json(out_path, translated)
    print(out_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
