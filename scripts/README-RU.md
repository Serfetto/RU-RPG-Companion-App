# Как выполнялся перевод

Весь перевод, а также написанные для него вспомогательные скрипты, создавались **с помощью нейросетей**.

Основная часть переводов бралась с сайта **[dnd.su](https://dnd.su)**, когда для соответствующих сущностей и текстов удавалось найти подходящие совпадения.

Если нужный текст не удавалось найти на **[dnd.su](https://dnd.su)**, он отправлялся в локально развернутый переводчик на базе LLM для перевода на русский язык.

Для автоматизации перевода использовалась локально развернутая модель: **[YandexGPT 5 Lite](https://huggingface.co/yandex/YandexGPT-5-Lite-8B-instruct-GGUF)**

Нейросети использовались как инструмент ускорения локализации, после чего результаты дополнительно проверялись и адаптировались под структуру файлов приложения.

# Запуск скриптов перевода

Перед запуском любого скрипта нужно склонировать оригинальный репозиторий систем, который используется как цель для перевода:

```bash
git clone https://github.com/blastervla/rpg-companion-app-systems
```

Установите зависимости:

```bash
pip install requests beautifulsoup4
```

## Настройка переводчика

Большинство переводчиков поддерживают fallback на локальный переводчик и позволяют настраивать его либо через редактирование скрипта, либо, если это поддерживается, через аргументы командной строки: `--translator-url`, `--translator-timeout`, `--translator-batch-size`, либо отключать его через `--no-translator`.

При необходимости можно изменить адрес переводчика прямо внутри скрипта, отредактировав `DEFAULT_TRANSLATOR_URL`.

Скрипт `dndsu_enumerated_types_json_translator.py` — особый случай: в текущем виде он использует захардкоженный путь `TARGET` внутри файла вместо интерфейса командной строки.

## Примеры запуска

### Feats
```bash
python scripts/dndsu_feat_json_translator.py "../rpg-companion-app-systems/path/to/feat_*.rpg.json" --in-place
```

### Classes
```bash
python scripts/dndsu_class_json_translator.py "../rpg-companion-app-systems/path/to/class_*.rpg.json" --in-place
```

### Races
```bash
python scripts/dndsu_race_json_translator.py "../rpg-companion-app-systems/path/to/race_*.json" --in-place
```

### Spells
```bash
python scripts/dndsu_spell_json_translator.py "../rpg-companion-app-systems/path/to/spell_*.json" --in-place
```

### Items
```bash
python scripts/dndsu_item_json_translator.py "../rpg-companion-app-systems/path/to/item_*.rpg.json" --in-place
```

### Weapons
```bash
python scripts/dndsu_weapon_json_translator.py "../rpg-companion-app-systems/path/to/weapon_*.rpg.json" --in-place
```

### Armor
```bash
python scripts/dndsu_armor_json_translator.py "../rpg-companion-app-systems/path/to/armor_*.json" --in-place
```

### Backgrounds
```bash
python scripts/dndsu_background_json_translator.py "../rpg-companion-app-systems/path/to/background_*.json" --in-place
```

### Character sheet sections
```bash
python scripts/dndsu_character_sheet_sections_translator.py "../rpg-companion-app-systems/systems-ru/5e/system/character_sheet_sections/*.rpgs" --in-place
```

### Character stats
```bash
python scripts/dndsu_character_stats_translator.py "../rpg-companion-app-systems/systems-ru/5e/system/character_stats.rpgs" --in-place
```

### System rpg
```bash
python scripts/dndsu_system_rpg_json_translator.py "../rpg-companion-app/systems-ru/5e/system/system.rpg.json" --in-place
```

## Режим вывода

Если не хочется перезаписывать оригинальные файлы, используйте `--out-dir translated` вместо `--in-place`, если конкретный скрипт это поддерживает.

## Особый случай: enumerated types

Для `dndsu_enumerated_types_json_translator.py` сначала нужно вручную отредактировать путь `TARGET` внутри скрипта, а затем просто запустить его через Python.