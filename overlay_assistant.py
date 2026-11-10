"""Non-invasive PyQt6 HUD; MCTS, screen capture, and process polling run on QThreads."""

from __future__ import annotations

import argparse
import os
import random
import sys
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from mcts_search import (
    Action,
    DeckModel,
    MCTSResult,
    SimState,
    apply_action,
    load_deck,
    make_initial_state,
    run_mcts,
)
from generate_combo import parse_hand
from vision_engine import VisionEngine, inspect_master_duel_process


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"
BANLIST = ROOT / "data" / "banlist.json"
HANDTRAP_ADVICE = {
    "ash blossom & joyous spring": "Chain Ash to a search, Deck-to-GY send, or Special Summon from Deck.",
    "ash blossom": "Chain Ash to a search, Deck-to-GY send, or Special Summon from Deck.",
    'maxx "c"': "Use Maxx \"C\" on the first opposing Special Summon to start the draw pressure.",
    "maxx c": "Use Maxx \"C\" on the first opposing Special Summon to start the draw pressure.",
    "mulcharmy fuwalos": "Use Fuwalos before the opponent begins Special Summoning from Deck or Extra Deck.",
    "ghost belle & haunted mansion": "Save Belle for a move that revives, banishes, or returns a card from the GY.",
}


def opponent_choke_point_advice(hand: Sequence[str]) -> list[str]:
    """Give short reminders for recognizable hand traps currently in the scanned hand."""
    advice: list[str] = []
    seen: set[str] = set()
    for card in hand:
        key = card.casefold().strip()
        message = HANDTRAP_ADVICE.get(key)
        if message and message not in seen:
            advice.append(f"{card}: {message}")
            seen.add(message)
    return advice or ["No recognized hand trap in this hand; pass priority only after checking opponent actions."]


try:
    from PyQt6.QtCore import QThread, Qt, pyqtSignal
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # Allows importing advice helpers without the optional desktop runtime.
    QApplication = None  # type: ignore[assignment,misc]
    QT_IMPORT_ERROR: Exception | None = exc
else:
    QT_IMPORT_ERROR = None


