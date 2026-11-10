"""Card-specific disruption and board-break classifications used by MCTS and the UI."""

from __future__ import annotations

from typing import Any


# Keys are case-folded card names. A profile describes one modeled interaction;
# the simulator still checks its own field/hand and effect-use state.
DISRUPTION_CARDS: dict[str, dict[str, Any]] = {
    "ash blossom & joyous spring": {
        "zone": "hand", "type": "Negate search/summon/send", "count": 1,
        "effect": "Negate a Deck search, Special Summon, or send from Deck.",
    },
    "ghost belle & haunted mansion": {
        "zone": "hand", "type": "GY protection/negate", "count": 1,
        "effect": "Negate an effect that moves cards out of the GY or banishment.",
    },
    'maxx "c"': {
        "zone": "hand", "type": "Draw pressure", "count": 1,
        "effect": "Draw for each opposing Special Summon this turn.",
    },
    "mulcharmy fuwalos": {
        "zone": "hand", "type": "Draw pressure", "count": 1,
        "effect": "Draw as the opponent Special Summons from Deck/Extra Deck.",
    },
    "effect veiler": {
        "zone": "hand", "type": "Monster-effect negate", "count": 1,
        "effect": "Negate a face-up opponent monster's effects for the turn.",
    },
    "infinite impermanence": {
        "zone": "hand", "type": "Monster-effect negate", "count": 1,
        "effect": "Negate a face-up opponent monster's effects for the turn.",
    },
    "nibiru, the primal being": {
        "zone": "hand", "type": "Board wipe", "count": 1,
        "effect": "Tribute face-up monsters after the opponent's fifth summon.",
    },
    "bystial magnamhut": {
        "zone": "hand", "type": "GY banish / summon", "count": 1,
        "effect": "Quick-effect banish from a GY, then summon this Bystial.",
    },
    'k9-17 "ripper"': {
        "zone": "field", "type": "Monster-effect negate", "count": 1,
        "effect": "Detach a material to negate a monster effect activated in hand or GY.",
    },
    'k9-66x "jacks"': {
        "zone": "field", "type": "Destroy", "count": 1,
        "activation_key": "destroy",
        "effect": "Destroy an opponent's monster when its modeled trigger is available.",
    },
    'k9-x "werewolf"': {
        "zone": "field", "type": "Banish", "count": 1,
        "effect": "Quick-effect banish an opponent's card from field/GY or hand.",
    },
    "rock of the vanquisher": {
        "zone": "field", "type": "Quick summon / recovery", "count": 1,
        "effect": "Quick-effect summon or recover a Vanquish Soul monster.",
    },
    "s:p little knight": {
        "zone": "field", "type": "Temporary banish", "count": 1,
        "activation_key": "banish_both",
        "effect": "Quick-effect banish two face-up monsters until the End Phase.",
    },
    "super starslayer ty-phon - sky crisis": {
        "zone": "field", "type": "Bounce / summon suppression", "count": 1,
        "effect": "Return a high-ATK monster on summon and suppress high-ATK monster effects.",
        "board_break": True,
    },
    "apollousa, bow of the goddess": {
        "zone": "field", "type": "Monster-effect negate", "count": 1,
        "effect": "Negate an opponent monster effect by reducing this card's ATK.",
    },
    "divine arsenal aa-zeus - sky thunder": {
        "zone": "field", "type": "Board wipe", "count": 1,
        "effect": "Detach two materials to send other cards on the field to the GY.",
    },
    "dogoran, the mad flame kaiju": {
        "zone": "board_breaker", "type": "Kaiju tribute", "count": 1,
        "effect": "Tribute an opponent monster to summon Dogoran to their field.",
        "action": "kaiju_tribute",
    },
}


def disruption_profile(card_name: str) -> dict[str, Any] | None:
    """Return the shared disruption profile for a card name, if classified."""
    return DISRUPTION_CARDS.get(str(card_name).strip().casefold())
