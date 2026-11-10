from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mdm_banlist import fetch_banlist, normalize_status
from mdm_scraper import (
    export_json,
    fetch_all_cards,
    build_parser as build_scraper_parser,
    open_database,
    sync_cards,
)
from mcts_search import (
    CardSpec,
    DeckModel,
    apply_action,
    legal_actions,
    load_deck,
    make_initial_state,
    evaluate_board,
    run_mcts,
)
from generate_combo import parse_hand
from ygo_ai.psct import parse_psct
from ygo_ai.state import (
    CardInstance,
    ChainLink,
    GameState,
    Phase,
    Position,
    TriggerCandidate,
    Zone,
    order_segoc_triggers,
)


class ScraperTests(unittest.TestCase):
    def test_pagination_deduplicates_overlapping_ids(self) -> None:
        pages = [
            {"cards": [{"gameId": "1", "name": "A"}, {"gameId": "2", "name": "B"}]},
            {"cards": [{"gameId": "2", "name": "B"}, {"gameId": "3", "name": "C"}]},
            {"cards": []},
        ]
        with patch("mdm_scraper.request_json", side_effect=pages) as request:
            cards = fetch_all_cards("https://example.test/cards", limit_per_page=2, interval=0)
        self.assertEqual([card["gameId"] for card in cards], ["1", "2", "3"])
        self.assertEqual([call.args[1]["page"] for call in request.call_args_list], [1, 2, 3])
        self.assertEqual([call.args[1]["offset"] for call in request.call_args_list], [0, 2, 4])

    def test_sqlite_diff_sync_force_and_json_export(self) -> None:
        record = {"gameId": "42", "name": "Test Magician", "type": "Monster", "level": 4}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            db = open_database(Path(temp) / "cards.db")
            self.assertEqual(sync_cards(db, [record], {}), (1, 0, 0))
            self.assertEqual(sync_cards(db, [record], {}), (0, 0, 1))
            self.assertEqual(sync_cards(db, [record], {}, force=True), (0, 1, 0))
            output = Path(temp) / "cards.json"
            self.assertEqual(export_json(db, output), 1)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported[0]["card_id"], "42")
            db.close()

    def test_banlist_status_aliases(self) -> None:
        self.assertEqual(normalize_status("Limited 1"), "Limited")
        self.assertEqual(normalize_status("2"), "Semi-Limited")
        self.assertEqual(normalize_status("Forbidden"), "Forbidden")

    def test_mdm_banlist_nested_card_and_to_status(self) -> None:
        row = {
            "_id": "change-1",
            "date": "2026-01-02T00:00:00.000Z",
            "card": {"_id": "card-1", "name": "Example Card"},
            "to": "Limited 1",
        }
        with patch("mdm_banlist.request_json", return_value=[row]):
            entries = fetch_banlist("https://example.test/banlist", limit_per_page=10, interval=0)
        self.assertEqual(entries[0]["card_id"], "card-1")
        self.assertEqual(entries[0]["ban_status"], "Limited")

    def test_banlist_repeated_page_stops_without_failing(self) -> None:
        row = {
            "_id": "change-1",
            "date": "2026-01-02T00:00:00.000Z",
            "card": {"_id": "card-1", "name": "Example Card"},
            "to": "Limited 1",
        }
        with patch("mdm_banlist.request_json", return_value=[row]) as request:
            entries = fetch_banlist(
                "https://example.test/banlist", limit_per_page=1, max_pages=8, interval=0
            )
        self.assertEqual(len(entries), 1)
        self.assertEqual(request.call_count, 3)

    def test_scraper_cli_accepts_limit_and_force(self) -> None:
        args = build_scraper_parser().parse_args(["sync", "--limit", "5", "--force"])
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.force)


