from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database_manager import DatabaseManager, hand_hash

try:
    import web_app
except (ImportError, RuntimeError):
    web_app = None


class WebDashboardTests(unittest.TestCase):
    @unittest.skipIf(web_app is None, "Install requirements-web.txt for route generator tests.")
    def test_ai_route_generator_runs_mcts_then_returns_compatible_cache_entry(self) -> None:
        request = web_app.GenerateRouteRequest(
            deck_file="vs_k9_deck.json",
            hand=[
                "K9-04 Noroi",
                "K9-17 Izuna",
                "Ash Blossom & Joyous Spring",
                "Vanquish Soul Razen",
                "Stake your Soul!",
            ],
            mode="going_1st",
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            cache_file = Path(temp) / "combo_cache.db"
            with patch.object(web_app, "CACHE_DB", cache_file), patch.object(
                web_app, "AI_ROUTE_ITERATIONS", 30
            ):
                first = web_app.generate_ai_route(request)
                DatabaseManager(cache_file).close()
                second = web_app.generate_ai_route(request)
                DatabaseManager(cache_file).close()
        self.assertEqual(len(first["hand"]), 5)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["simulations"], 30)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["steps"], first["steps"])

    def test_route_lab_frontend_separates_auto_and_human_routes(self) -> None:
        root = Path(__file__).parent.parent
        html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="generate-ai-route"', html)
        for slot in range(1, 6):
            self.assertIn(f'id="hand-slot-{slot}"', html)
        self.assertIn('id="human-route-steps"', html)
        self.assertIn("/api/generate-route", script)
        self.assertIn("Array.from(new Set(mainCards.map", script)
        self.assertIn("Vui lòng nhập các bước đi để AI chấm điểm", script)

    def test_database_cache_is_order_independent_and_preserves_gold_routes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            manager = DatabaseManager(Path(temp) / "combo_cache.db")
            hand = ["K9-04 Noroi", "K9-17 Izuna", "Ash Blossom"]
            self.assertEqual(hand_hash(hand), hand_hash(reversed(hand)))
            result = manager.evaluate_human_route(
                ["Normal Summon Noroi", "Xyz Summon Ripper"],
                hand=hand,
                end_board=['K9-17 "Ripper"'],
                score=12.0,
                mcts_score=12.0,
                deck_name="VSxK9",
            )
            self.assertTrue(result["is_gold_route"])
            cached = manager.get_combo(list(reversed(hand)), deck_name="vsxk9", mode="going_1st")
            self.assertIsNotNone(cached)
            self.assertTrue(cached["is_gold_route"])
            self.assertEqual(cached["end_board"], ['K9-17 "Ripper"'])
            weaker = manager.evaluate_human_route(
                ["Normal Summon Noroi"],
                hand=hand,
                end_board=["K9-04 Noroi"],
                score=1.0,
                mcts_score=12.0,
                deck_name="VSxK9",
            )
            self.assertFalse(weaker["is_gold_route"])
            self.assertEqual(manager.get_combo(hand)["route"]["steps"],
                             ["Normal Summon Noroi", "Xyz Summon Ripper"])
            manager.close()

    @unittest.skipIf(web_app is None, "Install requirements-web.txt for dashboard tests.")
    def test_catalog_loads_once_with_mdm_card_names(self) -> None:
        first = web_app.load_card_catalog()
        second = web_app.load_card_catalog()
        self.assertIs(first, second)
        names = {card["name"] for card in first}
        self.assertIn('"A Case for K9"', names)
        self.assertIn("K9-ØØ Lupis", names)
        self.assertGreater(len(first), 7000)

    @unittest.skipIf(web_app is None, "Install requirements-web.txt for route benchmark tests.")
    def test_human_route_is_simulated_and_cached(self) -> None:
        request = web_app.BenchmarkRequest(
            deck_file="vs_k9_deck.json",
            hand=["K9-04 Noroi", "K9-17 Izuna"],
            route_steps=[
                "Normal Summon [K9-04 Noroi]",
                "Activate [K9-04 Noroi] on summon; Special Summon [K9-17 Izuna] from Deck",
                "Activate [K9-17 Izuna] on summon; send [K9-66a Jokul] from Deck to GY",
                'Xyz Summon [K9-17 "Ripper"] using [K9-04 Noroi, K9-17 Izuna]',
            ],
            baseline_iterations=50,
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            cache_file = Path(temp) / "combo_cache.db"
            with patch.object(web_app, "CACHE_DB", cache_file):
                result = web_app.evaluate_human_route(request)
            DatabaseManager(cache_file).close()
            connection = sqlite3.connect(cache_file)
            try:
                row = connection.execute("SELECT is_gold_route, end_board FROM combos").fetchone()
            finally:
                connection.close()
        self.assertEqual(len(result["steps"]), 4)
        self.assertIn('K9-17 "Ripper"', result["end_board"])
        self.assertIsNotNone(row)

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("cv2", "numpy", "mss")),
                         "Install requirements-vision.txt for template matching tests.")
    def test_vision_matches_local_grayscale_card_template(self) -> None:
        import cv2
        import numpy as np

        from vision_engine import VisionConfig, VisionEngine

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            template_dir = Path(temp)
            template = np.random.default_rng(9).integers(0, 256, (116, 82), dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(template_dir / "K9 Sample.png"), template))
            screen = np.zeros((116, 200, 3), dtype=np.uint8)
            screen[:, :82] = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)
            engine = VisionEngine(
                template_dir=template_dir,
                config=VisionConfig(hand_region=(0, 0, 1, 1), log_region=(0, 0, 1, 1), hand_card_count=1),
            )
            result = engine.scan_image(screen)
        self.assertEqual(result["hand"], ["K9 Sample"])
        self.assertEqual(result["templates_loaded"], 1)


if __name__ == "__main__":
    unittest.main()
