"""Monte Carlo Tree Search over a compact, declarative Snake-Eye rules model.

This is an explainable research simulator. It models the listed operators, not all
Master Duel rulings; use ocgcore to validate any generated line before gameplay.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import combinations, product
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from engine.disruptions import disruption_profile
from mdm_banlist import MAX_COPIES, normalize_status


@dataclass(frozen=True, slots=True)
class CardSpec:
    name: str
    card_type: str = "Monster"
    level: int | None = None
    attribute: str | None = None
    race: str | None = None
    atk: int = 0
    effect_monster: bool = False
    normal_summon: bool = False
    operators: tuple[str, ...] = ()
    archetypes: tuple[str, ...] = ()
    link_rating: int | None = None
    link_min_materials: int = 0
    link_requirement: str = "effect_monster"
    cannot_be_link_material: bool = False
    rank: int | None = None
    xyz_min_materials: int = 0
    xyz_material_level: int | None = None
    normal_summon_requires_opponent_hand: int = 0
    negates: int = 0
    interruptions: int = 0
    interruption_requirements: tuple[tuple[str, ...], ...] = ()
    ban_status: str = "Unknown"
    can_be_special_summoned_from_gy: bool = True
    can_be_special_summoned_by_effect: bool = True


@dataclass(frozen=True, slots=True)
class FieldUnit:
    name: str
    summoned_normally: bool = False
    materials: tuple[str, ...] = ()
    material_kind: str = ""
    temporary: bool = False
    k9_extra_lock: bool = False
    cannot_be_material_for_light: bool = False


@dataclass(frozen=True, slots=True)
class SpellUnit:
    name: str
    face_up: bool
    set_by_effect: bool = False


@dataclass(frozen=True, slots=True)
class SimState:
    hand: tuple[str, ...]
    deck: tuple[str, ...]
    field: tuple[FieldUnit, ...] = ()
    spell_trap_zone: tuple[SpellUnit, ...] = ()
    graveyard: tuple[str, ...] = ()
    banished: tuple[str, ...] = ()
    extra_deck: tuple[str, ...] = ()
    effects_used: tuple[str, ...] = ()
    pending_summons: tuple[str, ...] = ()
    added_to_hand: tuple[str, ...] = ()
    sent_to_gy: tuple[str, ...] = ()
    normal_summon_used: bool = False
    fire_lock: bool = False
    resources_used: int = 0
    opponent_hand_count: int = 5
    opponent_hand_effect_activated: bool = False
    opponent_field_count: int = 0
    modeled_disruptions: int = 0
    modeled_board_breaks: int = 0


@dataclass(frozen=True, slots=True)
class Action:
    kind: str
    card: str
    target: str = ""
    costs: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    reveals: tuple[str, ...] = ()
    source: str = ""
    description: str = ""

    def __str__(self) -> str:
        return self.description or f"{self.kind}: {self.card}"


@dataclass(slots=True)
class MCTSResult:
    actions: list[Action]
    final_state: SimState
    score: float
    score_components: dict[str, float]
    simulations: int
    expanded_nodes: int
    target_card: str | None = None


@dataclass(slots=True)
class DeckModel:
    name: str
    main_deck: tuple[str, ...]
    extra_deck: tuple[str, ...]
    cards: dict[str, CardSpec]
    banlist_loaded: bool = False
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SearchNode:
    __slots__ = (
        "state",
        "parent",
        "action",
        "children",
        "untried_actions",
        "visits",
        "value_sum",
        "path",
    )

    def __init__(
        self,
        state: SimState,
        parent: SearchNode | None = None,
        action: Action | None = None,
        path: tuple[Action, ...] = (),
    ):
        self.state = state
        self.parent = parent
        self.action = action
        self.children: list[SearchNode] = []
        self.untried_actions: list[Action] | None = None
        self.visits = 0
        self.value_sum = 0.0
        self.path = path

    @property
    def mean_value(self) -> float:
        return self.value_sum / self.visits if self.visits else 0.0


def _card_from_json(value: Mapping[str, Any]) -> CardSpec:
    return CardSpec(
        name=str(value["name"]),
        card_type=str(value.get("card_type", "Monster")),
        level=value.get("level"),
        attribute=value.get("attribute"),
        race=value.get("race"),
        atk=int(value.get("atk", 0)),
        effect_monster=bool(value.get("effect_monster", False)),
        normal_summon=bool(value.get("normal_summon", False)),
        operators=tuple(value.get("operators", ())),
        archetypes=tuple(value.get("archetypes", ())),
        link_rating=value.get("link_rating"),
        link_min_materials=int(value.get("link_min_materials", 0)),
        link_requirement=str(value.get("link_requirement", "effect_monster")),
        cannot_be_link_material=bool(value.get("cannot_be_link_material", False)),
        rank=value.get("rank"),
        xyz_min_materials=int(value.get("xyz_min_materials", 0)),
        xyz_material_level=value.get("xyz_material_level"),
        normal_summon_requires_opponent_hand=int(value.get("normal_summon_requires_opponent_hand", 0)),
        negates=int(value.get("negates", 0)),
        interruptions=int(value.get("interruptions", 0)),
        interruption_requirements=tuple(tuple(group) for group in value.get("interruption_requirements", ())),
        ban_status=str(value.get("ban_status", "Unknown")),
        can_be_special_summoned_from_gy=bool(value.get("can_be_special_summoned_from_gy", True)),
        can_be_special_summoned_by_effect=bool(value.get("can_be_special_summoned_by_effect", True)),
    )


def load_deck(path: Path, banlist_path: Path | None = None) -> DeckModel:
    raw = json.loads(path.read_text(encoding="utf-8"))
    definitions = {row["name"]: _card_from_json(row) for row in raw["card_definitions"]}
    banlist_loaded = False
    limits: dict[str, int] = {}
    for name, spec in definitions.items():
        status = normalize_status(spec.ban_status)
        if status:
            limits[name.casefold()] = MAX_COPIES[status]
    if banlist_path and banlist_path.exists():
        snapshot = json.loads(banlist_path.read_text(encoding="utf-8"))
        records = snapshot.get("cards", snapshot) if isinstance(snapshot, Mapping) else snapshot
        if isinstance(records, list):
            for row in records:
                if isinstance(row, Mapping) and row.get("name"):
                    status = normalize_status(row.get("ban_status") or row.get("status"))
                    if status:
                        limits[str(row["name"]).casefold()] = MAX_COPIES[status]
            definitions = {
                name: replace(
                    spec,
                    ban_status=(
                        next(
                            status
                            for status, maximum in MAX_COPIES.items()
                            if maximum == limits[name.casefold()]
                        )
                        if name.casefold() in limits
                        else spec.ban_status
                    ),
                )
                for name, spec in definitions.items()
            }
            banlist_loaded = True
    warnings: list[str] = []
    used: Counter[str] = Counter()

    def expand(entries: list[Mapping[str, Any]]) -> list[str]:
        result: list[str] = []
        for entry in entries:
            name = str(entry["name"])
            requested = int(entry["count"])
            key = name.casefold()
            maximum = limits.get(key, 3)
            allowed = max(0, min(requested, maximum - used[key]))
            if allowed < requested:
                warnings.append(
                    f"Filtered {requested - allowed}x {name} to respect the current copy limit."
                )
            result.extend([name] * allowed)
            used[key] += allowed
        return result

    main_cards = expand(raw["main_deck"])
    extra_cards = expand(raw["extra_deck"])
    return DeckModel(
        name=str(raw.get("name", path.stem)),
        main_deck=tuple(main_cards),
        extra_deck=tuple(extra_cards),
        cards=definitions,
        banlist_loaded=banlist_loaded,
        sources=list(raw.get("sources", [])),
        warnings=warnings,
    )


def make_initial_state(
    deck: DeckModel,
    starting_hand: Iterable[str],
    *,
    opponent_hand_count: int = 5,
    opponent_hand_effect_activated: bool = False,
    opponent_field_count: int = 0,
) -> SimState:
    hand = list(starting_hand)
    main = list(deck.main_deck)
    for name in hand:
        try:
            main.remove(name)
        except ValueError as exc:
            raise ValueError(f"{name!r} is not available in the main deck.") from exc
    unknown = sorted({name for name in (*hand, *main, *deck.extra_deck) if name not in deck.cards})
    if unknown:
        raise ValueError(f"Missing card definitions: {', '.join(unknown)}")
    return SimState(
        hand=tuple(hand),
        deck=tuple(main),
        extra_deck=deck.extra_deck,
        opponent_hand_count=opponent_hand_count,
        opponent_hand_effect_activated=opponent_hand_effect_activated,
        opponent_field_count=opponent_field_count,
    )


def _used(state: SimState, card: str, effect: str) -> bool:
    return f"{card}::{effect}" in state.effects_used


def _mark_used(state: SimState, card: str, effect: str) -> tuple[str, ...]:
    return tuple(sorted(set(state.effects_used) | {f"{card}::{effect}"}))


def _can_special_summon(state: SimState, spec: CardSpec) -> bool:
    return not state.fire_lock or spec.attribute == "FIRE"


def _remove_one(items: tuple[str, ...], name: str) -> tuple[str, ...]:
    result = list(items)
    result.remove(name)
    return tuple(result)


def _to_gy(state: SimState, cards: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    cards = tuple(cards)
    return state.graveyard + cards, state.sent_to_gy + cards


def _new_summon(state: SimState, name: str, *, normal: bool, from_deck: bool) -> SimState:
    deck = state.deck
    hand = state.hand
    if name in hand:
        hand = _remove_one(hand, name)
    elif name in deck:
        deck = _remove_one(deck, name)
    pending = state.pending_summons + (name,)
    return SimState(
        hand=hand,
        deck=deck,
        field=state.field + (FieldUnit(name, summoned_normally=normal),),
        spell_trap_zone=state.spell_trap_zone,
        graveyard=state.graveyard,
        banished=state.banished,
        extra_deck=state.extra_deck,
        effects_used=state.effects_used,
        pending_summons=pending,
        added_to_hand=state.added_to_hand,
        sent_to_gy=state.sent_to_gy,
        normal_summon_used=state.normal_summon_used or normal,
        fire_lock=state.fire_lock,
        resources_used=state.resources_used + int(from_deck),
        opponent_hand_count=state.opponent_hand_count,
        opponent_hand_effect_activated=state.opponent_hand_effect_activated,
        opponent_field_count=state.opponent_field_count,
        modeled_disruptions=state.modeled_disruptions,
        modeled_board_breaks=state.modeled_board_breaks,
    )


def _action(
    kind: str,
    card: str,
    *,
    target: str = "",
    costs: tuple[str, ...] = (),
    materials: tuple[str, ...] = (),
    reveals: tuple[str, ...] = (),
    source: str = "",
    description: str,
) -> Action:
    return Action(kind, card, target, costs, materials, reveals, source, description)


def _link_material_options(
    state: SimState, spec: CardSpec, cards: dict[str, CardSpec]
) -> Iterable[tuple[tuple[int, ...], tuple[str, ...]]]:
    candidates = [
        (index, unit, cards[unit.name])
        for index, unit in enumerate(state.field)
    ]
    for size in range(max(1, spec.link_min_materials), min(4, len(candidates)) + 1):
        for subset in combinations(candidates, size):
            names = tuple(unit.name for _, unit, _ in subset)
            if spec.link_requirement == "different_names" and len(set(names)) != len(names):
                continue
            if spec.link_requirement == "vanquish_soul" and any(
                not unit.name.startswith("Vanquish Soul") for _, unit, _ in subset
            ):
                continue
            if spec.link_requirement == "normal_summoned_low_atk":
                if any(
                    not unit.summoned_normally or deck_spec.atk > 1000
                    for _, unit, deck_spec in subset
                ):
                    continue
            if spec.link_requirement == "level_1":
                if any(deck_spec.level != 1 for _, _, deck_spec in subset):
                    continue
            if spec.link_requirement == "fire_included":
                if not any(deck_spec.attribute == "FIRE" for _, _, deck_spec in subset):
                    continue
            if spec.link_requirement == "fire_monsters":
                if len(subset) < 2 or any(deck_spec.attribute != "FIRE" for _, _, deck_spec in subset):
                    continue
            valid = True
            value_options: list[tuple[int, ...]] = []
            for _, unit, _ in subset:
                material_spec = cards[unit.name]
                if material_spec.cannot_be_link_material:
                    valid = False
                    break
                if (
                    spec.link_requirement != "normal_summoned_low_atk"
                    and not material_spec.effect_monster
                ):
                    valid = False
                    break
                if material_spec.link_rating:
                    value_options.append((1, material_spec.link_rating))
                else:
                    value_options.append((1,))
            if not valid:
                continue
            for values in product(*value_options):
                if sum(values) == spec.link_rating:
                    yield tuple(index for index, _, _ in subset), names
                    break


def _is_k9(name: str) -> bool:
    return name.startswith("K9-") or name == '"A Case for K9"'


def _is_vs(name: str) -> bool:
    return name.startswith("Vanquish Soul")


def _xyz_material_options(
    state: SimState, spec: CardSpec, cards: dict[str, CardSpec]
) -> Iterable[tuple[str, ...]]:
    required_level = spec.xyz_material_level
    minimum = max(2, spec.xyz_min_materials)
    candidates = [
        unit.name
        for unit in state.field
        if cards[unit.name].level == required_level
        and (spec.attribute != "LIGHT" or not unit.cannot_be_material_for_light)
    ]
    for size in range(minimum, len(candidates) + 1):
        for subset in combinations(candidates, size):
            yield tuple(subset)


def _hand_cards_with_attribute(
    state: SimState, cards: dict[str, CardSpec], attribute: str
) -> tuple[str, ...]:
    return tuple(name for name in state.hand if cards[name].attribute == attribute)


def legal_actions(state: SimState, cards: dict[str, CardSpec]) -> list[Action]:
    """Enumerate modeled legal actions from the current abstract state."""
    actions: list[Action] = []

    if state.opponent_field_count > 0:
        for name in state.hand:
            profile = disruption_profile(name)
            if profile and profile.get("action") == "kaiju_tribute":
                actions.append(
                    _action(
                        "kaiju_tribute",
                        name,
                        target="opponent monster",
                        description=f"Special Summon [{name}] to the opponent's field by Tributing 1 opponent monster",
                    )
                )

    if len(state.field) < 5 and not state.normal_summon_used:
        for name in state.hand:
            spec = cards[name]
            normal_rule = (spec.level or 0) <= 4 or (
                spec.normal_summon_requires_opponent_hand > 0
                and state.opponent_hand_count >= spec.normal_summon_requires_opponent_hand
            )
            if spec.card_type == "Monster" and spec.normal_summon and normal_rule:
                actions.append(
                    _action(
                        "normal_summon",
                        name,
                        description=f"Normal Summon [{name}]",
                    )
                )

    # K9-66a summons itself and another Level 5 K9 monster from the hand.
    if len(state.field) <= 3:
        for name in state.hand:
            if "k9_jokul_pair" not in cards[name].operators or _used(state, name, "pair_summon"):
                continue
            for other in state.hand:
                if other == name or cards[other].card_type != "Monster" or cards[other].level != 5:
                    continue
                actions.append(
                    _action(
                        "k9_jokul_pair",
                        name,
                        target=other,
                        reveals=(name, other),
                        description=f"Reveal [{name}] and [{other}]; Special Summon both Level 5 monsters",
                    )
                )

    # K9-66b can pair with a Level 5 non-Pyro K9 monster in the GY.
    if len(state.field) < 5:
        for name in state.hand:
            if "k9_lantern_revive" not in cards[name].operators or _used(state, name, "revive"):
                continue
            for target in state.graveyard:
                target_spec = cards[target]
                if _is_k9(target) and target_spec.level == 5 and target_spec.race != "Pyro":
                    actions.append(
                        _action(
                            "k9_lantern_revive",
                            name,
                            target=target,
                            reveals=(name,),
                            description=f"Reveal [{name}]; Special Summon it and [{target}] from the GY",
                        )
                    )

    # K9 quick summons require the opponent to have used a monster effect in hand/GY this turn.
    if state.opponent_hand_effect_activated and len(state.field) < 5:
        for name in state.hand:
            if "k9_lupis_special_summon" in cards[name].operators and not _used(state, name, "quick_summon"):
                actions.append(
                    _action(
                        "k9_lupis_special_summon",
                        name,
                        description=f"Special Summon [{name}] from hand; banish it when it leaves the field",
                    )
                )
            if "k9_izuna_send" in cards[name].operators and not _used(state, name, "quick_summon"):
                actions.append(
                    _action(
                        "k9_izuna_special_summon",
                        name,
                        description=f"Special Summon [{name}] from hand after the opponent's hand/GY monster effect",
                    )
                )
        if not _used(state, "K9-00 Lupis", "quick_summon"):
            for name in state.graveyard:
                if "k9_lupis_special_summon" in cards[name].operators:
                    actions.append(
                        _action(
                            "k9_lupis_special_summon", name, source="GY",
                            description=f"Special Summon [{name}] from GY after the opponent's hand/GY monster effect",
                        )
                    )

    # VS reveal effects reveal attribute cards and keep them in the hand.
    if len(state.field) < 5:
        for name in state.hand:
            spec = cards[name]
            if "vs_hollie_sue_summon" not in spec.operators or _used(state, name, "hand_summon"):
                continue
            for reveal in state.hand:
                if reveal != name and _is_vs(reveal):
                    actions.append(
                        _action(
                            "vs_hollie_sue_summon",
                            name,
                            reveals=(reveal,),
                            description=f"Reveal [{reveal}] in hand; Special Summon [{name}]",
                        )
                    )

    # Stake your Soul! reveals a monster without discarding it, then summons a different VS of its attribute.
    if "Stake your Soul!" in state.hand and not _used(state, "Stake your Soul!", "activate") and len(state.field) < 5:
        for reveal in state.hand:
            if cards[reveal].card_type != "Monster" or cards[reveal].attribute is None:
                continue
            for target in state.deck:
                if _is_vs(target) and target != reveal and cards[target].attribute == cards[reveal].attribute:
                    actions.append(
                        _action(
                            "vs_stake",
                            "Stake your Soul!",
                            target=target,
                            reveals=(reveal,),
                            description=(
                                f"Activate [Stake your Soul!]; reveal [{reveal}] (keeps its attribute in hand); "
                                f"Special Summon [{target}] from Deck"
                            ),
                        )
                    )

    # VS tag-out monsters can replace a differently typed VS monster with themselves from hand.
    for name in state.hand:
        spec = cards[name]
        if "vs_tag_out" not in spec.operators or _used(state, name, "tag_out") or len(state.field) > 4:
            continue
        for unit in state.field:
            target_spec = cards[unit.name]
            if _is_vs(unit.name) and target_spec.race != spec.race:
                actions.append(
                    _action(
                        "vs_tag_out",
                        name,
                        target=unit.name,
                        costs=(unit.name,),
                        description=f"Return [{unit.name}] to hand; Special Summon [{name}] from hand",
                    )
                )

    # Search/activate the Continuous Spell and VS field search spell.
    for name in state.hand:
        if name == '"A Case for K9"' and len(state.spell_trap_zone) < 5 and not _used(state, name, "activate"):
            for target in state.deck:
                if _is_k9(target) and cards[target].card_type == "Monster":
                    actions.append(
                        _action("a_case_activate", name, target=target,
                                description=f"Activate [{name}]; add [{target}] from Deck to hand")
                    )

    for name in state.hand:
        if name == "Vanquish Soul, Start!" and len(state.spell_trap_zone) < 5 and not _used(state, name, "activate"):
            for unit in state.field:
                if not _is_vs(unit.name):
                    continue
                for target in state.deck:
                    if _is_vs(target) and cards[target].attribute != cards[unit.name].attribute:
                        actions.append(
                            _action(
                                "vs_start_search", name, target=target, costs=(unit.name,),
                                description=f"Activate [{name}] targeting [{unit.name}]; add [{target}] from Deck",
                            )
                        )

    # Diabellstar: send one other card from hand or face-up field to summon itself.
    for name in state.hand:
        spec = cards[name]
        if "diabellstar_self_summon" in spec.operators and not _used(state, name, "self_summon"):
            costs = [(card, "hand") for card in state.hand if card != name]
            costs.extend((unit.name, "field") for unit in state.field)
            for cost, cost_source in costs:
                if _can_special_summon(state, spec):
                    actions.append(
                        _action(
                            "diabellstar_self_summon",
                            name,
                            costs=(cost,),
                            source=cost_source,
                            description=(
                                f"Activate [{name}] from hand; send [{cost}] from {cost_source} to the GY as cost; "
                                f"Special Summon [{name}] from hand"
                            ),
                        )
                    )

    for name in state.hand:
        spec = cards[name]
        if "wanted_search" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                if "Diabellstar" in target:
                    actions.append(
                        _action(
                            "wanted_search",
                            name,
                            target=target,
                            source="Deck",
                            description=f"Activate [{name}]; add [{target}] from Deck to hand",
                        )
                    )
            for target in state.graveyard:
                if "Diabellstar" in target:
                    actions.append(
                        _action(
                            "wanted_search",
                            name,
                            target=target,
                            source="GY",
                            description=f"Activate [{name}]; add [{target}] from GY to hand",
                        )
                    )

        if "oss_activate" in spec.operators and not _used(state, name, "card_once_per_turn"):
            sources = ["hand"] if name in state.hand else []
            if any(unit.name == name for unit in state.spell_trap_zone):
                sources.append("spell")
            face_up_costs = [unit.name for unit in state.field]
            face_up_costs.extend(unit.name for unit in state.spell_trap_zone if unit.face_up and unit.name != name)
            for source in sources:
                for cost in face_up_costs:
                    for target in state.deck + state.hand:
                        target_spec = cards[target]
                        if (
                            target_spec.level == 1
                            and target_spec.attribute == "FIRE"
                            and target_spec.can_be_special_summoned_by_effect
                            and target != name
                        ):
                            if _can_special_summon(state, target_spec):
                                actions.append(
                                    _action(
                                        "oss_activate",
                                        name,
                                        target=target,
                                        costs=(cost,),
                                        source=source,
                                        description=(
                                            f"Activate [{name}]; send [{cost}] from field to GY as cost; "
                                            f"Special Summon [{target}] from {source.title() if source == 'hand' else 'Deck/hand'}"
                                        ),
                                    )
                                )

    for spell in state.spell_trap_zone:
        name = spell.name
        spec = cards[name]
        if "wanted_search" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                if "Diabellstar" in target:
                    actions.append(
                        _action(
                            "wanted_search", name, target=target, source="Deck",
                            description=f"Activate set [{name}]; add [{target}] from Deck to hand",
                        )
                    )
            for target in state.graveyard:
                if "Diabellstar" in target:
                    actions.append(
                        _action(
                            "wanted_search", name, target=target, source="GY",
                            description=f"Activate set [{name}]; add [{target}] from GY to hand",
                        )
                    )

    # Effects that trigger when these monsters are summoned.
    for name in state.pending_summons:
        spec = cards[name]
        if (
            "k9_noroi_summon" in spec.operators
            and any(unit.name == name and unit.summoned_normally for unit in state.field)
            and not _used(state, name, "deck_summon")
        ):
            for target in state.deck:
                target_spec = cards[target]
                if _is_k9(target) and target != name and target_spec.card_type == "Monster" and target_spec.race != "Machine":
                    actions.append(
                        _action(
                            "k9_noroi_summon", name, target=target,
                            description=f"Activate [{name}] on summon; Special Summon [{target}] from Deck",
                        )
                    )
        if "k9_izuna_send" in spec.operators and not _used(state, name, "send"):
            for target in state.deck:
                if _is_k9(target) and target != name:
                    actions.append(
                        _action(
                            "k9_izuna_send", name, target=target,
                            description=f"Activate [{name}] on summon; send [{target}] from Deck to GY",
                        )
                    )
        if "k9_jokul_search" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                if _is_k9(target) and cards[target].card_type == "Monster" and cards[target].race != "Aqua":
                    actions.append(
                        _action("k9_jokul_search", name, target=target,
                                description=f"Activate [{name}]; add non-Aqua [{target}] from Deck to hand")
                    )
        if "k9_lantern_search" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                if _is_k9(target) and cards[target].card_type in {"Spell", "Trap"}:
                    actions.append(
                        _action("k9_lantern_search", name, target=target,
                                description=f"Activate [{name}]; add [{target}] from Deck to hand")
                    )
        if "vs_search_monster" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                if _is_vs(target) and target != name and cards[target].card_type == "Monster" and cards[target].race != "Warrior":
                    actions.append(
                        _action("vs_search_monster", name, target=target,
                                description=f"Activate [{name}] on summon; add non-Warrior [{target}] from Deck to hand")
                    )
        if "vs_search_spelltrap" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                if _is_vs(target) and cards[target].card_type in {"Spell", "Trap"}:
                    actions.append(
                        _action("vs_search_spelltrap", name, target=target,
                                description=f"Activate [{name}] on summon; add [{target}] from Deck to hand")
                    )
        if "snake_eye_search_level1_fire" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                target_spec = cards[target]
                if target_spec.level == 1 and target_spec.attribute == "FIRE":
                    actions.append(
                        _action(
                            "snake_eye_search",
                            name,
                            target=target,
                            description=f"Activate [{name}] on summon; add [{target}] from Deck to hand",
                        )
                    )
        if "diabellstar_set_sinful" in spec.operators and not _used(state, name, "set"):
            for target in state.deck:
                target_spec = cards[target]
                if "Sinful Spoils" in target or "Sinful Spoils" in target_spec.archetypes:
                    actions.append(
                        _action(
                            "diabellstar_set",
                            name,
                            target=target,
                            description=f"Activate [{name}] on summon; Set [{target}] from Deck",
                        )
                    )
        if "poplar_search_spell_trap" in spec.operators and not _used(state, name, "search"):
            for target in state.deck:
                target_spec = cards[target]
                if target_spec.card_type in {"Spell", "Trap"} and "Snake-Eye" in target_spec.archetypes:
                    actions.append(
                        _action(
                            "poplar_search",
                            name,
                            target=target,
                            description=f"Activate [{name}] on summon; add [{target}] from Deck to hand",
                        )
                    )
        if "oak_revive_level1_fire" in spec.operators and not _used(state, name, "revive"):
            for target in state.graveyard:
                target_spec = cards[target]
                if (
                    target_spec.can_be_special_summoned_from_gy
                    and target_spec.level == 1
                    and target_spec.attribute == "FIRE"
                    and _can_special_summon(state, target_spec)
                ):
                    actions.append(
                        _action(
                            "oak_revive",
                            name,
                            target=target,
                            description=f"Activate [{name}] on summon; Special Summon [{target}] from GY",
                        )
                    )

    for name in state.hand:
        spec = cards[name]
        if (
            "poplar_ss_if_added" in spec.operators
            and name in state.added_to_hand
            and not _used(state, name, "free_summon")
            and _can_special_summon(state, spec)
            and len(state.field) < 5
        ):
            actions.append(
                _action(
                    "poplar_free_summon",
                    name,
                    description=f"Activate [{name}] because it was added to hand; Special Summon it",
                )
            )

    # Snake-Eye Ash/Oak can send themselves plus another face-up card to summon a Snake-Eye.
    for unit in state.field:
        name = unit.name
        spec = cards[name]
        if "snake_eye_send_two_ss" not in spec.operators or _used(state, name, "send_two"):
            continue
        other_costs = [candidate.name for candidate in state.field if candidate.name != name]
        other_costs.extend(candidate.name for candidate in state.spell_trap_zone if candidate.face_up)
        targets = [
            card
            for card in state.deck + state.hand
            if card != name and "Snake-Eye" in cards[card].archetypes
        ]
        for cost in other_costs:
            for target in targets:
                if _can_special_summon(state, cards[target]):
                    actions.append(
                        _action(
                            "snake_eye_send_two_ss",
                            name,
                            target=target,
                            costs=(name, cost),
                            description=(
                                f"Activate [{name}]; send [{name}] and [{cost}] to GY as cost; "
                                f"Special Summon [{target}] from Deck/hand"
                            ),
                        )
                    )

    # Poplar can become a face-up Continuous Spell after it reaches the GY.
    for name in state.sent_to_gy:
        spec = cards[name]
        if (
            name in state.graveyard
            and "poplar_gy_set" in spec.operators
            and not _used(state, name, "gy_set")
        ):
            for target in state.graveyard:
                if cards[target].attribute == "FIRE":
                    actions.append(
                        _action(
                            "poplar_gy_set",
                            name,
                            target=target,
                            description=(
                                f"Activate [{name}] in GY; place [{target}] face-up in the Spell/Trap Zone"
                            ),
                        )
                    )

    # Named GY follow-up effects.
    for name in state.graveyard:
        spec = cards[name]
        if "wanted_gy_draw" in spec.operators and not _used(state, name, "gy_draw"):
            for target in state.graveyard + state.banished:
                if target != name and "Sinful Spoils" in target:
                    actions.append(
                        _action(
                            "wanted_gy_draw",
                            name,
                            target=target,
                            description=(
                                f"Banish [{name}] from GY; return [{target}] to bottom of Deck; draw 1"
                            ),
                        )
                    )
        if "oss_gy_search" in spec.operators and not _used(state, name, "card_once_per_turn"):
            return_targets = [
                target
                for target in state.graveyard
                if target != name and ("Snake-Eye" in cards[target].archetypes or "Diabellstar" in target)
            ]
            search_targets = [
                target
                for target in state.deck
                if cards[target].level == 1 and cards[target].attribute == "FIRE"
            ]
            for return_target in return_targets:
                for search_target in search_targets:
                    actions.append(
                        _action(
                            "oss_gy_search",
                            name,
                            target=search_target,
                            costs=(return_target,),
                            description=(
                                f"Banish [{name}] from GY; return [{return_target}] to Deck; "
                                f"add [{search_target}] from Deck to hand"
                            ),
                        )
                    )

    # Promethean Princess's modeled revival is its Link Summon trigger.
    for unit in state.field:
        spec = cards[unit.name]
        if (
            unit.name in state.pending_summons
            and "promethean_revive_fire" in spec.operators
            and not _used(state, unit.name, "revive")
        ):
            for target in state.graveyard:
                if (
                    cards[target].can_be_special_summoned_from_gy
                    and cards[target].attribute == "FIRE"
                    and _can_special_summon(state, cards[target])
                ):
                    actions.append(
                        _action(
                            "promethean_revive",
                            unit.name,
                            target=target,
                            description=f"Activate [{unit.name}]; Special Summon [{target}] from GY",
                        )
                    )

    # VS reveal effects preserve attribute cards in hand; those attributes also enable live interrupts.
    for unit in state.field:
        spec = cards[unit.name]
        if "vs_hollie_sue_summon" in spec.operators and not _used(state, unit.name, "fire_dark_summon"):
            fire_names = _hand_cards_with_attribute(state, cards, "FIRE")
            dark_names = _hand_cards_with_attribute(state, cards, "DARK")
            for fire_name in fire_names:
                for dark_name in dark_names:
                    if fire_name == dark_name:
                        continue
                    for target in state.deck:
                        if _is_vs(target) and cards[target].race != "Psychic":
                            actions.append(
                                _action(
                                    "vs_hollie_sue_deck_summon", unit.name, target=target,
                                    reveals=(fire_name, dark_name),
                                    description=(f"Reveal [{fire_name}] (FIRE) and [{dark_name}] (DARK); "
                                                 f"Special Summon [{target}] from Deck")
                                )
                            )
        if "vs_effect_two_fire" in spec.operators and not _used(state, unit.name, "two_fire_search"):
            fire_names = _hand_cards_with_attribute(state, cards, "FIRE")
            for revealed in combinations(fire_names, 2):
                for target in state.deck:
                    if _is_vs(target):
                        actions.append(
                            _action(
                                "vs_two_fire_search", unit.name, target=target, reveals=tuple(revealed),
                                description=(f"Reveal two FIRE monsters [{revealed[0]}] and [{revealed[1]}]; "
                                             f"add [{target}] from Deck")
                            )
                        )
        if unit.name == 'K9-17 "Ripper"' and unit.materials and not _used(state, unit.name, "search"):
            detached = unit.materials[0]
            for target in state.deck:
                if _is_k9(target):
                    actions.append(
                        _action(
                            "k9_ripper_search", unit.name, target=target, costs=(detached,),
                            description=f"Detach [{detached}] from [{unit.name}]; add [{target}] from Deck to hand",
                        )
                    )

    if len(state.field) < 5 and any(
        key.startswith("Stake your Soul!") or key.startswith("Vanquish Soul") for key in state.effects_used
    ):
        for name in state.hand:
            if "vs_jiaolong_summon" in cards[name].operators and not _used(state, name, "free_summon"):
                actions.append(
                    _action("vs_jiaolong_summon", name,
                            description=f"Special Summon [{name}] from hand after a Vanquish Soul activation")
                )

    # Generic Xyz Summon from matching Level 4/5 bodies and declared material counts.
    for extra_name in state.extra_deck:
        spec = cards[extra_name]
        k9_locked = any(unit.k9_extra_lock for unit in state.field)
        if spec.rank and spec.xyz_min_materials and spec.ban_status != "Forbidden":
            if k9_locked and not _is_k9(extra_name):
                continue
            for material_names in _xyz_material_options(state, spec, cards):
                actions.append(
                    _action(
                        "xyz_summon", extra_name, materials=material_names,
                        description=f"Xyz Summon [{extra_name}] using [{', '.join(material_names)}]",
                    )
                )
        if (
            extra_name == "Super Starslayer TY-PHON - Sky Crisis"
            and state.opponent_field_count >= 2
            and state.field
            and not k9_locked
        ):
            for unit in state.field:
                actions.append(
                    _action(
                        "typhon_summon",
                        extra_name,
                        materials=(unit.name,),
                        description=f"Xyz Summon [{extra_name}] over [{unit.name}]; bounce an opponent's high-ATK monster",
                    )
                )
        if extra_name == "CXyz N.As.Ch. Knight":
            if k9_locked:
                continue
            for unit in state.field:
                if unit.name == "N.As.H. Knight":
                    actions.append(
                        _action(
                            "nash_rank_up", extra_name, target=unit.name,
                            materials=unit.materials,
                            description=f"Xyz Summon [{extra_name}] using [{unit.name}] and its attached materials",
                        )
                    )

    if state.opponent_field_count > 0:
        for unit in state.field:
            if unit.name == 'K9-66X "Jacks"' and not _used(state, unit.name, "destroy"):
                actions.append(
                    _action(
                        "k9_jacks_destroy",
                        unit.name,
                        target="opponent monster",
                        description=f"Detach 1 material from [{unit.name}] to destroy an opponent monster",
                    )
                )
            if (
                unit.name == "S:P Little Knight"
                and state.opponent_hand_effect_activated
                and not _used(state, unit.name, "banish_both")
            ):
                for own_target in state.field:
                    if own_target.name != unit.name and not own_target.temporary:
                        actions.append(
                            _action(
                                "sp_banish",
                                unit.name,
                                target="opponent monster",
                                materials=(own_target.name,),
                                description=(f"Activate [{unit.name}]; banish [{own_target.name}] and an opponent monster "
                                             "until the End Phase"),
                            )
                        )

    # K9-X Forced Release converts one K9 Xyz into a differently named K9 Xyz and carries materials over.
    if "K9-X Forced Release" in state.hand and not _used(state, "K9-X Forced Release", "activate"):
        for unit in state.field:
            if not (_is_k9(unit.name) and cards[unit.name].rank):
                continue
            for extra_name in state.extra_deck:
                if _is_k9(extra_name) and cards[extra_name].rank and extra_name != unit.name:
                    actions.append(
                        _action(
                            "k9_forced_release", extra_name, target=unit.name,
                            description=f"Activate [K9-X Forced Release]; Xyz Summon [{extra_name}] using [{unit.name}] and transfer its materials",
                        )
                    )

    # Generic Link Summon from declared material requirements and Link ratings.
    if len(state.field) >= 1:
        for extra_name in state.extra_deck:
            spec = cards[extra_name]
            if not spec.link_rating or spec.ban_status == "Forbidden":
                continue
            if any(unit.k9_extra_lock for unit in state.field) and not _is_k9(extra_name):
                continue
            if state.fire_lock and spec.attribute != "FIRE":
                continue
            for indexes, material_names in _link_material_options(state, spec, cards):
                if len(state.field) - len(indexes) + 1 > 5:
                    continue
                actions.append(
                    _action(
                        "link_summon",
                        extra_name,
                        materials=material_names,
                        description=(
                            f"Link Summon [{extra_name}] using [{', '.join(material_names)}]"
                        ),
                    )
                )
    trigger_kinds = {
        "k9_noroi_summon",
        "k9_izuna_send",
        "k9_jokul_search",
        "k9_lantern_search",
        "vs_search_monster",
        "vs_search_spelltrap",
        "snake_eye_search",
        "diabellstar_set",
        "poplar_search",
        "oak_revive",
        "poplar_free_summon",
        "poplar_gy_set",
        "promethean_revive",
        "sp_banish",
    }
    pending_triggers = [action for action in actions if action.kind in trigger_kinds]
    return pending_triggers or actions


def apply_action(state: SimState, action: Action, cards: dict[str, CardSpec]) -> SimState:
    """Apply one action emitted by legal_actions, including its simplified resolution."""
    hand, deck = state.hand, state.deck
    field = list(state.field)
    spells = list(state.spell_trap_zone)
    gy, banished, extra = state.graveyard, state.banished, state.extra_deck
    effects_used = state.effects_used
    pending = state.pending_summons
    added = state.added_to_hand
    sent = state.sent_to_gy
    normal_used = state.normal_summon_used
    fire_lock = state.fire_lock
    resources = state.resources_used
    opponent_hand_count = state.opponent_hand_count
    opponent_hand_effect_activated = state.opponent_hand_effect_activated
    opponent_field_count = state.opponent_field_count
    modeled_disruptions = state.modeled_disruptions
    modeled_board_breaks = state.modeled_board_breaks

    def take_from_deck(name: str) -> None:
        nonlocal deck, resources
        deck = _remove_one(deck, name)
        resources += 1

    def take_from_hand_or_deck(name: str) -> None:
        nonlocal hand, deck, resources
        if name in hand:
            hand = _remove_one(hand, name)
        else:
            take_from_deck(name)

    def send(names: Iterable[str]) -> None:
        nonlocal field, spells, gy, sent, pending
        for name in names:
            found = False
            for index, unit in enumerate(field):
                if unit.name == name:
                    field.pop(index)
                    found = True
                    break
            if not found:
                for index, spell in enumerate(spells):
                    if spell.name == name:
                        spells.pop(index)
                        found = True
                        break
            if found:
                gy += (name,)
                sent += (name,)
                if not any(unit.name == name for unit in field):
                    pending = tuple(pending_name for pending_name in pending if pending_name != name)

    def add_to_hand(name: str) -> None:
        nonlocal hand, deck, gy, added, resources
        if name in deck:
            deck = _remove_one(deck, name)
            resources += 1
        elif name in gy:
            gy = _remove_one(gy, name)
        hand += (name,)
        added += (name,)

    def remove_spell(name: str) -> bool:
        nonlocal spells
        for index, spell in enumerate(spells):
            if spell.name == name:
                spells.pop(index)
                return True
        return False

    def summon(
        name: str,
        *,
        from_deck: bool = True,
        temporary: bool = False,
        k9_extra_lock: bool = False,
    ) -> None:
        nonlocal hand, deck, field, pending, resources
        if name in hand:
            hand = _remove_one(hand, name)
        elif name in deck:
            deck = _remove_one(deck, name)
            resources += int(from_deck)
        field.append(FieldUnit(name, temporary=temporary, k9_extra_lock=k9_extra_lock))
        pending += (name,)

    if action.kind == "kaiju_tribute":
        hand = _remove_one(hand, action.card)
        opponent_field_count = max(0, opponent_field_count - 1)
        modeled_board_breaks += 1
        resources += 1
    elif action.kind == "normal_summon":
        hand = _remove_one(hand, action.card)
        field.append(FieldUnit(action.card, summoned_normally=True))
        pending += (action.card,)
        normal_used = True
    elif action.kind == "k9_jokul_pair":
        hand = _remove_one(hand, action.card)
        hand = _remove_one(hand, action.target)
        field.extend((
            FieldUnit(action.card, cannot_be_material_for_light=True),
            FieldUnit(action.target, cannot_be_material_for_light=True),
        ))
        pending += (action.card, action.target)
        effects_used = _mark_used(state, action.card, "pair_summon")
    elif action.kind == "k9_lantern_revive":
        hand = _remove_one(hand, action.card)
        gy = _remove_one(gy, action.target)
        field.extend((
            FieldUnit(action.card, cannot_be_material_for_light=True),
            FieldUnit(action.target, cannot_be_material_for_light=True),
        ))
        pending += (action.card, action.target)
        effects_used = _mark_used(state, action.card, "revive")
    elif action.kind in {"k9_lupis_special_summon", "k9_izuna_special_summon", "vs_hollie_sue_summon", "vs_jiaolong_summon"}:
        if action.source == "GY":
            gy = _remove_one(gy, action.card)
        else:
            hand = _remove_one(hand, action.card)
        field.append(FieldUnit(action.card))
        pending += (action.card,)
        effect_key = "free_summon" if action.kind == "vs_jiaolong_summon" else "quick_summon" if action.kind.startswith("k9_") else "hand_summon"
        effects_used = _mark_used(state, action.card, effect_key)
    elif action.kind == "vs_stake":
        hand = _remove_one(hand, action.card)
        gy += (action.card,)
        summon(action.target, temporary=True)
        effects_used = _mark_used(state, action.card, "activate")
    elif action.kind == "vs_tag_out":
        hand = _remove_one(hand, action.card)
        field = [unit for unit in field if unit.name != action.target]
        hand += (action.target,)
        field.append(FieldUnit(action.card))
        pending += (action.card,)
        effects_used = _mark_used(state, action.card, "tag_out")
    elif action.kind == "a_case_activate":
        hand = _remove_one(hand, action.card)
        spells.append(SpellUnit(action.card, face_up=True))
        add_to_hand(action.target)
        effects_used = _mark_used(state, action.card, "activate")
    elif action.kind == "vs_start_search":
        hand = _remove_one(hand, action.card)
        gy += (action.card,)
        add_to_hand(action.target)
        effects_used = _mark_used(state, action.card, "activate")
    elif action.kind == "k9_noroi_summon":
        summon(action.target, k9_extra_lock=True)
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "deck_summon")
    elif action.kind == "k9_izuna_send":
        take_from_deck(action.target)
        gy += (action.target,)
        sent += (action.target,)
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "send")
    elif action.kind in {"k9_jokul_search", "k9_lantern_search", "vs_search_monster", "vs_search_spelltrap", "vs_two_fire_search", "k9_ripper_search"}:
        if action.kind == "k9_ripper_search":
            detached = action.costs[0]
            unit_index = next(
                i for i, unit in enumerate(field)
                if unit.name == action.card and detached in unit.materials
            )
            unit = field[unit_index]
            materials = list(unit.materials)
            materials.remove(detached)
            field[unit_index] = replace(unit, materials=tuple(materials))
            gy += (detached,)
            sent += (detached,)
        add_to_hand(action.target)
        pending = tuple(name for name in pending if name != action.card)
        effect_key = "two_fire_search" if action.kind == "vs_two_fire_search" else "search"
        effects_used = _mark_used(state, action.card, effect_key)
    elif action.kind == "vs_hollie_sue_deck_summon":
        summon(action.target)
        effects_used = _mark_used(state, action.card, "fire_dark_summon")
    elif action.kind == "xyz_summon":
        remaining_names = list(action.materials)
        kept: list[FieldUnit] = []
        for unit in field:
            if unit.name in remaining_names:
                remaining_names.remove(unit.name)
            else:
                kept.append(unit)
        field = kept
        pending = tuple(name for name in pending if any(unit.name == name for unit in kept))
        extra = _remove_one(extra, action.card)
        field.append(FieldUnit(action.card, materials=action.materials, material_kind="xyz"))
        resources += 1
        if action.card == "Super Starslayer TY-PHON - Sky Crisis" and opponent_field_count > 0:
            opponent_field_count -= 1
            modeled_board_breaks += 1
    elif action.kind == "typhon_summon":
        material = action.materials[0]
        field = [unit for unit in field if unit.name != material]
        gy += (material,)
        sent += (material,)
        extra = _remove_one(extra, action.card)
        field.append(FieldUnit(action.card, materials=(material,), material_kind="xyz"))
        resources += 1
        opponent_field_count = max(0, opponent_field_count - 1)
        modeled_board_breaks += 1
    elif action.kind in {"nash_rank_up", "k9_forced_release"}:
        source_unit = next(unit for unit in field if unit.name == action.target)
        field = [unit for unit in field if unit.name != action.target]
        extra = _remove_one(extra, action.card)
        field.append(
            FieldUnit(
                action.card,
                materials=(source_unit.name,) + source_unit.materials,
                material_kind="xyz",
            )
        )
        if action.kind == "k9_forced_release":
            hand = _remove_one(hand, "K9-X Forced Release")
            gy += ("K9-X Forced Release",)
            effects_used = _mark_used(state, "K9-X Forced Release", "activate")
        resources += 1
    elif action.kind == "k9_jacks_destroy":
        unit_index = next(i for i, unit in enumerate(field) if unit.name == action.card)
        unit = field[unit_index]
        if unit.materials:
            detached = unit.materials[0]
            field[unit_index] = replace(unit, materials=unit.materials[1:])
            gy += (detached,)
            sent += (detached,)
        modeled_disruptions += 1
        opponent_field_count = max(0, opponent_field_count - 1)
        modeled_board_breaks += 1
        effects_used = _mark_used(state, action.card, "destroy")
    elif action.kind == "sp_banish":
        own_target = action.materials[0]
        field = [unit for unit in field if unit.name != own_target]
        banished += (own_target,)
        opponent_field_count = max(0, opponent_field_count - 1)
        modeled_disruptions += 1
        modeled_board_breaks += 1
        effects_used = _mark_used(state, action.card, "banish_both")
    elif action.kind == "diabellstar_self_summon":
        if action.source == "hand":
            cost = action.costs[0]
            hand = _remove_one(hand, cost)
            gy += (cost,)
            sent += (cost,)
        else:
            send(action.costs)
        hand = _remove_one(hand, action.card)
        field.append(FieldUnit(action.card))
        pending += (action.card,)
        effects_used = _mark_used(state, action.card, "self_summon")
    elif action.kind == "wanted_search":
        if action.card in hand:
            hand = _remove_one(hand, action.card)
        else:
            remove_spell(action.card)
        gy += (action.card,)
        if action.source == "Deck":
            add_to_hand(action.target)
        else:
            add_to_hand(action.target)
        effects_used = _mark_used(state, action.card, "search")
    elif action.kind == "diabellstar_set":
        spells.append(SpellUnit(action.target, face_up=False, set_by_effect=True))
        deck = _remove_one(deck, action.target)
        resources += 1
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "set")
    elif action.kind == "oss_activate":
        send(action.costs)
        if action.source == "hand":
            hand = _remove_one(hand, action.card)
        else:
            remove_spell(action.card)
        gy += (action.card,)
        summon(action.target, from_deck=action.target in deck)
        effects_used = _mark_used(state, action.card, "card_once_per_turn")
    elif action.kind == "snake_eye_search":
        add_to_hand(action.target)
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "search")
    elif action.kind == "poplar_free_summon":
        hand = _remove_one(hand, action.card)
        field.append(FieldUnit(action.card))
        pending += (action.card,)
        added = tuple(name for name in added if name != action.card)
        effects_used = _mark_used(state, action.card, "free_summon")
    elif action.kind == "poplar_search":
        add_to_hand(action.target)
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "search")
    elif action.kind == "snake_eye_send_two_ss":
        send(action.costs)
        summon(action.target, from_deck=action.target in deck)
        effects_used = _mark_used(state, action.card, "send_two")
    elif action.kind == "oak_revive":
        gy = _remove_one(gy, action.target)
        field.append(FieldUnit(action.target))
        pending += (action.target,)
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "revive")
    elif action.kind == "poplar_gy_set":
        gy = _remove_one(gy, action.target)
        spells.append(SpellUnit(action.target, face_up=True))
        sent = tuple(name for name in sent if name != action.card)
        effects_used = _mark_used(state, action.card, "gy_set")
    elif action.kind == "wanted_gy_draw":
        banished += (action.card,)
        gy = _remove_one(gy, action.card)
        if action.target in gy:
            gy = _remove_one(gy, action.target)
            deck += (action.target,)
        elif action.target in banished:
            banished = _remove_one(banished, action.target)
            deck += (action.target,)
        if deck:
            hand += (deck[0],)
            deck = deck[1:]
        effects_used = _mark_used(state, action.card, "gy_draw")
    elif action.kind == "oss_gy_search":
        banished += (action.card,)
        gy = _remove_one(gy, action.card)
        return_target = action.costs[0]
        gy = _remove_one(gy, return_target)
        deck += (return_target,)
        add_to_hand(action.target)
        effects_used = _mark_used(state, action.card, "card_once_per_turn")
    elif action.kind == "promethean_revive":
        gy = _remove_one(gy, action.target)
        field.append(FieldUnit(action.target))
        pending += (action.target,)
        pending = tuple(name for name in pending if name != action.card)
        effects_used = _mark_used(state, action.card, "revive")
        fire_lock = True
    elif action.kind == "link_summon":
        remaining_names = list(action.materials)
        kept: list[FieldUnit] = []
        for unit in field:
            if unit.name in remaining_names:
                remaining_names.remove(unit.name)
                gy += (unit.name,)
                sent += (unit.name,)
            else:
                kept.append(unit)
        field = kept
        pending = tuple(name for name in pending if any(unit.name == name for unit in kept))
        extra = _remove_one(extra, action.card)
        field.append(FieldUnit(action.card, materials=action.materials, material_kind="link"))
        resources += 1
        pending += (action.card,)
    else:
        raise ValueError(f"Unsupported action kind {action.kind!r}.")

    return SimState(
        hand=hand,
        deck=deck,
        field=tuple(field),
        spell_trap_zone=tuple(spells),
        graveyard=gy,
        banished=banished,
        extra_deck=extra,
        effects_used=effects_used,
        pending_summons=pending,
        added_to_hand=added,
        sent_to_gy=sent,
        normal_summon_used=normal_used,
        fire_lock=fire_lock,
        resources_used=resources,
        opponent_hand_count=opponent_hand_count,
        opponent_hand_effect_activated=opponent_hand_effect_activated,
        opponent_field_count=opponent_field_count,
        modeled_disruptions=modeled_disruptions,
        modeled_board_breaks=modeled_board_breaks,
    )


def evaluate_board(
    state: SimState,
    cards: dict[str, CardSpec],
    *,
    opponent_hand: int = 5,
    weights: Mapping[str, float] | None = None,
    target_card: str | None = None,
    mode: str = "going_1st",
) -> tuple[float, dict[str, float]]:
    if mode not in {"going_1st", "going_2nd"}:
        raise ValueError("mode must be 'going_1st' or 'going_2nd'.")
    if weights is None and mode == "going_2nd":
        weights = {
            "negates": 2.0,
            "interruptions": 3.5,
            "board_breaks": 4.5,
            "card_advantage": 1.3,
            "total_atk": 0.0015,
            "resources_used": 0.3,
            "attribute_coverage": 0.25,
            "target_reached": 8.0,
            "target_potential": 4.0,
            "otk_bonus": 0.0,
        }
    weights = weights or {
        "negates": 4.0,
        "interruptions": 5.0,
        "board_breaks": 0.0,
        "card_advantage": 1.5,
        "total_atk": 0.001,
        "resources_used": 0.5,
        "attribute_coverage": 0.6,
        "target_reached": 4.0,
        "target_potential": 2.0,
        "otk_bonus": 0.0,
    }
    established_field = tuple(unit for unit in state.field if not unit.temporary)
    negates = float(sum(cards[unit.name].negates for unit in established_field))
    hand_attributes = Counter(cards[name].attribute for name in state.hand if cards[name].attribute)
    field_interruptions = 0.0
    for unit in established_field:
        spec = cards[unit.name]
        profile = disruption_profile(unit.name)
        if profile and profile.get("zone") == "field":
            activation_key = profile.get("activation_key")
            if activation_key and _used(state, unit.name, str(activation_key)):
                continue
            field_interruptions += float(max(spec.interruptions, int(profile.get("count", 0))))
        elif spec.interruptions and (
            not spec.interruption_requirements
            or any(
                all(hand_attributes[attribute] > 0 for attribute in requirement)
                for requirement in spec.interruption_requirements
            )
        ):
            field_interruptions += spec.interruptions
    handtrap_interruptions = float(sum(
        int(profile.get("count", 1))
        for name in state.hand
        if (profile := disruption_profile(name)) and profile.get("zone") == "hand"
    ))
    interruptions = field_interruptions + handtrap_interruptions
    attribute_coverage = float(sum(hand_attributes[attribute] > 0 for attribute in ("FIRE", "DARK", "EARTH")))
    card_advantage = float(len(state.hand) + len(established_field) - opponent_hand)
    total_atk = float(sum(cards[unit.name].atk for unit in established_field))
    otk_damage = total_atk
    is_otk = otk_damage >= 8000
    resources = float(state.resources_used)
    board_breaks = float(state.modeled_board_breaks)
    target_reached = float(
        target_card is not None and any(unit.name == target_card and not unit.temporary for unit in state.field)
    )
    target_potential = 0.0
    if target_card and target_card in state.extra_deck and target_card in cards:
        target_spec = cards[target_card]
        if any(_link_material_options(state, target_spec, cards)):
            target_potential = 1.0
        elif target_spec.xyz_material_level is not None and any(_xyz_material_options(state, target_spec, cards)):
            target_potential = 1.0
        elif target_spec.link_rating:
            eligible: list[FieldUnit] = []
            for unit in state.field:
                spec = cards[unit.name]
                if target_spec.link_requirement == "fire_monsters":
                    matches = spec.attribute == "FIRE"
                elif target_spec.link_requirement == "fire_included":
                    matches = spec.attribute == "FIRE"
                elif target_spec.link_requirement == "different_names":
                    matches = spec.effect_monster
                elif target_spec.link_requirement == "normal_summoned_low_atk":
                    matches = unit.summoned_normally and spec.atk <= 1000
                elif target_spec.link_requirement == "level_1":
                    matches = spec.level == 1
                else:
                    matches = spec.effect_monster
                if matches:
                    eligible.append(unit)
            points = sum(cards[unit.name].link_rating or 1 for unit in eligible)
            progress = min(0.99, points / max(1, target_spec.link_rating))
            if len(eligible) >= target_spec.link_min_materials:
                target_potential = max(target_potential, progress)
    components = {
        "negates": negates,
        "interruptions": interruptions,
        "field_disruptions": field_interruptions,
        "handtrap_disruptions": handtrap_interruptions,
        "board_breaks": board_breaks,
        "otk_damage": otk_damage,
        "otk_reached": float(is_otk),
        "disruptions_used": float(state.modeled_disruptions),
        "card_advantage": card_advantage,
        "total_atk": total_atk,
        "resources_used": resources,
        "attribute_coverage": attribute_coverage,
        "target_reached": target_reached,
        "target_potential": target_potential,
    }
    score = (
        weights["negates"] * negates
        + weights["interruptions"] * interruptions
        + weights.get("board_breaks", 0.0) * board_breaks
        + weights["card_advantage"] * card_advantage
        + weights["total_atk"] * total_atk
        - weights["resources_used"] * resources
        + weights.get("attribute_coverage", 0.6) * attribute_coverage
        + weights.get("target_reached", 8.0) * target_reached
        + weights.get("target_potential", 3.0) * target_potential
        + weights.get("otk_bonus", 0.0) * float(is_otk)
        + (20.0 if mode == "going_2nd" and is_otk else 0.0)
    )
    return score, components


def _rollout_action(
    actions: list[Action],
    rng: random.Random,
    *,
    target_card: str | None = None,
    cards: Mapping[str, CardSpec] | None = None,
) -> Action:
    priorities = {
        "wanted_search": 2.0,
        "diabellstar_self_summon": 2.5,
        "diabellstar_set": 2.0,
        "oss_activate": 5.0,
        "snake_eye_search": 4.0,
        "snake_eye_send_two_ss": 2.0,
        "poplar_free_summon": 4.0,
        "poplar_search": 4.0,
        "oak_revive": 2.0,
        "link_summon": 0.35 if target_card else 1.2,
        "kaiju_tribute": 5.5,
        "typhon_summon": 5.0,
        "k9_jacks_destroy": 3.5,
        "k9_jokul_pair": 7.0,
        "k9_lantern_revive": 5.0,
        "k9_noroi_summon": 6.0,
        "k9_izuna_send": 3.0,
        "k9_jokul_search": 3.0,
        "k9_lantern_search": 3.0,
        "k9_ripper_search": 3.0,
        "a_case_activate": 3.0,
        "vs_stake": 6.0,
        "vs_search_monster": 4.0,
        "vs_search_spelltrap": 3.0,
        "vs_hollie_sue_summon": 4.0,
        "vs_hollie_sue_deck_summon": 5.0,
        "vs_tag_out": 2.5,
        "vs_jiaolong_summon": 4.0,
        "vs_two_fire_search": 3.5,
        "vs_start_search": 3.0,
        "xyz_summon": 1.2,
        "nash_rank_up": 2.0,
        "k9_forced_release": 2.0,
        "k9_xyz_summon": 5.0,
    }
    weights: list[float] = []
    for action in actions:
        weight = priorities.get(action.kind, 1.0)
        if action.kind == "normal_summon":
            weight *= 4.0 if action.card == "Snake-Eye Ash" else 3.5 if action.card in {
                "Vanquish Soul Razen", "Vanquish Soul Dr. Mad Love", "K9-04 Noroi", "K9-17 Izuna"
            } else 0.5
        # Prefer establishing the K9 negate before the fifth Special Summon.
        if action.kind in {"xyz_summon", "nash_rank_up"} and action.card == 'K9-17 "Ripper"':
            weight = max(weight, 24.0)
        if action.kind in {"xyz_summon", "nash_rank_up"} and action.card == "Divine Arsenal AA-ZEUS - Sky Thunder":
            weight = max(weight, 12.0)
        if cards and action.kind == "snake_eye_search" and "poplar_ss_if_added" in cards[action.target].operators:
            weight *= 4.0
        if cards and action.kind == "poplar_search" and "oss_activate" in cards[action.target].operators:
            weight *= 4.0
        if cards and action.kind == "diabellstar_set":
            weight *= 2.0 if "oss_activate" in cards[action.target].operators else 0.75
        if target_card and action.kind == "link_summon" and action.card == target_card:
            weight = 100.0
        if target_card and action.kind in {"xyz_summon", "nash_rank_up", "k9_forced_release"} and action.card == target_card:
            weight = 100.0
        elif target_card and action.kind == "link_summon" and cards:
            target_spec = cards[target_card]
            candidate_spec = cards[action.card]
            if (
                candidate_spec.link_rating
                and target_spec.link_rating
                and candidate_spec.link_rating < target_spec.link_rating
                and candidate_spec.attribute == target_spec.attribute
            ):
                weight = max(weight, 3.0)
        if cards and target_card and cards[target_card].link_requirement in {"fire_monsters", "fire_included"}:
            if action.kind == "diabellstar_self_summon":
                weight *= 4.0 if action.source == "hand" else 0.25
            elif action.kind in {"oss_activate", "snake_eye_send_two_ss"}:
                pays_fire = any(cards[cost].attribute == "FIRE" for cost in action.costs)
                weight *= 0.25 if pays_fire else 2.0
        weights.append(weight)
    return rng.choices(actions, weights=weights, k=1)[0]


def run_mcts(
    deck: DeckModel,
    starting_hand: Iterable[str],
    *,
    iterations: int = 600,
    rollout_depth: int = 20,
    exploration: float = math.sqrt(2.0),
    seed: int = 7,
    opponent_hand: int = 5,
    opponent_hand_effect_activated: bool = False,
    target_card: str | None = None,
    mode: str = "going_1st",
    initial_state: SimState | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> MCTSResult:
    if iterations < 1 or rollout_depth < 1:
        raise ValueError("iterations and rollout_depth must be positive.")
    if mode not in {"going_1st", "going_2nd"}:
        raise ValueError("mode must be 'going_1st' or 'going_2nd'.")
    root_state = initial_state or make_initial_state(
        deck,
        starting_hand,
        opponent_hand_count=opponent_hand,
        opponent_hand_effect_activated=opponent_hand_effect_activated,
        opponent_field_count=3 if mode == "going_2nd" else 0,
    )
    unknown = sorted(
        {name for name in (*root_state.hand, *root_state.deck, *root_state.extra_deck) if name not in deck.cards}
    )
    if unknown:
        raise ValueError(f"Search state contains cards missing from the deck model: {', '.join(unknown)}")
    rng = random.Random(seed)
    root = SearchNode(root_state)
    best_state = root_state
    best_path: tuple[Action, ...] = ()
    best_score, best_components = evaluate_board(
        root_state, deck.cards, opponent_hand=opponent_hand, target_card=target_card, mode=mode
    )
    expanded_nodes = 1
    completed_iterations = 0

    for _ in range(iterations):
        if should_stop is not None and should_stop():
            break
        completed_iterations += 1
        node = root
        path: tuple[Action, ...] = ()
        depth = 0

        while True:
            if node.untried_actions is None:
                node.untried_actions = legal_actions(node.state, deck.cards)
            if not node.untried_actions or depth >= rollout_depth:
                if not node.children or depth >= rollout_depth:
                    break
                parent_visits = max(1, node.visits)
                node = max(
                    node.children,
                    key=lambda child: child.mean_value
                    + exploration * math.sqrt(math.log(parent_visits) / max(1, child.visits)),
                )
                if node.action:
                    path += (node.action,)
                depth += 1
                continue

            action = rng.choice(node.untried_actions)
            node.untried_actions.remove(action)
            next_state = apply_action(node.state, action, deck.cards)
            path += (action,)
            depth += 1
            node = SearchNode(next_state, parent=node, action=action, path=path)
            node.parent.children.append(node)
            expanded_nodes += 1
            break

        simulation_state = node.state
        simulation_path = path
        rollout_reward, rollout_components = evaluate_board(
            simulation_state, deck.cards, opponent_hand=opponent_hand, target_card=target_card, mode=mode
        )
        if rollout_reward > best_score:
            best_state, best_path = simulation_state, simulation_path
            best_score, best_components = rollout_reward, rollout_components

        while depth < rollout_depth:
            actions = legal_actions(simulation_state, deck.cards)
            if not actions:
                break
            action = _rollout_action(
                actions, rng, target_card=target_card, cards=deck.cards
            )

            simulation_state = apply_action(simulation_state, action, deck.cards)
            simulation_path += (action,)
            depth += 1
            rollout_reward, components = evaluate_board(
                simulation_state, deck.cards, opponent_hand=opponent_hand, target_card=target_card, mode=mode
            )
            if rollout_reward > best_score:
                best_state, best_path = simulation_state, simulation_path
                best_score, best_components = rollout_reward, components

        node.value_sum += rollout_reward
        node.visits += 1
        parent = node.parent
        while parent is not None:
            parent.value_sum += rollout_reward
            parent.visits += 1
            parent = parent.parent

    return MCTSResult(
        actions=list(best_path),
        final_state=best_state,
        score=best_score,
        score_components=best_components,
        simulations=completed_iterations,
        expanded_nodes=expanded_nodes,
        target_card=target_card,
    )

