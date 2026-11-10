"""Run a bounded BFS combo search on the synthetic two-card training hand."""

from __future__ import annotations

from .search import build_training_example, find_longest_combo


def main() -> int:
    cards, starting_hand, extra_deck, target = build_training_example()
    result = find_longest_combo(
        cards,
        starting_hand,
        extra_deck,
        target,
        max_depth=8,
    )
    print(f"Starting hand: {', '.join(starting_hand)}")
    print(f"Goal: summon {target} from the Extra Deck")
    if not result.found:
        print("No line found within the configured search limits.")
        return 1
    print("Generated line:")
    for step, action in enumerate(result.actions, start=1):
        print(f"  {step}. {action}")
    final = result.final_state
    assert final is not None
    print("Final field:")
    for monster in final.field:
        print(f"  - {monster.name} (materials: {', '.join(monster.materials) or 'none'})")
    print(f"States expanded: {result.states_expanded}; truncated: {result.truncated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

