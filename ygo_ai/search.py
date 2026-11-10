"""Bounded symbolic combo search for a small declarative card model."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ComboCard:
    name: str
    location: str
    level: int | None = None
    rank: int | None = None
    race: str | None = None
    normal_summon: bool = False
    self_special_if_control_race: str | None = None
    xyz_material_count: int = 0
    negates: int = 0
    disruptions: int = 0
    atk: int = 0


@dataclass(frozen=True, slots=True)
class BoardMonster:
    name: str
    level: int | None
    rank: int | None
    race: str | None
    materials: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComboState:
    hand: tuple[str, ...]
    field: tuple[BoardMonster, ...]
    extra_deck: tuple[str, ...]
    graveyard: tuple[str, ...] = ()
    normal_summon_used: bool = False
    effects_used: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Action:
    kind: str
    card_name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}: {self.card_name}" + (f" ({self.detail})" if self.detail else "")


@dataclass(slots=True)
class ComboResult:
    actions: list[Action]
    final_state: ComboState | None
    states_expanded: int
    truncated: bool

    @property
    def found(self) -> bool:
        return self.final_state is not None


@dataclass(frozen=True, slots=True)
class ValueWeights:
    negates: float = 3.0
    disruptions: float = 2.0
    card_advantage: float = 1.5
    total_atk: float = 0.001
    resources_used: float = 0.5


def _key(state: ComboState) -> tuple[object, ...]:
    field = tuple(
        sorted(
            (monster.name, monster.level, monster.rank, monster.race, tuple(sorted(monster.materials)))
            for monster in state.field
        )
    )
    return (
        tuple(sorted(state.hand)),
        field,
        tuple(sorted(state.extra_deck)),
        tuple(sorted(state.graveyard)),
        state.normal_summon_used,
        tuple(sorted(state.effects_used)),
    )


def _successors(state: ComboState, cards: dict[str, ComboCard]) -> Iterable[tuple[Action, ComboState]]:
    field_races = {monster.race for monster in state.field}
    for name in state.hand:
        card = cards[name]
        if (
            card.location == "main"
            and card.normal_summon
            and not state.normal_summon_used
            and len(state.field) < 5
        ):
            remaining_hand = list(state.hand)
            remaining_hand.remove(name)
            summoned = BoardMonster(name, card.level, card.rank, card.race)
            yield (
                Action("Normal Summon", name),
                ComboState(
                    hand=tuple(remaining_hand),
                    field=state.field + (summoned,),
                    extra_deck=state.extra_deck,
                    graveyard=state.graveyard,
                    normal_summon_used=True,
                    effects_used=state.effects_used,
                ),
            )

        effect_id = f"{name}::self_special"
        if (
            card.location == "main"
            and card.self_special_if_control_race
            and card.self_special_if_control_race in field_races
            and effect_id not in state.effects_used
            and len(state.field) < 5
        ):
            remaining_hand = list(state.hand)
            remaining_hand.remove(name)
            summoned = BoardMonster(name, card.level, card.rank, card.race)
            yield (
                Action("Activate self-Special Summon", name),
                ComboState(
                    hand=tuple(remaining_hand),
                    field=state.field + (summoned,),
                    extra_deck=state.extra_deck,
                    graveyard=state.graveyard,
                    normal_summon_used=state.normal_summon_used,
                    effects_used=tuple(sorted(state.effects_used + (effect_id,))),
                ),
            )

    for extra_name in state.extra_deck:
        extra = cards[extra_name]
        if extra.location != "extra" or extra.rank is None or extra.xyz_material_count <= 0:
            continue
        eligible = [
            (index, monster)
            for index, monster in enumerate(state.field)
            if monster.level == extra.rank
        ]
        for chosen in combinations(eligible, extra.xyz_material_count):
            chosen_indexes = {index for index, _ in chosen}
            materials = tuple(monster.name for _, monster in chosen)
            remaining_field = tuple(
                monster for index, monster in enumerate(state.field) if index not in chosen_indexes
            )
            xyz_monster = BoardMonster(
                extra.name,
                level=None,
                rank=extra.rank,
                race=extra.race,
                materials=materials,
            )
            remaining_extra = list(state.extra_deck)
            remaining_extra.remove(extra_name)
            yield (
                Action("Xyz Summon", extra.name, f"overlay {', '.join(materials)}"),
                ComboState(
                    hand=state.hand,
                    field=remaining_field + (xyz_monster,),
                    extra_deck=tuple(remaining_extra),
                    # Xyz materials remain attached; they are not sent to the GY here.
                    graveyard=state.graveyard,
                    normal_summon_used=state.normal_summon_used,
                    effects_used=state.effects_used,
                ),
            )


def _target_reached(state: ComboState, target_name: str) -> bool:
    return any(monster.name == target_name for monster in state.field)


def evaluate_end_board(
    state: ComboState,
    cards: dict[str, ComboCard],
    *,
    opponent_cards: int = 0,
    starting_hand_size: int = 0,
    starting_extra_deck_size: int = 0,
    weights: ValueWeights = ValueWeights(),
) -> float:
    """Estimate an end board using the negates/disruption/advantage/resources formula."""
    negates = sum(cards[monster.name].negates for monster in state.field)
    disruptions = sum(cards[monster.name].disruptions for monster in state.field)
    card_advantage = len(state.hand) + len(state.field) - opponent_cards
    total_atk = sum(cards[monster.name].atk for monster in state.field)
    resources_used = max(0, starting_hand_size - len(state.hand))
    resources_used += max(0, starting_extra_deck_size - len(state.extra_deck))
    return (
        weights.negates * negates
        + weights.disruptions * disruptions
        + weights.card_advantage * card_advantage
        + weights.total_atk * total_atk
        - weights.resources_used * resources_used
    )


def find_longest_combo(
    cards: dict[str, ComboCard],
    starting_hand: tuple[str, ...],
    extra_deck: tuple[str, ...],
    target_name: str,
    *,
    max_depth: int = 12,
    max_nodes: int = 100_000,
) -> ComboResult:
    """BFS all bounded legal lines, returning the longest line that reaches target.

    State repetition is pruned on the current branch, which prevents cycles without
    discarding a longer route to the same state reached through a different sequence.
    """
    for name in (*starting_hand, *extra_deck, target_name):
        if name not in cards:
            raise KeyError(f"No declarative card definition for {name!r}.")
    initial = ComboState(hand=starting_hand, field=(), extra_deck=extra_deck)
    queue = deque([(initial, tuple(), frozenset({_key(initial)}))])
    best_actions: tuple[Action, ...] = ()
    best_state: ComboState | None = None
    best_score = float("-inf")
    expanded = 0
    truncated = False

    while queue:
        if expanded >= max_nodes:
            truncated = True
            break
        state, path, path_keys = queue.popleft()
        expanded += 1
        if _target_reached(state, target_name):
            score = len(path) * 1_000_000 + evaluate_end_board(
                state,
                cards,
                starting_hand_size=len(starting_hand),
                starting_extra_deck_size=len(extra_deck),
            )
            if score > best_score:
                best_score = score
                best_actions = path
                best_state = state
            continue
        if len(path) >= max_depth:
            continue
        for action, next_state in _successors(state, cards):
            key = _key(next_state)
            if key in path_keys:
                continue
            queue.append((next_state, path + (action,), path_keys | {key}))

    return ComboResult(
        actions=list(best_actions),
        final_state=best_state,
        states_expanded=expanded,
        truncated=truncated,
    )


def build_training_example() -> tuple[dict[str, ComboCard], tuple[str, ...], tuple[str, ...], str]:
    """Return a tiny synthetic deck used to demonstrate general action search."""
    adept = "Training Adept"
    squire = "Training Squire"
    target = "Twin-Star Xyz Dragon"
    cards = {
        adept: ComboCard(
            name=adept,
            location="main",
            level=4,
            race="Warrior",
            normal_summon=True,
            atk=1800,
        ),
        squire: ComboCard(
            name=squire,
            location="main",
            level=4,
            race="Warrior",
            normal_summon=True,
            self_special_if_control_race="Warrior",
            atk=1200,
        ),
        target: ComboCard(
            name=target,
            location="extra",
            rank=4,
            race="Dragon",
            xyz_material_count=2,
            atk=2500,
        ),
    }
    return cards, (adept, squire), (target,), target

