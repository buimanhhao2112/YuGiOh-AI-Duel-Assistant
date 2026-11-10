"""Build examples/vs_k9_deck.json from the local MDM card export plus two fallback records."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MAIN_DECK = [
    ("Ash Blossom & Joyous Spring", 3),
    ("Bystial Magnamhut", 1),
    ("Dogoran, the Mad Flame Kaiju", 1),
    ("Ghost Belle & Haunted Mansion", 2),
    ("K9-04 Noroi", 1),
    ("K9-17 Izuna", 3),
    ("K9-66a Jokul", 2),
    ("K9-66b Lantern", 1),
    ("K9-00 Lupis", 1),
    ('Maxx "C"', 1),
    ("Mulcharmy Fuwalos", 3),
    ("Vanquish Soul Caesar Valius", 1),
    ("Vanquish Soul Dr. Mad Love", 3),
    ("Vanquish Soul Heavy Borger", 2),
    ("Vanquish Soul Hollie Sue", 3),
    ("Vanquish Soul Jiaolong", 2),
    ("Vanquish Soul Razen", 2),
    ('"A Case for K9"', 1),
    ("Chaotic Elements", 1),
    ("K9-X Forced Release", 1),
    ("Reinforcement of the Army", 1),
    ("Stake your Soul!", 1),
    ("Triple Tactics Talent", 1),
    ("Vanquish Soul, Start!", 1),
    ("Vanquish Soul Snow Devil", 1),
]

EXTRA_DECK = [
    ("Arc Rebellion Xyz Dragon", 1),
    ("CXyz N.As.Ch. Knight", 1),
    ("Divine Arsenal AA-ZEUS - Sky Thunder", 1),
    ('K9-17 "Ripper"', 2),
    ('K9-66X "Jacks"', 1),
    ('K9-X "Werewolf"', 1),
    ("N.As.H. Knight", 1),
    ("Number 104: Masquerade", 1),
    ("Number C104: Umbral Horror Masquerade", 1),
    ("Rock of the Vanquisher", 2),
    ("S:P Little Knight", 1),
    ("Saryuja Skull Dread", 1),
    ("Super Starslayer TY-PHON - Sky Crisis", 1),
]

FALLBACK_CARDS = {
    "K9-00 Lupis": {
        "card_id": "91025875",
        "name": "K9-00 Lupis",
        "card_type": "Monster",
        "frame_type": "Effect",
        "level": 5,
        "attribute": "EARTH",
        "race": "Beast-Warrior",
        "atk": 2300,
        "def": 200,
        "archetypes": ["K9"],
        "description": (
            "An Xyz Monster that has this card as material gains this effect: your opponent cannot target this card with card effects. "
            "During the Main Phase, if your opponent activated a monster effect in the hand or GY this turn (Quick Effect): "
            "Special Summon this card from your hand or GY, but banish it when it leaves the field. "
            "During your opponent's turn, you can immediately Xyz Summon using this card you control."
        ),
    },
    '"A Case for K9"': {
        "card_id": "80181649",
        "name": '"A Case for K9"',
        "card_type": "Spell",
        "frame_type": "Continuous",
        "attribute": None,
        "race": None,
        "atk": 0,
        "def": None,
        "archetypes": ["K9"],
        "ban_status": "Unlimited",
        "description": (
            'When this card is activated: add 1 "K9" monster from your Deck to your hand. '
            'K9 monsters you control gain 900 ATK during any turn in which your opponent activated a monster effect in the hand or GY. '
            'If this card in the Spell & Trap Zone is destroyed by card effect: Set 1 K9 Quick-Play Spell from your Deck or GY. '
            'You can only use this effect once per turn and only activate 1 per turn.'
        ),
    },
}

OPERATORS: dict[str, list[str]] = {
    "K9-04 Noroi": ["k9_noroi_summon"],
    "K9-17 Izuna": ["k9_izuna_send"],
    "K9-66a Jokul": ["k9_jokul_pair", "k9_jokul_search"],
    "K9-66b Lantern": ["k9_lantern_revive", "k9_lantern_search"],
    "K9-00 Lupis": ["k9_lupis_special_summon"],
    '"A Case for K9"': ["a_case_search"],
    "K9-X Forced Release": ["k9_forced_release"],
    'K9-17 "Ripper"': ["k9_ripper_search"],
    'K9-66X "Jacks"': ["k9_jacks_destroy"],
    'K9-X "Werewolf"': ["k9_werewolf_effect"],
    "N.As.H. Knight": ["nash_attach_number", "nash_rank_up"],
    "CXyz N.As.Ch. Knight": ["cxyz_number_summon"],
    "Vanquish Soul Caesar Valius": ["vs_tag_out", "vs_effect_earth", "vs_effect_earth_fire_dark"],
    "Vanquish Soul Dr. Mad Love": ["vs_search_spelltrap", "vs_effect_dark", "vs_effect_dark_earth"],
    "Vanquish Soul Heavy Borger": ["vs_tag_out", "vs_effect_dark", "vs_effect_earth_fire"],
    "Vanquish Soul Hollie Sue": ["vs_hollie_sue_summon", "vs_effect_earth_dark", "vs_effect_fire_dark"],
    "Vanquish Soul Jiaolong": ["vs_jiaolong_summon", "vs_effect_fire", "vs_effect_two_fire"],
    "Vanquish Soul Razen": ["vs_search_monster", "vs_effect_fire", "vs_effect_fire_dark"],
    "Stake your Soul!": ["vs_stake"],
    "Vanquish Soul, Start!": ["vs_start_search"],
    "Vanquish Soul Snow Devil": ["vs_snow_devil"],
    "Rock of the Vanquisher": ["vs_rock_effect"],
}

INTERRUPTION_REQUIREMENTS = {
    "Vanquish Soul Caesar Valius": [["EARTH", "FIRE", "DARK"]],
    "Vanquish Soul Dr. Mad Love": [["DARK", "EARTH"]],
    "Vanquish Soul Hollie Sue": [["EARTH", "DARK"]],
    "Vanquish Soul Razen": [["FIRE", "DARK"]],
}

LINK_RULES = {
    "Rock of the Vanquisher": (1, "vanquish_soul"),
    "S:P Little Knight": (2, "different_names"),
    "Saryuja Skull Dread": (2, "different_names_any"),
}

XYZ_RULES = {
    "Arc Rebellion Xyz Dragon": (5, 3, 5),
    "K9-17 \"Ripper\"": (5, 2, 5),
    "K9-66X \"Jacks\"": (5, 2, 5),
    "K9-X \"Werewolf\"": (9, 2, 9),
    "N.As.H. Knight": (5, 2, 5),
    "Number 104: Masquerade": (4, 3, 4),
    "Number C104: Umbral Horror Masquerade": (5, 4, 5),
    "CXyz N.As.Ch. Knight": (6, 3, 6),
    "Divine Arsenal AA-ZEUS - Sky Thunder": (12, 2, 12),
}

INTERRUPTIONS = {
    "K9-17 \"Ripper\"": {"negates": 1},
    "K9-66X \"Jacks\"": {"interruptions": 1},
    "K9-X \"Werewolf\"": {"interruptions": 1},
    "Vanquish Soul Caesar Valius": {"interruptions": 1},
    "Vanquish Soul Dr. Mad Love": {"interruptions": 1},
    "Vanquish Soul Hollie Sue": {"interruptions": 1},
    "Vanquish Soul Razen": {"interruptions": 1},
    "S:P Little Knight": {"interruptions": 1},
}


def normalize_definition(name: str, raw: dict[str, object]) -> dict[str, object]:
    frame = str(raw.get("frame_type") or raw.get("frameType") or "Effect")
    value = raw.get("level_rank_link")
    if value is None:
        value = raw.get("level") or raw.get("rank") or raw.get("linkRating")
    definition: dict[str, object] = {
        "name": name,
        "card_type": str(raw.get("type") or raw.get("card_type") or "Monster"),
        "attribute": raw.get("attribute"),
        "race": raw.get("race_type") or raw.get("race"),
        "atk": int(raw.get("atk") or 0),
        "effect_monster": str(raw.get("type") or raw.get("card_type")) == "Monster" and frame != "Normal",
        "operators": OPERATORS.get(name, []),
        "archetypes": [part.strip() for part in str(raw.get("archetype") or "").split(",") if part.strip()],
        "effect_text": str(raw.get("description") or ""),
    }
    if raw.get("ban_status") and raw.get("ban_status") != "Unlimited":
        definition["ban_status"] = raw["ban_status"]
    if frame == "Link":
        definition["link_rating"] = int(value or 0)
        definition["link_min_materials"], definition["link_requirement"] = LINK_RULES.get(name, (2, "effect_monster"))
        if name == "Rock of the Vanquisher":
            definition["cannot_be_link_material"] = True
    elif frame == "Xyz":
        definition["rank"] = int(value or 0)
        definition["xyz_min_materials"] = 2
        definition["xyz_material_level"] = int(value or 0)
        if name in XYZ_RULES:
            rank, minimum, material_level = XYZ_RULES[name]
            definition["rank"] = rank
            definition["xyz_min_materials"] = minimum
            definition["xyz_material_level"] = material_level
    elif str(raw.get("type") or raw.get("card_type")) == "Monster" and value is not None:
        definition["level"] = int(value)
        definition["normal_summon"] = int(value) <= 4
    if name in {"K9-04 Noroi", "K9-17 Izuna", "K9-66a Jokul", "K9-66b Lantern", "K9-00 Lupis"}:
        definition["normal_summon"] = True
        definition["normal_summon_requires_opponent_hand"] = 2
    if name in INTERRUPTION_REQUIREMENTS:
        definition["interruption_requirements"] = INTERRUPTION_REQUIREMENTS[name]
    definition.update(INTERRUPTIONS.get(name, {}))
    return definition


def main() -> None:
    mdm_rows = json.loads((ROOT / "data/cards.json").read_text(encoding="utf-8"))
    cards_by_name = {str(row["name"]): row for row in mdm_rows}
    all_names = {name for name, _ in (*MAIN_DECK, *EXTRA_DECK)}
    definitions: list[dict[str, object]] = []
    for name in sorted(all_names):
        row = cards_by_name.get(name)
        if name == "K9-00 Lupis":
            row = row or cards_by_name.get("K9-ØØ Lupis")
        row = row or FALLBACK_CARDS.get(name)
        if row is None:
            raise SystemExit(f"No MDM record or explicit fallback for {name!r}.")
        definitions.append(normalize_definition(name, row))
    deck = {
        "name": "Vanquish Soul x K9 (user-supplied list)",
        "format": "User-supplied 40/15 list",
        "note": "Counts match the supplied list exactly. The MCTS model implements selected VS attribute-reveal, K9 Level 5, Rank 4/5, and Link interactions; unimplemented effects remain descriptive metadata.",
        "sources": [
            "https://www.db.yugioh-card.com/yugiohdb/card_search.action?cid=21379&ope=2",
            "https://www.db.yugioh-card.com/yugiohdb/card_search.action?cid=21385&ope=2&request_locale=en",
            "https://www.db.yugioh-card.com/yugiohdb/card_search.action?cid=21376&ope=2",
        ],
        "main_deck": [{"name": name, "count": count} for name, count in MAIN_DECK],
        "extra_deck": [{"name": name, "count": count} for name, count in EXTRA_DECK],
        "card_definitions": definitions,
    }
    (ROOT / "examples/vs_k9_deck.json").write_text(
        json.dumps(deck, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {ROOT / 'examples/vs_k9_deck.json'}: {sum(c for _, c in MAIN_DECK)} Main / {sum(c for _, c in EXTRA_DECK)} Extra")


if __name__ == "__main__":
    main()
