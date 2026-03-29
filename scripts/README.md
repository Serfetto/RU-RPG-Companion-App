# How the translation was made

All translations, as well as the helper scripts written for them, were created **with the help of neural networks**.

Most of the translations were taken from **[dnd.su](https://dnd.su)** whenever suitable matches for the corresponding entities and texts could be found there.

If a required text could not be found on **[dnd.su](https://dnd.su)**, it was sent to a locally deployed LLM-based translator for Russian translation.

The following locally deployed model was used for translation automation: **[YandexGPT 5 Lite](https://huggingface.co/yandex/YandexGPT-5-Lite-8B-instruct-GGUF)**

Neural networks were used as a tool to speed up the localization process, after which the results were additionally reviewed and adapted to fit the structure of the application files.

# Running the translation scripts

Before running any script, clone the original systems repository that will be used as the translation target:

```bash
git clone https://github.com/blastervla/rpg-companion-app-systems
```

Install dependencies:

```bash
pip install requests beautifulsoup4
```

## Translator configuration

Most translators support a fallback local translator and allow configuring it either by editing the script or, where supported, through command-line arguments such as `--translator-url`, `--translator-timeout`, `--translator-batch-size`, or disabling it with `--no-translator`.

If needed, you can change the default translator endpoint directly inside the script by editing `DEFAULT_TRANSLATOR_URL`.

The `dndsu_enumerated_types_json_translator.py` script is a special case: in its current form it uses a hardcoded `TARGET` path inside the file instead of a command-line interface.

## Example commands

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

## Output mode

If you do not want to overwrite the original files, use `--out-dir translated` instead of `--in-place` where supported.

## Special case: enumerated types

For `dndsu_enumerated_types_json_translator.py`, edit the `TARGET` path inside the script first and then run it with Python.