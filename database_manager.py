"""Persistent, indexed SQLite storage for combo routes and human benchmarks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "combo_cache.db"
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()
_THREAD_CONNECTIONS = threading.local()


def hand_hash(hand: Iterable[str]) -> str:
    """Return a stable SHA-256 key for a hand regardless of card order."""
    normalized = sorted(
        unicodedata.normalize("NFKC", str(card)).strip().casefold()
        for card in hand
    )
    payload = "\0".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DatabaseManager:
    """One indexed SQLite connection per calling thread keeps repeated lookups fast."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.db_path.resolve()).casefold()
        pool = getattr(_THREAD_CONNECTIONS, "connections", None)
        if pool is None:
            pool = {}
            _THREAD_CONNECTIONS.connections = pool
        cached = pool.get(key)
        if cached is not None:
            return cached
        file_was_present = self.db_path.exists() and self.db_path.stat().st_size > 0
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        if key not in _SCHEMA_READY or not file_was_present:
            with _SCHEMA_LOCK:
                if key not in _SCHEMA_READY or not file_was_present:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS combos (
                            hand_hash TEXT PRIMARY KEY,
                            deck_name TEXT NOT NULL,
                            route_json TEXT NOT NULL,
                            end_board TEXT NOT NULL,
                            score REAL NOT NULL,
                            is_gold_route INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            mode TEXT NOT NULL DEFAULT 'going_1st'
                        )"""
                    )
                    columns = {
                        row["name"]
                        for row in connection.execute("PRAGMA table_info(combos)").fetchall()
                    }
                    if "mode" not in columns:
                        connection.execute(
                            "ALTER TABLE combos ADD COLUMN mode TEXT NOT NULL DEFAULT 'going_1st'"
                        )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_combos_hand_hash ON combos(hand_hash)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_combos_context ON combos(deck_name, mode, hand_hash)"
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS gold_routes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            hand_hash TEXT NOT NULL,
                            deck_name TEXT NOT NULL,
                            route_json TEXT NOT NULL,
                            end_board TEXT NOT NULL,
                            score REAL NOT NULL,
                            created_at TEXT NOT NULL,
                            mode TEXT NOT NULL DEFAULT 'going_1st'
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_gold_routes_context_score "
                        "ON gold_routes(deck_name, mode, hand_hash, score DESC)"
                    )
                    connection.commit()
                    _SCHEMA_READY.add(key)
        pool[key] = connection
        return connection

    def close(self) -> None:
        """Close this database's cached connection for the current thread."""
        pool = getattr(_THREAD_CONNECTIONS, "connections", {})
        connection = pool.pop(str(self.db_path.resolve()).casefold(), None)
        if connection is not None:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.commit()

    def get_combo(
        self, hand: Iterable[str], *, deck_name: str | None = None, mode: str | None = None
    ) -> dict[str, Any] | None:
        key = hand_hash(hand)
        connection = self._connect()
        gold_query = (
            "SELECT hand_hash, deck_name, route_json, end_board, score, "
            "1 AS is_gold_route, created_at, mode FROM gold_routes WHERE hand_hash = ?"
        )
        gold_parameters: list[Any] = [key]
        if deck_name is not None:
            gold_query += " AND deck_name = ? COLLATE NOCASE"
            gold_parameters.append(deck_name)
        if mode is not None:
            gold_query += " AND mode = ?"
            gold_parameters.append(mode)
        gold_query += " ORDER BY score DESC, created_at DESC LIMIT 1"
        row = connection.execute(gold_query, gold_parameters).fetchone()
        if row is None:
            query = (
                "SELECT hand_hash, deck_name, route_json, end_board, score, "
                "is_gold_route, created_at, mode FROM combos WHERE hand_hash = ?"
            )
            parameters: list[Any] = [key]
            if deck_name is not None:
                query += " AND deck_name = ? COLLATE NOCASE"
                parameters.append(deck_name)
            if mode is not None:
                query += " AND mode = ?"
                parameters.append(mode)
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        value = dict(row)
        if deck_name is not None and value["deck_name"].casefold() != deck_name.casefold():
            return None
        if mode is not None and value["mode"] != mode:
            return None
        value["route"] = json.loads(value.pop("route_json"))
        value["end_board"] = json.loads(value["end_board"])
        value["is_gold_route"] = bool(value["is_gold_route"])
        return value

    def save_combo(
        self,
        *,
        hand: Iterable[str],
        deck_name: str,
        route_steps: Iterable[str],
        end_board: Iterable[str],
        score: float,
        is_gold_route: bool = False,
        mode: str = "going_1st",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        hand_list = list(hand)
        end_board_list = list(end_board)
        key = hand_hash(hand_list)
        route = {
            "hand": hand_list,
            "steps": list(route_steps),
            "mode": mode,
            **(metadata or {}),
        }
        connection = self._connect()
        try:
            current = connection.execute(
                "SELECT deck_name, mode, score, is_gold_route FROM combos WHERE hand_hash = ?",
                (key,),
            ).fetchone()
            same_context = current and current["deck_name"].casefold() == deck_name.casefold() and current["mode"] == mode
            if is_gold_route:
                connection.execute(
                    """INSERT INTO gold_routes(hand_hash, deck_name, route_json, end_board, score, created_at, mode)
                       VALUES(?, ?, ?, ?, ?, datetime('now'), ?)""",
                    (
                        key,
                        deck_name,
                        json.dumps(route, ensure_ascii=False),
                        json.dumps(end_board_list, ensure_ascii=False),
                        float(score),
                        mode,
                    ),
                )
            if same_context and current["is_gold_route"] and not is_gold_route:
                connection.commit()
                return key
            if same_context and current["is_gold_route"] and is_gold_route and current["score"] > score:
                connection.commit()
                return key
            if same_context and not current["is_gold_route"] and not is_gold_route and current["score"] > score:
                connection.commit()
                return key
            connection.execute(
                """INSERT INTO combos(hand_hash, deck_name, route_json, end_board, score,
                       is_gold_route, created_at, mode)
                   VALUES(?, ?, ?, ?, ?, ?, datetime('now'), ?)
                   ON CONFLICT(hand_hash) DO UPDATE SET deck_name=excluded.deck_name,
                     route_json=excluded.route_json, end_board=excluded.end_board,
                     score=excluded.score, is_gold_route=excluded.is_gold_route,
                     created_at=excluded.created_at, mode=excluded.mode""",
                (
                    key,
                    deck_name,
                    json.dumps(route, ensure_ascii=False),
                    json.dumps(end_board_list, ensure_ascii=False),
                    float(score),
                    int(is_gold_route),
                    mode,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return key

    def evaluate_human_route(
        self,
        route_steps: Iterable[str],
        *,
        hand: Iterable[str],
        end_board: Iterable[str],
        score: float,
        mcts_score: float,
        deck_name: str,
        mode: str = "going_1st",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Benchmark a replayed human route and retain it as Gold when it ties or wins."""
        hand_list = list(hand)
        human_score = float(score)
        baseline_score = float(mcts_score)
        is_gold = human_score >= baseline_score
        key = self.save_combo(
            hand=hand_list,
            deck_name=deck_name,
            route_steps=route_steps,
            end_board=end_board,
            score=human_score,
            is_gold_route=is_gold,
            mode=mode,
            metadata={"baseline_score": baseline_score, **(metadata or {})},
        )
        best = self.get_combo(hand_list, deck_name=deck_name, mode=mode)
        return {
            "hand_hash": key,
            "is_gold_route": is_gold,
            "stored_gold_route": bool(best and best["is_gold_route"]),
            "stored_score": best["score"] if best else human_score,
            "human_score": human_score,
            "mcts_score": baseline_score,
            "route": best["route"] if best else None,
        }


def evaluate_human_route(
    route_steps: Iterable[str],
    *,
    hand: Iterable[str],
    end_board: Iterable[str],
    score: float,
    mcts_score: float,
    deck_name: str,
    mode: str = "going_1st",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Convenience entry point used by the dashboard and command-line tools."""
    return DatabaseManager(db_path).evaluate_human_route(
        route_steps,
        hand=hand,
        end_board=end_board,
        score=score,
        mcts_score=mcts_score,
        deck_name=deck_name,
        mode=mode,
    )
