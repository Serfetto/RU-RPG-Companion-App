from __future__ import annotations
import json
from pathlib import Path

TARGET = Path(r'C:\Users\Serfetto\Desktop\fff\rpg-companion-app\systems-ru\5e\system\enumerated_types')
EXCLUDED = {'dice.rpg.json', 'plus_minus.rpg.json', 'weight_units.rpg.json'}
SKIPPED = {'index.rpg.json'}

ABILITY_NAMES = {
    'strength': 'Сила',
    'dexterity': 'Ловкость',
    'constitution': 'Телосложение',
    'intelligence': 'Интеллект',
    'wisdom': 'Мудрость',
    'charisma': 'Харизма',
}
ABILITY_ABBRS = {
    'strength': 'Сил',
    'dexterity': 'Лов',
    'constitution': 'Тел',
    'intelligence': 'Инт',
    'wisdom': 'Мдр',
    'charisma': 'Хар',
}
ABILITY_GEN = {
    'strength': 'Силы',
    'dexterity': 'Ловкости',
    'constitution': 'Телосложения',
    'intelligence': 'Интеллекта',
    'wisdom': 'Мудрости',
    'charisma': 'Харизмы',
}
SKILLS = {
    'acrobatics': 'Акробатика',
    'animal_handling': 'Уход за животными',
    'arcana': 'Магия',
    'athletics': 'Атлетика',
    'deception': 'Обман',
    'history': 'История',
    'insight': 'Проницательность',
    'intimidation': 'Запугивание',
    'investigation': 'Анализ',
    'medicine': 'Медицина',
    'nature': 'Природа',
    'perception': 'Восприятие',
    'performance': 'Выступление',
    'persuasion': 'Убеждение',
    'religion': 'Религия',
    'sleight_of_hand': 'Ловкость рук',
    'stealth': 'Скрытность',
    'survival': 'Выживание',
}
ALIGNMENTS = {
    'lawful_good': 'Законно-добрый',
    'neutral_good': 'Нейтрально-добрый',
    'chaotic_good': 'Хаотично-добрый',
    'lawful_neutral': 'Законно-нейтральный',
    'true_neutral': 'Нейтральный',
    'chaotic_neutral': 'Хаотично-нейтральный',
    'lawful_evil': 'Законно-злой',
    'neutral_evil': 'Нейтрально-злой',
    'chaotic_evil': 'Хаотично-злой',
}


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def top(data, **fields):
    for key, value in fields.items():
        if value is not None:
            data[key] = value


def apply(data, mapping):
    seen = set()
    for row in data.get('types', []):
        rid = row['id']
        if rid not in mapping:
            raise KeyError(f'unexpected row id {rid!r} in {data.get("id")!r}')
        spec = mapping[rid]
        if isinstance(spec, str):
            row['name'] = spec
        else:
            for key, value in spec.items():
                row[key] = value
        seen.add(rid)
    missing = set(mapping) - seen
    if missing:
        raise KeyError(f'missing rows in {data.get("id")!r}: {sorted(missing)}')


def effect_advantage_label(rid):
    stat = rid.removeprefix('$character.')
    for ability in ABILITY_NAMES:
        if stat == f'{ability}_modifier':
            return ABILITY_NAMES[ability]
        if stat == f'{ability}_saving_throw':
            return f'Спасбросок {ABILITY_GEN[ability]}'
    if stat == 'initiative':
        return 'Инициатива'
    if stat in SKILLS:
        return SKILLS[stat]
    raise KeyError(rid)