class PSCTTests(unittest.TestCase):
    def test_hopt_and_search_resolution(self) -> None:
        text = (
            "If this card is Normal or Special Summoned: add 1 Level 1 FIRE monster "
            "from your Deck to your hand.\nYou can only use this effect of this card once per turn."
        )
        effect = parse_psct("Snake-Eye Ash", text)[0]
        self.assertEqual(effect.condition, "If this card is Normal or Special Summoned")
        self.assertEqual(effect.usage_limit, "HOPT")
        self.assertTrue(any(action.operator == "SearchToHand" for action in effect.resolve_actions))

    def test_cost_target_quick_speed_and_continuous_effect(self) -> None:
        quick = parse_psct(
            "Effect Veiler",
            "During your opponent's Main Phase (Quick Effect): You can discard this card; "
            "target 1 face-up monster your opponent controls; negate its effects until the end of this turn.",
        )[0]
        self.assertTrue(quick.quick_effect)
        self.assertEqual(quick.spell_speed, 2)
        self.assertEqual(quick.costs, "You can discard this card")
        self.assertTrue(quick.targets.startswith("target 1 face-up monster"))
        continuous = parse_psct(
            "Mystic Field", "While this card is face-up on the field, all monsters gain 500 ATK.",
            card_kind="Continuous Spell",
        )[0]
        self.assertTrue(continuous.continuous_effect)
        self.assertEqual(continuous.effect_kind, "continuous")
        counter = parse_psct("Counter Trap", "When a card is activated: negate that activation.",
                             card_kind="Counter Trap")[0]
        self.assertEqual(counter.spell_speed, 3)

    def test_soft_opt_is_distinct_from_hopt(self) -> None:
        effect = parse_psct("Example", "Once per turn: You can draw 1 card.")[0]
        self.assertEqual(effect.usage_limit, "OPT")


class StateTests(unittest.TestCase):
    def test_chain_resolves_lifo_and_priority_closes_chain(self) -> None:
        state = GameState(phase=Phase.MAIN1)
        state.add_chain_link(ChainLink(1, 0, "a", "effect_a"))
        state.add_chain_link(ChainLink(2, 1, "b", "effect_b", spell_speed=2))
        self.assertFalse(state.pass_priority())
        self.assertTrue(state.pass_priority())
        self.assertIsNone(state.priority_player)
        self.assertEqual(state.resolve_next_chain_link().card_id, "b")
        self.assertEqual(state.resolve_next_chain_link().card_id, "a")

    def test_chain_rejects_illegal_spell_speed_and_accepts_faster_response(self) -> None:
        state = GameState(phase=Phase.MAIN1)
        state.add_chain_link(ChainLink(1, 0, "spell", "activation", spell_speed=1))
        with self.assertRaisesRegex(ValueError, "cannot chain"):
            state.add_chain_link(ChainLink(2, 1, "ignition", "effect", spell_speed=1))
        state.add_chain_link(ChainLink(2, 1, "quick", "effect", spell_speed=2))
        state.add_chain_link(ChainLink(3, 0, "counter", "activation", spell_speed=3))
        with self.assertRaisesRegex(ValueError, "cannot chain"):
            state.add_chain_link(ChainLink(4, 1, "quick", "effect", spell_speed=2))

    def test_segoc_groups_mandatory_before_optional_and_respects_turn_player(self) -> None:
        candidates = [
            TriggerCandidate(1, "n-opt", "a", optional=True),
            TriggerCandidate(0, "t-opt", "b", optional=True),
            TriggerCandidate(1, "n-mandatory", "c"),
            TriggerCandidate(0, "t-mandatory", "d"),
        ]
        ordered = order_segoc_triggers(candidates, turn_player=0)
        self.assertEqual(
            [candidate.card_id for candidate in ordered],
            ["t-mandatory", "n-mandatory", "t-opt", "n-opt"],
        )

    def test_phase_turn_and_overlay_material(self) -> None:
        state = GameState(phase=Phase.END)
        self.assertEqual(state.advance_phase(), Phase.DRAW)
        self.assertEqual(state.turn_player, 1)
        host = CardInstance("h", "Host", 0, 0, Zone.MAIN_MONSTER, Position.FACE_UP_ATTACK)
        material = CardInstance("m", "Material", 0, 0, Zone.MAIN_MONSTER, Position.FACE_UP_ATTACK)
        state.players[0].main_monster_zone[0] = host
        state.players[0].main_monster_zone[1] = material
        state.attach_overlay_material(host, material)
        self.assertEqual(material.zone, Zone.MATERIAL)
        self.assertIsNone(state.players[0].main_monster_zone[1])
        detached = state.detach_overlay_material(host)
        self.assertIs(detached, material)
        self.assertEqual(material.zone, Zone.GRAVEYARD)
        self.assertIn(material, state.players[0].graveyard)