if QApplication is not None:

    class MctsWorker(QThread):
        result_ready = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(
            self,
            deck: DeckModel,
            hand: Sequence[str],
            *,
            iterations: int,
            mode: str,
            initial_state: SimState | None = None,
            prefix: Sequence[Action] = (),
            seed: int = 7,
        ) -> None:
            super().__init__()
            self.deck = deck
            self.hand = tuple(hand)
            self.iterations = iterations
            self.mode = mode
            self.initial_state = initial_state
            self.prefix = tuple(prefix)
            self.seed = seed

        def run(self) -> None:
            try:
                result = run_mcts(
                    self.deck,
                    self.hand,
                    iterations=self.iterations,
                    rollout_depth=24,
                    seed=self.seed,
                    mode=self.mode,
                    initial_state=self.initial_state,
                    should_stop=self.isInterruptionRequested,
                )
                self.result_ready.emit((result, self.prefix))
            except Exception as exc:
                self.failed.emit(f"{type(exc).__name__}: {exc}")


    class VisionMonitorThread(QThread):
        game_state = pyqtSignal(object)
        vision_result = pyqtSignal(object)

        def __init__(self, engine: VisionEngine, interval_ms: int = 750) -> None:
            super().__init__()
            self.engine = engine
            self.interval_ms = max(250, interval_ms)
            self._stopping = False

        def request_stop(self) -> None:
            self._stopping = True
            self.requestInterruption()

        def run(self) -> None:
            previous: tuple[bool, bool] | None = None
            while not self._stopping and not self.isInterruptionRequested():
                state = inspect_master_duel_process()
                current = (bool(state["running"]), bool(state["foreground"]))
                if current != previous:
                    self.game_state.emit(state)
                    previous = current
                if current[0] and current[1]:
                    try:
                        self.vision_result.emit(self.engine.scan_screen())
                    except Exception as exc:
                        self.vision_result.emit({"error": f"{type(exc).__name__}: {exc}", "hand": []})
                self.msleep(self.interval_ms)


    class RouteStepWidget(QFrame):
        pivot_requested = pyqtSignal(int)

        def __init__(self, index: int, action: Action, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("routeStep")
            row = QHBoxLayout(self)
            row.setContentsMargins(8, 6, 8, 6)
            label = QLabel(f"Bước {index + 1}: {action}")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            pivot = QPushButton("⚡")
            pivot.setToolTip("Tính lại từ sau bước này bằng 5.000 lượt MCTS")
            pivot.setFixedWidth(38)
            pivot.clicked.connect(lambda: self.pivot_requested.emit(index))
            row.addWidget(label, 1)
            row.addWidget(pivot)


    class OverlayAssistant(QMainWindow):
        def __init__(self, *, web_url: str, open_browser: bool = True) -> None:
            super().__init__()
            self.web_url = web_url
            self.open_browser = open_browser
            self.engine = VisionEngine()
            self.game_state: dict[str, Any] = {"running": False, "foreground": False}
            self.last_vision: dict[str, Any] = {"hand": []}
            self.active_deck: DeckModel | None = None
            self.active_hand: list[str] = []
            self.active_mode = "going_1st"
            self.active_actions: list[Action] = []
            self.active_state: SimState | None = None
            self.worker: MctsWorker | None = None
            self._drag_offset = None
            self.setWindowTitle("Yu-Gi-Oh! AI Duel Assistant")
            self.setWindowFlags(
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setMinimumWidth(410)
            self.setMaximumWidth(500)
            self._build_ui()
            self.monitor = VisionMonitorThread(self.engine)
            self.monitor.game_state.connect(self._on_game_state)
            self.monitor.vision_result.connect(self._on_vision_result)
            self.monitor.start()
            if self.open_browser:
                from PyQt6.QtCore import QTimer

                QTimer.singleShot(1600, lambda: webbrowser.open(self.web_url))

        def _build_ui(self) -> None:
            root = QWidget()
            root.setObjectName("hudPanel")
            self.setCentralWidget(root)
            root.setStyleSheet(
                "QWidget#hudPanel { background: rgba(9, 9, 11, 208); border: 1px solid #3f3f46;"
                " border-radius: 12px; color: #fafafa; }"
                "QLabel { color: #e4e4e7; } QComboBox, QLineEdit { background: #18181b;"
                " border: 1px solid #3f3f46; border-radius: 6px; padding: 6px; color: #fafafa; }"
                "QPushButton { background: #27272a; border: 1px solid #52525b; border-radius: 6px;"
                " color: #fafafa; padding: 7px; } QPushButton:hover { background: #3f3f46; }"
                "QFrame#routeStep { background: rgba(24, 24, 27, 235); border: 1px solid #3f3f46;"
                " border-radius: 7px; } QScrollArea { border: 0; background: transparent; }"
            )
            layout = QVBoxLayout(root)
            layout.setContentsMargins(12, 10, 12, 10)
            header = QHBoxLayout()
            title = QLabel("DUEL ASSISTANT")
            title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            self.status_label = QLabel("Đang chờ Master Duel")
            self.status_label.setStyleSheet("color: #fbbf24;")
            close_button = QPushButton("×")
            close_button.setFixedWidth(30)
            close_button.setToolTip("Dừng HUD và đóng ứng dụng")
            close_button.clicked.connect(self.close)
            header.addWidget(title)
            header.addStretch(1)
            header.addWidget(self.status_label)
            header.addWidget(close_button)
            layout.addLayout(header)

            controls = QHBoxLayout()
            self.deck_picker = QComboBox()
            for path in sorted(EXAMPLES.glob("*.json")):
                try:
                    from mcts_search import load_deck

                    deck = load_deck(path, BANLIST)
                    if deck.main_deck and deck.extra_deck:
                        self.deck_picker.addItem(deck.name, path.name)
                except Exception:
                    continue
            default_index = self.deck_picker.findData("vs_k9_deck.json")
            if default_index >= 0:
                self.deck_picker.setCurrentIndex(default_index)
            self.mode_picker = QComboBox()
            self.mode_picker.addItem("Đi trước", "going_1st")
            self.mode_picker.addItem("Đi sau", "going_2nd")
            controls.addWidget(self.deck_picker, 2)
            controls.addWidget(self.mode_picker, 1)
            layout.addLayout(controls)

            self.hand_input = QLineEdit()
            self.hand_input.setPlaceholderText("Nhập tay bài, ngăn cách bằng dấu |")
            layout.addWidget(self.hand_input)
            action_row = QHBoxLayout()
            self.solve_button = QPushButton("Tìm combo")
            self.solve_button.clicked.connect(self._solve_hand)
            random_button = QPushButton("Bốc tay ngẫu nhiên")
            random_button.clicked.connect(self._random_hand)
            action_row.addWidget(self.solve_button)
            action_row.addWidget(random_button)
            layout.addLayout(action_row)
            self.hand_label = QLabel("Tay bài: chưa nhận diện")
            self.hand_label.setWordWrap(True)
            layout.addWidget(self.hand_label)
            self.score_label = QLabel("End-board: —")
            self.score_label.setWordWrap(True)
            layout.addWidget(self.score_label)

            self.route_content = QWidget()
            self.route_layout = QVBoxLayout(self.route_content)
            self.route_layout.setContentsMargins(0, 0, 0, 0)
            self.route_layout.setSpacing(5)
            self.route_scroll = QScrollArea()
            self.route_scroll.setWidgetResizable(True)
            self.route_scroll.setMinimumHeight(150)
            self.route_scroll.setMaximumHeight(310)
            self.route_scroll.setWidget(self.route_content)
            layout.addWidget(self.route_scroll)

            self.advice_label = QLabel("Advisor: chờ nhận diện tay bài.")
            self.advice_label.setWordWrap(True)
            self.advice_label.setStyleSheet("color: #a1a1aa; font-size: 11px;")
            layout.addWidget(self.advice_label)
            footer = QLabel("Kéo thanh tiêu đề để di chuyển • [⚡] tính lại sau bước đó")
            footer.setStyleSheet("color: #71717a; font-size: 10px;")
            layout.addWidget(footer)

        def _on_game_state(self, state: dict[str, Any]) -> None:
            self.game_state = state
            visible = bool(state.get("running") and state.get("foreground"))
            if visible:
                self.status_label.setText("Master Duel đang hoạt động")
                self.status_label.setStyleSheet("color: #4ade80;")
                if not self.isVisible():
                    self.show()
                self.raise_()
            else:
                reason = "Master Duel đã tắt" if not state.get("running") else "Tạm ẩn khi Alt-Tab"
                self.status_label.setText(reason)
                self.status_label.setStyleSheet("color: #fbbf24;")
                self.hide()

        def _on_vision_result(self, result: dict[str, Any]) -> None:
            self.last_vision = result
            hand = result.get("hand", [])
            if hand:
                self.hand_label.setText("Tay bài nhận diện: " + " | ".join(hand))
                self.advice_label.setText("Advisor: " + "\n".join(opponent_choke_point_advice(hand)))
                if not self.hand_input.text().strip():
                    self.hand_input.setText(" | ".join(hand))
            elif result.get("error"):
                self.hand_label.setText("Vision: " + result["error"])
            elif result.get("template_warning"):
                self.hand_label.setText("Vision sẵn sàng; chưa có ảnh mẫu trong data/card_thumbnails.")

        def _selected_deck(self) -> DeckModel:
            filename = self.deck_picker.currentData()
            if not filename:
                raise ValueError("Không tìm thấy deck mẫu trong examples/.")
            return load_deck(EXAMPLES / filename, BANLIST if BANLIST.exists() else None)

        def _solve_hand(self) -> None:
            if self.worker and self.worker.isRunning():
                self.status_label.setText("MCTS đang chạy…")
                return
            raw_hand = self.hand_input.text().strip()
            if not raw_hand:
                raw_hand = " | ".join(self.last_vision.get("hand", []))
            try:
                deck = self._selected_deck()
                hand = parse_hand(deck, raw_hand) if raw_hand.strip() else []
            except ValueError as exc:
                self.status_label.setText(str(exc))
                return
            if not hand:
                self.status_label.setText("Nhập tay bài hoặc thêm ảnh mẫu trước")
                return
            self._start_search(hand, deck, self.mode_picker.currentData() or "going_1st")

        def _random_hand(self) -> None:
            if self.worker and self.worker.isRunning():
                return
            deck = self._selected_deck()
            if len(deck.main_deck) < 5:
                self.status_label.setText("Deck phải có ít nhất 5 lá")
                return
            hand = random.sample(list(deck.main_deck), 5)
            self.hand_input.setText(" | ".join(hand))
            self._start_search(hand, deck, self.mode_picker.currentData() or "going_1st")

        def _start_search(
            self,
            hand: Sequence[str],
            deck: DeckModel,
            mode: str,
            *,
            initial_state: SimState | None = None,
            prefix: Sequence[Action] = (),
            iterations: int = 1800,
        ) -> None:
            if self.worker and self.worker.isRunning():
                self.status_label.setText("MCTS đang chạy…")
                return
            try:
                if initial_state is None:
                    make_initial_state(deck, hand)
            except ValueError as exc:
                self.status_label.setText(str(exc))
                return
            self.active_deck = deck
            self.active_hand = list(hand)
            self.active_mode = mode
            self.solve_button.setEnabled(False)
            self.status_label.setText(f"Đang tìm… {iterations:,} lượt MCTS")
            self.worker = MctsWorker(
                deck,
                hand,
                iterations=iterations,
                mode=mode,
                initial_state=initial_state,
                prefix=prefix,
                seed=17,
            )
            self.worker.result_ready.connect(self._on_search_result)
            self.worker.failed.connect(self._on_search_error)
            self.worker.finished.connect(lambda: self.solve_button.setEnabled(True))
            self.worker.start()

        def _on_search_result(self, payload: tuple[MCTSResult, Sequence[Action]]) -> None:
            result, prefix = payload
            self.active_actions = [*prefix, *result.actions]
            self.active_state = result.final_state
            self._clear_route()
            if not self.active_actions:
                self._add_message("Không tìm được hành động hợp lệ từ tay bài này.")
            else:
                for index, action in enumerate(self.active_actions):
                    row = RouteStepWidget(index, action)
                    row.pivot_requested.connect(self._pivot_from_step)
                    self.route_layout.addWidget(row)
            self.route_layout.addStretch(1)
            field = [unit.name for unit in result.final_state.field]
            self.score_label.setText(
                f"End-board: {', '.join(field) if field else 'chưa có quái trên sân'}\n"
                f"Điểm {result.score:.2f} • Negate {result.score_components.get('negates', 0):g} • "
                f"Tương tác {result.score_components.get('interruptions', 0):g} • "
                f"{result.simulations:,} lượt / {result.expanded_nodes:,} node"
            )
            self.status_label.setText("Combo đã sẵn sàng")
            advice_hand = self.active_hand or list(self.last_vision.get("hand", []))
            self.advice_label.setText("Advisor: " + "\n".join(opponent_choke_point_advice(advice_hand)))

        def _on_search_error(self, error: str) -> None:
            self.status_label.setText("MCTS lỗi")
            self._clear_route()
            self._add_message(error)

        def _clear_route(self) -> None:
            while self.route_layout.count():
                item = self.route_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        def _add_message(self, message: str) -> None:
            label = QLabel(message)
            label.setWordWrap(True)
            self.route_layout.addWidget(label)

        def _pivot_from_step(self, step_index: int) -> None:
            if not self.active_deck or not self.active_actions:
                return
            prefix = self.active_actions[: step_index + 1]
            try:
                state = make_initial_state(self.active_deck, self.active_hand)
                for action in prefix:
                    state = apply_action(state, action, self.active_deck.cards)
            except Exception as exc:
                self.status_label.setText(f"Không thể dựng lại state: {exc}")
                return
            self._start_search(
                self.active_hand,
                self.active_deck,
                self.active_mode,
                initial_state=state,
                prefix=prefix,
                iterations=5000,
            )

        def mousePressEvent(self, event: Any) -> None:
            if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 38:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event: Any) -> None:
            if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event: Any) -> None:
            self._drag_offset = None
            super().mouseReleaseEvent(event)

        def closeEvent(self, event: Any) -> None:
            self.monitor.request_stop()
            self.monitor.wait(4000)
            if self.worker and self.worker.isRunning():
                self.worker.requestInterruption()
                self.worker.wait(2500)
            super().closeEvent(event)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-url", default=os.environ.get("YGO_WEB_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if QApplication is None:
        raise SystemExit(
            "PyQt6 is required for the desktop overlay. Install it with "
            "'uv pip install --python .venv\\Scripts\\python.exe PyQt6'."
        ) from QT_IMPORT_ERROR
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Yu-Gi-Oh! AI Duel Assistant")
    overlay = OverlayAssistant(web_url=args.web_url, open_browser=not args.no_browser)
    overlay.show()
    overlay.hide()  # The process monitor shows it only while Master Duel is foreground.
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