def effect_stat_label(rid):
    stat = rid.removeprefix('$character.')
    for ability in ABILITY_NAMES:
        if stat == f'{ability}_score':
            return ABILITY_NAMES[ability]
        if stat == f'{ability}_saving_throw':
            return f'Спасбросок {ABILITY_GEN[ability]}'
    fixed = {
        'armor_class': 'КД',
        'passive_perception': 'Пассивное восприятие',
        'current_hp': 'Хиты',
        'max_hp': 'Макс. хиты',
        'initiative': 'Инициатива',
        'speed': 'Скорость',
        'burrow_speed': 'Скорость копания',
        'climb_speed': 'Скорость лазания',
        'swim_speed': 'Скорость плавания',
        'fly_speed': 'Скорость полета',
        'proficiency_bonus': 'Бонус мастерства',
        'level': 'Уровень',
        'spell_attack_extra_bonus': 'Доп. бонус к атаке заклинанием',
        'spell_dc_extra_bonus': 'Доп. бонус к Сл заклинаний',
    }
    if stat in fixed:
        return fixed[stat]
    if stat in SKILLS:
        return SKILLS[stat]
    if stat.startswith('spell_slots_'):
        level = stat.removeprefix('spell_slots_')
        return f'Ячейки заклинаний {level}-го уровня'
    if stat.startswith('max_spell_slots_') and stat.endswith('_bonus'):
        level = stat.removeprefix('max_spell_slots_').removesuffix('_bonus')
        return f'Бонус к макс. ячейкам {level}-го уровня'
    raise KeyError(rid)


def effect_mod_label(rid):
    if rid == 'constant':
        return 'Константа'
    stat = rid.removeprefix('$character.')
    for ability in ABILITY_NAMES:
        if stat == f'{ability}_modifier':
            return f'Модификатор {ABILITY_GEN[ability]}'
    raise KeyError(rid)


