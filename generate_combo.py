"""Run MCTS over a deck model and export a human-readable combo report."""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcts_search import DeckModel, MCTSResult, load_deck, run_mcts


def parse_hand(deck: DeckModel, text: str) -> list[str]:
    names_by_folded = {name.casefold(): name for name in deck.main_deck}
    # MDM uses slashed-O while supplied deck lists often use two zeroes.
    unicode_name = "k9-\u00f8\u00f8 lupis"
    typed_alias = "k9-00 lupis"
    if unicode_name in names_by_folded:
        names_by_folded.setdefault(typed_alias, names_by_folded[unicode_name])
    elif typed_alias in names_by_folded:
        names_by_folded.setdefault(unicode_name, names_by_folded[typed_alias])
    value = text.strip()
    if "|" in value or "\n" in value or "\r" in value:
        tokens = [token.strip() for token in value.replace("\r", "|").replace("\n", "|").split("|") if token.strip()]
    elif value.casefold() in names_by_folded:
        tokens = [value]
    else:
        # Greedily join comma-separated pieces so names such as
        # "Nibiru, the Primal Being" remain a single card.
        pieces = [piece.strip() for piece in value.split(",")]
        tokens = []
        cursor = 0
        while cursor < len(pieces):
            found: tuple[int, str] | None = None
            for end in range(len(pieces), cursor, -1):
                candidate = ", ".join(pieces[cursor:end]).casefold()
                compact_candidate = ",".join(pieces[cursor:end]).casefold()
                name = names_by_folded.get(candidate) or names_by_folded.get(compact_candidate)
                if name:
                    found = (end, name)
                    break
            if found is None:
                raise ValueError(f"Could not match a card name in {value!r}.")
            cursor, name = found
            tokens.append(name)
    result: list[str] = []
    available = list(deck.main_deck)
    for token in tokens:
        name = names_by_folded.get(token.casefold())
        if not name:
            raise ValueError(f"{token!r} is not in this deck's legal main-deck pool.")
        available.remove(name)
        result.append(name)
    return result


def random_hand(deck: DeckModel, size: int, seed: int) -> list[str]:
    if size < 1 or size > len(deck.main_deck):
        raise ValueError(f"Hand size must be from 1 to {len(deck.main_deck)}.")
    return random.Random(seed).sample(list(deck.main_deck), size)