class MCTSTests(unittest.TestCase):
    def test_mcts_discovers_an_extra_deck_target(self) -> None:
        cards = {
            "Fire A": CardSpec("Fire A", level=1, attribute="FIRE", atk=800, effect_monster=True, normal_summon=True),
            "Fire B": CardSpec("Fire B", level=1, attribute="FIRE", atk=800, effect_monster=True, normal_summon=True),
            "Link Target": CardSpec(
                "Link Target", attribute="FIRE", atk=1000, effect_monster=True,
                link_rating=1, link_min_materials=1, link_requirement="level_1", interruptions=2,
            ),
        }
        deck = DeckModel("test", ("Fire A", "Fire B"), ("Link Target",), cards)
        result = run_mcts(deck, ["Fire A", "Fire B"], iterations=80, seed=11)
        self.assertTrue(any(action.kind == "link_summon" for action in result.actions))
        self.assertEqual(result.final_state.field[-1].name, "Link Target")
        self.assertEqual(result.score_components["interruptions"], 2)

    def test_live_filtered_sample_keeps_a_40_card_main_and_15_card_extra(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            snapshot = Path(temp) / "banlist.json"
            snapshot.write_text(json.dumps({"cards": [
                {"name": "Snake-Eye Oak", "ban_status": "Forbidden"},
                {"name": "Bonfire", "ban_status": "Limited"},
                {"name": "Called by the Grave", "ban_status": "Forbidden"},
                {"name": "Apollousa, Bow of the Goddess", "ban_status": "Forbidden"},
            ]}), encoding="utf-8")
            deck = load_deck(Path(__file__).parent.parent / "examples" / "snake_eye_deck.json", snapshot)
        self.assertEqual(len(deck.main_deck), 40)
        self.assertEqual(len(deck.extra_deck), 15)

    def test_snake_eye_ash_route_can_reach_promethean_princess(self) -> None:
        deck = load_deck(
            Path(__file__).parent.parent / "examples" / "snake_eye_deck.json",
            Path(__file__).parent.parent / "data" / "banlist.json",
        )
        hand = [
            "Snake-Eye Ash", "Diabellstar the Black Witch",
            "WANTED: Seeker of Sinful Spoils", "Effect Veiler", "Kurikara Divincarnate",
        ]
        state = make_initial_state(deck, hand)

        def apply(kind: str, *, card: str | None = None, target: str | None = None,
                  materials: tuple[str, ...] | None = None, cost: str | None = None) -> None:
            nonlocal state
            candidates = legal_actions(state, deck.cards)
            action = next((action for action in candidates
                           if action.kind == kind
                           and (card is None or action.card == card)
                           and (target is None or action.target == target)
                           and (materials is None or action.materials == materials)
                           and (cost is None or cost in action.costs)), None)
            if action is None:
                links = [(candidate.card, candidate.materials) for candidate in candidates if candidate.kind == "link_summon"]
                raise AssertionError(f"No {kind} for {card}; field={[u.name for u in state.field]}, links={links}")
            state = apply_action(state, action, deck.cards)

        apply("normal_summon", card="Snake-Eye Ash")
        apply("snake_eye_search", card="Snake-Eye Ash", target="Snake-Eyes Poplar")
        apply("poplar_free_summon", card="Snake-Eyes Poplar")
        apply("poplar_search", card="Snake-Eyes Poplar", target="Original Sinful Spoils - Snake-Eye")
        apply("diabellstar_self_summon", card="Diabellstar the Black Witch", cost="Effect Veiler")
        apply("diabellstar_set", card="Diabellstar the Black Witch",
              target="WANTED: Seeker of Sinful Spoils")
        self.assertFalse(any(
            action.kind == "oss_activate" and action.target == "Kurikara Divincarnate"
            for action in legal_actions(state, deck.cards)
        ))
        apply("oss_activate", card="Original Sinful Spoils - Snake-Eye",
              target="Snake-Eyes Poplar", cost="Diabellstar the Black Witch")
        self.assertFalse(any(
            action.kind == "oss_gy_search"
            for action in legal_actions(state, deck.cards)
        ))
        apply("link_summon", card="Hiita the Fire Charmer, Ablaze",
              materials=("Snake-Eye Ash", "Snake-Eyes Poplar"))
        apply("poplar_gy_set", card="Snake-Eyes Poplar")
        apply("link_summon", card="Promethean Princess, Bestower of Flames",
              materials=("Snake-Eyes Poplar", "Hiita the Fire Charmer, Ablaze"))
        self.assertIn("Promethean Princess, Bestower of Flames", [unit.name for unit in state.field])
        self.assertFalse(any(
            action.kind == "promethean_revive" and action.target == "Kurikara Divincarnate"
            for action in legal_actions(state, deck.cards)
        ))

    def test_vs_k9_deck_keeps_the_supplied_40_15_counts(self) -> None:
        root = Path(__file__).parent.parent
        deck = load_deck(root / "examples" / "vs_k9_deck.json", root / "data" / "banlist.json")
        self.assertEqual((len(deck.main_deck), len(deck.extra_deck)), (40, 15))
        self.assertEqual(deck.main_deck.count("K9-17 Izuna"), 3)
        self.assertEqual(deck.main_deck.count('"A Case for K9"'), 1)
        self.assertEqual(deck.extra_deck.count('K9-17 "Ripper"'), 2)
        self.assertEqual(parse_hand(deck, "K9-ØØ Lupis"), ["K9-00 Lupis"])

    def test_k9_noroi_and_izuna_route_reaches_rank_five_ripper(self) -> None:
        root = Path(__file__).parent.parent
        deck = load_deck(root / "examples" / "vs_k9_deck.json", root / "data" / "banlist.json")
        state = make_initial_state(deck, ["K9-04 Noroi", "K9-17 Izuna"])

        def apply(kind: str, *, card: str, target: str | None = None,
                  materials: tuple[str, ...] | None = None) -> None:
            nonlocal state
            action = next((candidate for candidate in legal_actions(state, deck.cards)
                           if candidate.kind == kind and candidate.card == card
                           and (target is None or candidate.target == target)
                           and (materials is None or candidate.materials == materials)), None)
            self.assertIsNotNone(action, f"Missing {kind} for {card}")
            state = apply_action(state, action, deck.cards)

        apply("normal_summon", card="K9-04 Noroi")
        apply("k9_noroi_summon", card="K9-04 Noroi", target="K9-17 Izuna")
        apply("k9_izuna_send", card="K9-17 Izuna", target="K9-66a Jokul")
        self.assertFalse(any(action.kind == "link_summon" and action.card == "Rock of the Vanquisher"
                             for action in legal_actions(state, deck.cards)))
        materials = ("K9-04 Noroi", "K9-17 Izuna")
        apply("xyz_summon", card='K9-17 "Ripper"', materials=materials)
        self.assertEqual(state.field[-1].materials, materials)
        score, components = evaluate_board(state, deck.cards)
        self.assertEqual(components["negates"], 1)
        self.assertEqual(components["total_atk"], deck.cards['K9-17 "Ripper"'].atk)

    def test_vs_stake_reveals_without_discarding_the_attribute_card(self) -> None:
        root = Path(__file__).parent.parent
        deck = load_deck(root / "examples" / "vs_k9_deck.json", root / "data" / "banlist.json")
        state = make_initial_state(deck, ["Stake your Soul!", "Vanquish Soul Razen"])
        action = next(action for action in legal_actions(state, deck.cards)
                      if action.kind == "vs_stake" and action.reveals == ("Vanquish Soul Razen",))
        state = apply_action(state, action, deck.cards)
        self.assertIn("Vanquish Soul Razen", state.hand)
        self.assertEqual(state.field[-1].name, action.target)
        _, components = evaluate_board(state, deck.cards)
        self.assertEqual(components["attribute_coverage"], 1)

    def test_vs_reveal_attributes_enable_interaction_and_rock_link(self) -> None:
        root = Path(__file__).parent.parent
        deck = load_deck(root / "examples" / "vs_k9_deck.json", root / "data" / "banlist.json")
        hand = ["Vanquish Soul Razen", "Vanquish Soul Jiaolong", "Vanquish Soul Dr. Mad Love"]
        state = make_initial_state(deck, hand)
        normal = next(action for action in legal_actions(state, deck.cards)
                      if action.kind == "normal_summon" and action.card == "Vanquish Soul Razen")
        state = apply_action(state, normal, deck.cards)
        search = next(action for action in legal_actions(state, deck.cards)
                      if action.kind == "vs_search_monster" and action.target == "Vanquish Soul Heavy Borger")
        state = apply_action(state, search, deck.cards)
        _, components = evaluate_board(state, deck.cards)
        self.assertEqual(components["interruptions"], 1)
        self.assertEqual(components["attribute_coverage"], 2)
        rock = next(action for action in legal_actions(state, deck.cards)
                    if action.kind == "link_summon" and action.card == "Rock of the Vanquisher")
        state = apply_action(state, rock, deck.cards)
        self.assertIn("Rock of the Vanquisher", [unit.name for unit in state.field])


if __name__ == "__main__":
    unittest.main()
