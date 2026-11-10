"""Minimal duel-state data structures; authoritative rules belong to ocgcore."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Sequence


class Phase(StrEnum):
    DRAW = "Draw"
    STANDBY = "Standby"
    MAIN1 = "Main1"
    BATTLE = "Battle"
    MAIN2 = "Main2"
    END = "End"


class Zone(StrEnum):
    HAND = "Hand"
    DECK = "Deck"
    EXTRA_DECK = "Extra Deck"
    MAIN_MONSTER = "Main Monster Zone"
    EXTRA_MONSTER = "Extra Monster Zone"
    SPELL_TRAP = "Spell/Trap Zone"
    FIELD = "Field Spell Zone"
    GRAVEYARD = "Graveyard"
    BANISHED = "Banished"
    MATERIAL = "Overlay Material"


class Position(StrEnum):
    FACE_UP_ATTACK = "Face-up ATK"
    FACE_UP_DEFENSE = "Face-up DEF"
    FACE_DOWN_ATTACK = "Face-down ATK"
    FACE_DOWN_DEFENSE = "Face-down DEF"


class SpellSpeed(IntEnum):
    """Yu-Gi-Oh! chain speed: ignition/normal effects, quick effects, counter traps."""

    SPEED_1 = 1
    SPEED_2 = 2
    SPEED_3 = 3


@dataclass(slots=True)
class CardInstance:
    card_id: str
    name: str
    owner: int
    controller: int
    zone: Zone
    position: Position | None = None
    counters: dict[str, int] = field(default_factory=dict)
    negated: bool = False
    effects_used_this_turn: set[str] = field(default_factory=set)
    instance_id: str = ""
    overlay_materials: list[CardInstance] = field(default_factory=list)


@dataclass(slots=True)
class PlayerState:
    hand: list[CardInstance] = field(default_factory=list)
    main_monster_zone: list[CardInstance | None] = field(default_factory=lambda: [None] * 5)
    extra_monster_zone: list[CardInstance | None] = field(default_factory=lambda: [None] * 2)
    spell_trap_zone: list[CardInstance | None] = field(default_factory=lambda: [None] * 5)
    field_spell: CardInstance | None = None
    graveyard: list[CardInstance] = field(default_factory=list)
    banished: list[CardInstance] = field(default_factory=list)
    deck: list[CardInstance] = field(default_factory=list)
    extra_deck: list[CardInstance] = field(default_factory=list)
    normal_summon_used: bool = False

    @property
    def monsters_on_field(self) -> list[CardInstance]:
        return [card for card in self.main_monster_zone + self.extra_monster_zone if card is not None]


@dataclass(slots=True)
class ChainLink:
    link_number: int
    controller: int
    card_id: str
    effect_id: str
    activation_description: str = ""
    targets: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    spell_speed: int = SpellSpeed.SPEED_1
    simultaneous_trigger: bool = False
    negated: bool = False


@dataclass(frozen=True, slots=True)
class TriggerCandidate:
    """A trigger effect waiting to be ordered in a SEGOC event window."""

    controller: int
    card_id: str
    effect_id: str
    activation_description: str = ""
    targets: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    spell_speed: int = SpellSpeed.SPEED_1

    @property
    def key(self) -> str:
        return f"{self.controller}:{self.card_id}:{self.effect_id}"


def order_segoc_triggers(
    candidates: Sequence[TriggerCandidate],
    *,
    turn_player: int,
    preferred_order: Sequence[str] = (),
) -> list[TriggerCandidate]:
    """Apply SEGOC grouping, preserving timestamp order unless a player orders a group.

    The four mandatory/optional groups are ordered: turn player mandatory, non-turn
    player mandatory, turn player optional, non-turn player optional. ``preferred_order``
    may reorder candidates only within their own group, as the rules allow.
    """
    if turn_player not in (0, 1):
        raise ValueError("turn_player must be 0 or 1")
    buckets: list[list[TriggerCandidate]] = [[], [], [], []]
    for candidate in candidates:
        if candidate.controller not in (0, 1):
            raise ValueError("Trigger controller must be player 0 or 1")
        if candidate.spell_speed not in (1, 2, 3):
            raise ValueError("Spell speed must be 1, 2, or 3")
        is_turn_player = candidate.controller == turn_player
        group = (0 if is_turn_player else 1) if not candidate.optional else (2 if is_turn_player else 3)
        buckets[group].append(candidate)

    preference = {key: index for index, key in enumerate(preferred_order)}
    result: list[TriggerCandidate] = []
    for bucket in buckets:
        if preference:
            bucket.sort(key=lambda item: preference.get(item.key, len(preference)))
        result.extend(bucket)
    return result


@dataclass(slots=True)
class GameState:
    players: dict[int, PlayerState] = field(
        default_factory=lambda: {0: PlayerState(), 1: PlayerState()}
    )
    turn_player: int = 0
    priority_player: int | None = 0
    phase: Phase = Phase.DRAW
    current_turn_number: int = 1
    chain_stack: list[ChainLink] = field(default_factory=list)
    consecutive_priority_passes: int = 0

    def add_chain_link(self, link: ChainLink) -> None:
        """Append the newest activation; it will resolve before earlier links."""
        if link.controller not in (0, 1):
            raise ValueError("Chain-link controller must be player 0 or 1")
        if link.spell_speed not in (1, 2, 3):
            raise ValueError("Spell speed must be 1, 2, or 3")
        if self.chain_stack and not link.simultaneous_trigger:
            previous_speed = self.chain_stack[-1].spell_speed
            if link.spell_speed == SpellSpeed.SPEED_1 or link.spell_speed < previous_speed:
                raise ValueError(
                    f"Spell Speed {link.spell_speed} cannot chain to Spell Speed {previous_speed}."
                )
        expected_number = len(self.chain_stack) + 1
        if link.link_number != expected_number:
            raise ValueError(f"Chain link must be numbered {expected_number}.")
        self.chain_stack.append(link)
        self.priority_player = 1 - link.controller
        self.consecutive_priority_passes = 0

    def queue_segoc(
        self,
        candidates: Sequence[TriggerCandidate],
        *,
        preferred_order: Sequence[str] = (),
    ) -> list[ChainLink]:
        """Place all simultaneous triggers on the current chain using SEGOC order."""
        if self.priority_player is None:
            raise RuntimeError("Cannot assemble SEGOC while a chain is resolving.")
        ordered = order_segoc_triggers(
            candidates, turn_player=self.turn_player, preferred_order=preferred_order
        )
        links: list[ChainLink] = []
        for candidate in ordered:
            link = ChainLink(
                link_number=len(self.chain_stack) + 1,
                controller=candidate.controller,
                card_id=candidate.card_id,
                effect_id=candidate.effect_id,
                activation_description=candidate.activation_description,
                targets=candidate.targets,
                payload=dict(candidate.payload),
                spell_speed=candidate.spell_speed,
                simultaneous_trigger=True,
            )
            self.add_chain_link(link)
            links.append(link)
        return links

    def pass_priority(self) -> bool:
        """Pass priority; return True when both players passed over an open chain."""
        if self.priority_player is None:
            raise RuntimeError("Priority is unavailable while a chain is resolving.")
        self.consecutive_priority_passes += 1
        if self.consecutive_priority_passes >= 2:
            self.consecutive_priority_passes = 0
            if self.chain_stack:
                self.priority_player = None
                return True
            self.priority_player = self.turn_player
            return False
        self.priority_player = 1 - self.priority_player
        return False

    def resolve_next_chain_link(self) -> ChainLink | None:
        """Pop the newest link, matching Yu-Gi-Oh!'s reverse chain resolution order."""
        if not self.chain_stack:
            return None
        self.consecutive_priority_passes = 0
        self.priority_player = None
        link = self.chain_stack.pop()
        if not self.chain_stack:
            self.priority_player = self.turn_player
        return link

    def advance_phase(self) -> Phase:
        """Advance through the turn phases, handing off after the End Phase."""
        phases = [Phase.DRAW, Phase.STANDBY, Phase.MAIN1, Phase.BATTLE, Phase.MAIN2, Phase.END]
        current_index = phases.index(self.phase)
        if current_index == len(phases) - 1:
            self.advance_turn()
        else:
            self.phase = phases[current_index + 1]
        return self.phase

    def advance_turn(self) -> None:
        """Start the opponent's turn and clear per-turn use flags."""
        self.turn_player = 1 - self.turn_player
        self.current_turn_number += 1
        self.phase = Phase.DRAW
        self.priority_player = self.turn_player
        self.consecutive_priority_passes = 0
        self.players[self.turn_player].normal_summon_used = False
        for player in (0, 1):
            for card in self.iter_all_cards(player):
                card.effects_used_this_turn.clear()

    def reset_turn_usage(self, player: int) -> None:
        self.players[player].normal_summon_used = False
        for card in self.iter_all_cards(player):
            card.effects_used_this_turn.clear()

    def iter_all_cards(self, player: int):
        state = self.players[player]
        yield from state.hand
        yield from (card for card in state.main_monster_zone if card is not None)
        yield from (card for card in state.extra_monster_zone if card is not None)
        yield from (card for card in state.spell_trap_zone if card is not None)
        if state.field_spell is not None:
            yield state.field_spell
        yield from state.graveyard
        yield from state.banished
        yield from state.deck
        yield from state.extra_deck

    def attach_overlay_material(self, host: CardInstance, material: CardInstance) -> None:
        """Move a card instance under a host as an Xyz-style overlay material."""
        if host.zone not in {Zone.MAIN_MONSTER, Zone.EXTRA_MONSTER}:
            raise ValueError("Overlay host must be a monster on the field.")
        for player in self.players.values():
            if material in player.hand:
                player.hand.remove(material)
            if material in player.graveyard:
                player.graveyard.remove(material)
            if material in player.banished:
                player.banished.remove(material)
            if material in player.deck:
                player.deck.remove(material)
            if material in player.extra_deck:
                player.extra_deck.remove(material)
            for zone in (player.main_monster_zone, player.extra_monster_zone, player.spell_trap_zone):
                for index, card in enumerate(zone):
                    if card is material:
                        zone[index] = None
        material.zone = Zone.MATERIAL
        material.position = None
        material.controller = host.controller
        host.overlay_materials.append(material)

    def detach_overlay_material(self, host: CardInstance, index: int = 0) -> CardInstance:
        """Detach an overlay unit to its owner's Graveyard and return it."""
        material = host.overlay_materials.pop(index)
        material.zone = Zone.GRAVEYARD
        material.position = None
        self.players[material.owner].graveyard.append(material)
        return material