def render_report(
    deck: DeckModel,
    hand: list[str],
    result: MCTSResult,
    *,
    seed: int,
    iterations: int,
    mode: str = "going_1st",
) -> str:
    lines = [
        "# MCTS Combo Search",
        "",
        f"- Deck: {deck.name}",
        f"- Main Deck after filters: {len(deck.main_deck)} cards",
        f"- Extra Deck after filters: {len(deck.extra_deck)} cards",
        f"- Starting hand: {', '.join(hand)}",
        f"- Target: {result.target_card or 'highest modeled board value'}",
        f"- Mode: {mode}",
        f"- Random seed: {seed}",
        f"- MCTS simulations: {iterations}",
        f"- Expanded nodes: {result.expanded_nodes}",
        f"- Banlist snapshot: {'loaded' if deck.banlist_loaded else 'not loaded; deck legality is not verified'}",
        "",
        "> Prototype simulation. Validate any line with ocgcore before treating it as a legal duel sequence.",
        "> End Phase and opponent responses are not simulated; Stake your Soul! summons are marked temporary.",
        "",
        "## Generated sequence",
        "",
    ]
    if result.actions:
        lines.extend(
            f"**Bước {index}:** {action}"
            for index, action in enumerate(result.actions, start=1)
        )
    else:
        lines.append("MCTS found no modeled action for this hand.")
    lines.extend(["", "## End board", ""])
    if result.final_state.field:
        for unit in result.final_state.field:
            material_label = "materials" if unit.material_kind == "xyz" else "link materials"
            materials = f" — {material_label}: {', '.join(unit.materials)}" if unit.materials else ""
            temporary = " (temporary; returns to hand in the End Phase if still on field)" if unit.temporary else ""
            lines.append(f"- {unit.name}{temporary}{materials}")
    else:
        lines.append("- No monsters on the field.")
    if result.final_state.spell_trap_zone:
        lines.extend(["", "### Spell/Trap Zone", ""])
        for unit in result.final_state.spell_trap_zone:
            face = "face-up" if unit.face_up else "face-down"
            lines.append(f"- {unit.name} ({face})")
    if result.final_state.hand:
        lines.extend(["", f"Cards remaining in hand: {', '.join(result.final_state.hand)}"])
    lines.extend(
        [
            "",
            "## Board value",
            "",
            f"- Total score: {result.score:.3f}",
            f"- Negates: {result.score_components['negates']:.0f}",
            f"- Interruptions: {result.score_components['interruptions']:.0f}",
            f"- Card advantage: {result.score_components['card_advantage']:.0f}",
            f"- Total ATK: {result.score_components['total_atk']:.0f}",
            f"- Deck resources used: {result.score_components['resources_used']:.0f}",
            f"- FIRE/DARK/EARTH attributes retained in hand: {result.score_components.get('attribute_coverage', 0):.0f}/3",
        ]
    )
    if "target_reached" in result.score_components:
        lines.append(f"- Target reached: {'yes' if result.score_components['target_reached'] else 'no'}")
    if deck.warnings:
        lines.extend(["", "## Deck filters", ""])
        lines.extend(f"- {warning}" for warning in deck.warnings)
    lines.extend(["", f"Generated at {datetime.now(timezone.utc).isoformat(timespec='seconds')}.", ""])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deck", type=Path, default=Path("examples/vs_k9_deck.json"))
    parser.add_argument("--banlist", type=Path, default=Path("data/banlist.json"))
    parser.add_argument("--hand", help="five card names separated by |; omit for a random hand")
    parser.add_argument("--hand-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--mode", choices=("going_1st", "going_2nd"), default="going_1st")
    parser.add_argument("--opponent-hand-size", type=int, default=5)
    parser.add_argument(
        "--opponent-hand-effect-activated",
        action="store_true",
        help="Enable K9 Quick Effects that require an opponent hand/GY monster effect this turn.",
    )
    parser.add_argument("--target", help="Extra Deck target card name to prioritize")
    parser.add_argument("--iterations", type=int, default=15000)
    parser.add_argument("--rollout-depth", type=int, default=24)
    parser.add_argument("--output", type=Path, default=Path("output/vs_k9_combo_result.md"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    deck = load_deck(args.deck, args.banlist)
    hand = parse_hand(deck, args.hand) if args.hand else random_hand(deck, args.hand_size, args.seed)
    if args.target and args.target not in deck.extra_deck:
        raise ValueError(f"Target {args.target!r} is not in the legal Extra Deck after banlist filtering.")
    result = run_mcts(
        deck,
        hand,
        iterations=args.iterations,
        rollout_depth=args.rollout_depth,
        seed=args.seed,
        opponent_hand=args.opponent_hand_size,
        opponent_hand_effect_activated=args.opponent_hand_effect_activated,
        target_card=args.target,
        mode=args.mode,
    )
    report = render_report(deck, hand, result, seed=args.seed, iterations=args.iterations, mode=args.mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = args.output.with_suffix(args.output.suffix + ".tmp")
    temp_path.write_text(report, encoding="utf-8")
    temp_path.replace(args.output)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    print(f"Deck: {deck.name}")
    print(f"Deck pool after banlist filtering: {len(deck.main_deck)} Main / {len(deck.extra_deck)} Extra")
    print(f"Starting hand: {', '.join(hand)}")
    if args.opponent_hand_effect_activated:
        print("K9 hand/GY Quick Effects: enabled")
    if args.target:
        print(f"Target: {args.target} ({'reached' if result.score_components['target_reached'] else 'not reached'})")
    print("Combo:")
    if result.actions:
        for index, action in enumerate(result.actions, start=1):
            print(f"Bước {index}: {action}")
    else:
        print("No modeled action found.")
    print(f"End board: {', '.join(unit.name for unit in result.final_state.field) or '(empty)'}")
    print(f"Board value: {result.score:.3f} | simulations: {result.simulations}")
    print(f"Report: {args.output}")
    if not deck.banlist_loaded:
        print("Warning: no current banlist snapshot found; deck legality was not checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