TRANSLATORS = {
    'ability.rpg.json': lambda d: (top(d, name='Характеристика'), apply(d, {k: {'name': ABILITY_NAMES[k], 'abbreviation': ABILITY_ABBRS[k]} for k in ABILITY_NAMES})),
    'ability_plus_none.rpg.json': lambda d: (top(d, name='Типы характеристик'), apply(d, {'none': 'Нет', **{k: ABILITY_NAMES[k] for k in ABILITY_NAMES}})),
    'ability_score_modes.rpg.json': lambda d: (top(d, name='Способы задания характеристик'), apply(d, {'custom': 'Вручную', 'point_buy': 'Покупка очков'})),
    'advantage_status.rpg.json': lambda d: (top(d, name='Статус преимущества'), apply(d, {'none': 'Обычный', 'advantage': 'Преимущество', 'disadvantage': 'Помеха'})),
    'alignments.rpg.json': lambda d: (top(d, name='Мировоззрения'), apply(d, ALIGNMENTS)),
    'ammunition_types.rpg.json': lambda d: (top(d, name='Боеприпасы'), apply(d, {'none': 'Нет', 'arrow': 'Стрела', 'bolt': 'Арбалетный болт', 'bullet': 'Снаряд для пращи', 'needle': 'Игла духовой трубки', 'firearm': 'Боеприпасы к огнестрельному оружию'})),
    'appearance_weapons.rpg.json': lambda d: (top(d, name='Облики оружия'), apply(d, {'battle-axe': 'Боевой топор', 'war-hammer': 'Боевой молот', 'dagger': 'Кинжал', 'sword': 'Меч', 'long_sword': 'Длинный меч', 'claymore': 'Клеймор', 'long_bow': 'Длинный лук', 'crossbow': 'Арбалет', 'mage_wand': 'Волшебная палочка', 'lute': 'Лютня', 'shield': 'Щит', 'javelin': 'Метательное копье', 'mace': 'Булава', 'spear': 'Копье', 'sickle': 'Серп', 'shortbow': 'Короткий лук', 'sling': 'Праща', 'flail': 'Цеп', 'glaive': 'Глефа', 'halberd': 'Алебарда', 'maul': 'Кувалда', 'morningstar': 'Моргенштерн', 'scimitar': 'Скимитар', 'trident': 'Трезубец', 'whip': 'Кнут', 'blunderbuss': 'Мушкетон', 'heavy_crossbow': 'Тяжелый арбалет', 'musket': 'Мушкет', 'hand_mortar': 'Ручная мортира', 'pistol': 'Пистолет'})),
    'armor_types.rpg.json': lambda d: (top(d, name='Типы доспехов'), apply(d, {'light': 'Легкий', 'medium': 'Средний', 'heavy': 'Тяжелый', 'shield': 'Щит'})),
    'charge_types.rpg.json': lambda d: (top(d, name='Типы зарядов'), apply(d, {'constant': 'Постоянный', 'calculated': 'Вычисляемый'})),
    'conditions.rpg.json': lambda d: (top(d, name='Операторы сравнения'), apply(d, {'lower_than': '<', 'lower_than_equal': '≤', 'equal': '=', 'not_equal': '≠', 'higher_than': '>', 'higher_than_equal': '≥'})),
    'currencies.rpg.json': lambda d: (top(d, name='Монеты'), apply(d, {'copper': 'мм', 'silver': 'см', 'electrum': 'эм', 'gold': 'зм', 'platinum': 'пм'})),
    'damage_types.rpg.json': lambda d: (top(d, name='Тип урона', plural='Типы урона'), apply(d, {'bludgeoning': 'Дробящий', 'piercing': 'Колющий', 'slashing': 'Рубящий', 'acid': 'Кислота', 'cold': 'Холод', 'fire': 'Огонь', 'force': 'Силовое поле', 'lightning': 'Электричество', 'necrotic': 'Некротическая энергия', 'poison': 'Яд', 'psychic': 'Психическая энергия', 'radiant': 'Излучение', 'thunder': 'Звук', 'magic': 'Магия'})),
    'death_saves_menu.rpg.json': lambda d: (top(d, name='Меню спасбросков от смерти'), apply(d, {'death_saves_stabilize': 'Стабилизировать', 'death_saves_roll': 'Бросок'})),
    'effective_caster_level_types.rpg.json': lambda d: (top(d, name='Типы эффективного уровня заклинателя'), apply(d, {'zero': 'Нулевой (0)', 'third': 'Треть (1/3)', 'half': 'Половина (1/2)', 'full': 'Полный (1)', 'custom': 'Пользовательский'})),
    'effect_advantage_character_stats.rpg.json': lambda d: (top(d, name='Характеристики для преимущества/помехи'), apply(d, {row['id']: effect_advantage_label(row['id']) for row in d.get('types', [])})),
    'effect_aggregation_types.rpg.json': lambda d: (top(d, name='Способы объединения эффектов'), apply(d, {'min': 'Минимум', 'max': 'Максимум', 'sum': 'Сумма', 'none': 'Выбрать вручную', 'set': 'Установить'})),
    'effect_character_stats.rpg.json': lambda d: (top(d, name='Характеристики эффекта'), apply(d, {row['id']: effect_stat_label(row['id']) for row in d.get('types', [])})),
    'effect_character_stats_plus_mods_and_constant.rpg.json': lambda d: (top(d, name='Характеристики эффекта'), apply(d, {row['id']: effect_mod_label(row['id']) for row in d.get('types', [])})),
    'effect_trigger_types.rpg.json': lambda d: (top(d, name='Типы срабатывания эффекта'), apply(d, {'passive': 'Пассивный', 'event': 'Событие', 'active': 'Активный'})),
    'effect_types.rpg.json': lambda d: (top(d, name='Типы эффектов'), apply(d, {'none': 'Без эффекта', 'add_to_stat': 'Добавить к характеристике', 'set_stat': 'Установить характеристику', 'disadvantage': 'Помеха', 'advantage': 'Преимущество', 'add_spell': 'Добавить заклинание', 'use_spell': 'Применить заклинание'})),
    'eye_colors.rpg.json': lambda d: (top(d, name='Цвета глаз'), apply(d, {'white_eyes': 'Белые глаза', 'black_eyes': 'Черные глаза', 'green_eyes': 'Зеленые глаза', 'orange_eyes': 'Оранжевые глаза', 'yellow_eyes': 'Желтые глаза', 'red_eyes': 'Красные глаза', 'blue_eyes': 'Синие глаза', 'turquoise_eyes': 'Бирюзовые глаза', 'violet_eyes': 'Фиолетовые глаза', 'hazel_eyes': 'Светло-карие глаза', 'brown_eyes': 'Карие глаза', 'amber_eyes': 'Янтарные глаза', 'gray_eyes': 'Серые глаза', 'teal_eyes': 'Сине-зеленые глаза', 'gold_eyes': 'Золотые глаза', 'silver_eyes': 'Серебристые глаза', 'copper_eyes': 'Медные глаза', 'maroon_eyes': 'Бордовые глаза'})),
    'hair_colors.rpg.json': lambda d: (top(d, name='Цвета волос'), apply(d, {'black_hair': 'Черные волосы', 'brown_hair': 'Каштановые волосы', 'blonde_hair': 'Светлые волосы', 'brunette_hair': 'Темно-каштановые волосы', 'gray_hair': 'Седые волосы', 'white_hair': 'Белые волосы', 'red_hair': 'Рыжие волосы', 'platinum_blonde_hair': 'Платиново-светлые волосы', 'auburn_hair': 'Каштаново-рыжие волосы', 'silver_hair': 'Серебристые волосы', 'golden_brown_hair': 'Золотисто-каштановые волосы', 'ash_blonde_hair': 'Пепельно-светлые волосы', 'blue_hair': 'Синие волосы', 'green_hair': 'Зеленые волосы', 'purple_hair': 'Фиолетовые волосы', 'pink_hair': 'Розовые волосы', 'teal_hair': 'Сине-зеленые волосы', 'turquoise_hair': 'Бирюзовые волосы', 'lavender_hair': 'Лавандовые волосы', 'mint_green hair': 'Мятно-зеленые волосы', 'magenta_hair': 'Пурпурные волосы', 'indigo_hair': 'Индиговые волосы', 'neon_green_hair': 'Неоново-зеленые волосы'})),
    'hit_point_modes.rpg.json': lambda d: (top(d, name='Способы определения хитов'), apply(d, {'roll': 'Бросок', 'average': 'Среднее', 'max': 'Максимум'})),
    'item_rarity.rpg.json': lambda d: (top(d, name='Редкость'), apply(d, {'none': 'Нет', 'unknown': 'Неизвестно', 'varies': 'Различается', 'common': 'Обычный', 'uncommon': 'Необычный', 'rare': 'Редкий', 'very_rare': 'Очень редкий', 'legendary': 'Легендарный', 'unique': 'Уникальный', 'artifact': 'Артефакт'})),
    'item_types.rpg.json': lambda d: (top(d, name='Типы предметов'), apply(d, {'adventuring_gear': 'Снаряжение для приключений', 'ammunition': 'Боеприпасы', 'armor': 'Доспех', 'artisans_tools': 'Инструменты ремесленника', 'food_and_drink': 'Еда и напитки', 'explosive': 'Взрывчатка', 'gaming_set': 'Игровой набор', 'instrument': 'Музыкальный инструмент', 'mount': 'Ездовое животное', 'other': 'Прочее', 'poison': 'Яд', 'potion': 'Зелье', 'ring': 'Кольцо', 'rod': 'Жезл', 'rune': 'Руна', 'scroll': 'Свиток', 'spellcasting_focus': 'Магическая фокусировка', 'staff': 'Посох', 'tack_and_harness': 'Сбруя и упряжь', 'tool': 'Инструмент', 'trade_good': 'Товар', 'vehicle_air': 'Транспорт (воздушный)', 'vehicle_land': 'Транспорт (наземный)', 'vehicle_space': 'Транспорт (космический)', 'vehicle_water': 'Транспорт (водный)', 'wand': 'Волшебная палочка', 'weapon': 'Оружие', 'wondrous': 'Чудесный предмет'})),
    'lifecycle_events.rpg.json': lambda d: (top(d, name='События жизненного цикла'), apply(d, {'long_rest': 'длительный отдых', 'short_rest': 'короткий отдых', 'dusk': 'закат', 'dawn': 'рассвет', 'manual': 'вручную'})),
    'lifecycle_events_menu.rpg.json': lambda d: (top(d, name='Меню событий жизненного цикла'), apply(d, {'trigger_long_rest': 'Длительный отдых', 'trigger_short_rest': 'Короткий отдых', 'dusk': 'Установить время на закат', 'dawn': 'Установить время на рассвет'})),
    'min_max_of.rpg.json': lambda d: (top(d, name='Минимум / максимум'), apply(d, {'min': 'Минимум', 'max': 'Максимум'})),
    'monster_action_types.rpg.json': lambda d: (top(d, name='Типы атак монстров'), apply(d, {'none': 'Нет', 'melee': 'Рукопашная', 'ranged': 'Дальнобойная', 'melee_or_ranged': 'Рукопашная или дальнобойная'})),
    'monster_alignments.rpg.json': lambda d: (top(d, name='Мировоззрения монстров'), apply(d, {'unaligned': 'Без мировоззрения'})),
    'monster_cr_types.rpg.json': lambda d: (top(d, name='Опасность монстров'), apply(d, {row['id']: f"Опасность {row['id'].removeprefix('cr_').replace('_', '/')}" for row in d.get('types', [])})),
    'monster_habitats.rpg.json': lambda d: (top(d, name='Среда обитания монстров'), apply(d, {'arctic': 'Арктика', 'coastal': 'Побережье', 'desert': 'Пустыня', 'forest': 'Лес', 'grassland': 'Луг', 'hill': 'Холмы', 'mountain': 'Горы', 'swamp': 'Болото', 'underdark': 'Подземье', 'underwater': 'Под водой', 'urban': 'Город', 'planar_abyss': 'Бездна', 'planar_acheron': 'Ахерон', 'planar_air': 'Стихийный план Воздуха', 'planar_arborea': 'Арборея', 'planar_arcadia': 'Аркадия', 'planar_astral': 'Астральный план', 'planar_beastlands': 'Звериные земли', 'planar_bytopia': 'Битопия', 'planar_carceri': 'Карцери', 'planar_earth': 'Стихийный план Земли', 'planar_elemental': 'Стихийные планы', 'planar_elemental_chaos': 'Стихийный хаос', 'planar_elysium': 'Элизиум', 'planar_ethereal': 'Эфирный план', 'planar_feywild': 'Царство Фей', 'planar_fire': 'Стихийный план Огня', 'planar_gehenna': 'Геенна', 'planar_hades': 'Гадес', 'planar_limbo': 'Лимбо', 'planar_lower': 'Нижние планы', 'planar_mechanus': 'Механус', 'planar_celestia': 'Гора Селестия', 'planar_nine_hells': 'Девять Преисподних', 'planar_pandemonium': 'Пандемониум', 'planar_shadowfell': 'Царство Теней', 'planar_water': 'Стихийный план Воды', 'planar_upper': 'Верхние планы', 'planar_ysgard': 'Исгард'})),
    'monster_sizes.rpg.json': lambda d: (top(d, name='Размеры монстров'), apply(d, {'tiny': 'Крошечный', 'small': 'Маленький', 'medium': 'Средний', 'large': 'Большой', 'huge': 'Огромный', 'gargantuan': 'Громадный'})),
    'monster_types.rpg.json': lambda d: (top(d, name='Типы монстров'), apply(d, {'aberration': 'Аберрация', 'beast': 'Зверь', 'celestial': 'Небожитель', 'construct': 'Конструкт', 'dragon': 'Дракон', 'elemental': 'Элементаль', 'fey': 'Фея', 'fiend': 'Исчадие', 'giant': 'Великан', 'humanoid': 'Гуманоид', 'monstrosity': 'Монстр', 'ooze': 'Слизь', 'plant': 'Растение', 'undead': 'Нежить'})),
    'outfits.rpg.json': lambda d: (top(d, name='Наряды'), apply(d, {'wizard': 'Волшебник', 'sorcerer': 'Чародей', 'heavy_armor': 'Тяжелый доспех', 'light_armor': 'Легкий доспех', 'cloak': 'Плащ', 'casual_wear': 'Повседневная одежда', 'formal_wear': 'Нарядная одежда', 'robe': 'Мантия'})),
    'recharge_types.rpg.json': lambda d: (top(d, name='Типы восстановления'), apply(d, {'all': 'Все', 'formula': 'Формула'})),
    'sexes.rpg.json': lambda d: (top(d, name='Пол'), apply(d, {'female': 'Женский', 'male': 'Мужской'})),
    'skill_proficiency_types.rpg.json': lambda d: (top(d, name='Типы владения навыком'), apply(d, {'none': 'Нет', 'proficient': 'Владение', 'expert': 'Экспертиза'})),
    'skill_types.rpg.json': lambda d: (top(d, name='Навыки'), apply(d, SKILLS)),
    'sort_skill_proficiencies_menu.rpg.json': lambda d: (top(d, name='Сортировка навыков'), apply(d, {'sort_by_name': 'По имени', 'sort_by_ability': 'По характеристике'})),
    'spell_level.rpg.json': lambda d: (top(d, name='Уровень заклинания', plural='Уровни заклинаний', abbreviation='Ур. закл.'), apply(d, {'spell_level_0': {'name': 'Заговор', 'plural': 'Заговоры', 'abbreviation': 'Ур. 0'}, **{f'spell_level_{i}': {'name': f'{i}-й уровень', 'plural': f'{i}-й уровень', 'abbreviation': f'Ур. {i}'} for i in range(1, 10)}})),
    'spell_school.rpg.json': lambda d: (top(d, name='Школа магии', plural='Школы магии'), apply(d, {'spell_school_abjuration': {'name': 'Ограждение', 'abbreviation': 'Огр.'}, 'spell_school_conjuration': {'name': 'Вызов', 'abbreviation': 'Выз.'}, 'spell_school_divination': {'name': 'Прорицание', 'abbreviation': 'Прор.'}, 'spell_school_enchantment': {'name': 'Очарование', 'abbreviation': 'Очар.'}, 'spell_school_evocation': {'name': 'Воплощение', 'abbreviation': 'Вопл.'}, 'spell_school_illusion': {'name': 'Иллюзия', 'abbreviation': 'Илл.'}, 'spell_school_necromancy': {'name': 'Некромантия', 'abbreviation': 'Некр.'}, 'spell_school_transmutation': {'name': 'Преобразование', 'abbreviation': 'Преобр.'}})),
    'weapon_general_types.rpg.json': lambda d: (top(d, name='Категории оружия'), apply(d, {'simple_melee': 'Простое рукопашное', 'simple_ranged': 'Простое дальнобойное', 'martial_melee': 'Воинское рукопашное', 'martial_ranged': 'Воинское дальнобойное'})),
    'weapon_types.rpg.json': lambda d: (top(d, name='Типы оружия'), apply(d, {'club': 'Дубинка', 'dagger': 'Кинжал', 'greatclub': 'Дубина', 'handaxe': 'Одноручный топор', 'javelin': 'Метательное копье', 'light_hammer': 'Легкий молот', 'mace': 'Булава', 'quarterstaff': 'Боевой посох', 'sickle': 'Серп', 'spear': 'Копье', 'firearm': 'Огнестрельное оружие', 'crossbow_light': 'Легкий арбалет', 'dart': 'Дротик', 'shortbow': 'Короткий лук', 'sling': 'Праща', 'battleaxe': 'Боевой топор', 'flail': 'Цеп', 'glaive': 'Глефа', 'greataxe': 'Секира', 'greatsword': 'Двуручный меч', 'halberd': 'Алебарда', 'lance': 'Кавалерийское копье', 'longsword': 'Длинный меч', 'maul': 'Кувалда', 'morningstar': 'Моргенштерн', 'pike': 'Пика', 'rapier': 'Рапира', 'scimitar': 'Скимитар', 'shortsword': 'Короткий меч', 'trident': 'Трезубец', 'war_pick': 'Клевец', 'warhammer': 'Боевой молот', 'whip': 'Кнут', 'blowgun': 'Духовая трубка', 'crossbow_hand': 'Ручной арбалет', 'crossbow_heavy': 'Тяжелый арбалет', 'longbow': 'Длинный лук', 'net': 'Сеть', 'unarmed_strike': 'Безоружный удар'})),
    'weapon_types_plus_all_types.rpg.json': lambda d: (top(d, name='Типы оружия и группы'), apply(d, {'all_martial': 'Все воинские', 'all_simple': 'Все простые'})),
}

translated = skipped = excluded = 0
for path in sorted(TARGET.glob('*.rpg.json')):
    if path.name in EXCLUDED:
        excluded += 1
        continue
    if path.name in SKIPPED:
        skipped += 1
        continue
    if path.name not in TRANSLATORS:
        raise KeyError(f'No translator for {path.name}')
    data = load(path)
    if not isinstance(data, dict):
        raise TypeError(f'Expected object json in {path.name}')
    TRANSLATORS[path.name](data)
    save(path, data)
    translated += 1

print(f'Translated: {translated}')
print(f'Skipped: {skipped}')
print(f'Excluded: {excluded}')
