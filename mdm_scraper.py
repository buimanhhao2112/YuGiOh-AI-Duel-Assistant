#!/usr/bin/env python3
"""Fetch, normalize and diff-sync Master Duel Meta's public card API."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ygo_ai.cards import Card, normalize_mdm_card


DEFAULT_ENDPOINT = "https://www.masterduelmeta.com/api/v1/cards"
USER_AGENT = "YuGiOh-AI-Combo-Lab/0.1 (public card data sync; polite pagination)"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ScrapeError(RuntimeError):
    """Raised for an API response that cannot be safely consumed."""


class DatabaseAdapter:
    """Small placeholder/transaction adapter shared by SQLite and psycopg 3."""

    def __init__(self, connection: Any, *, postgres: bool):
        self.connection = connection
        self.postgres = postgres

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        if self.postgres:
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, parameters)

    def __enter__(self) -> "DatabaseAdapter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        return False

    def close(self) -> None:
        self.connection.close()


def _extract_records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("cards", "results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
    raise ScrapeError("API response was not a card array or a supported paginated object.")


def request_json(
    endpoint: str,
    params: Mapping[str, Any],
    timeout: float = 30,
    retries: int = 4,
) -> Any:
    url = f"{endpoint}?{urlencode(params)}" if params else endpoint
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset))
        except HTTPError as exc:
            if exc.code == 403:
                raise ScrapeError(
                    "MDM returned HTTP 403 (possibly access policy/anti-bot). Stopping without "
                    "trying to bypass it; retry later or use an authorized data export."
                ) from exc
            if exc.code not in RETRYABLE_STATUS or attempt >= retries:
                raise ScrapeError(f"MDM request failed with HTTP {exc.code}: {url}") from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = min(60.0, float(retry_after)) if retry_after else 1.5**attempt
            except ValueError:
                delay = 1.5**attempt
            time.sleep(delay)
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise ScrapeError(f"Could not fetch valid JSON from MDM: {exc}") from exc
            time.sleep(min(30.0, 1.5**attempt))
    raise ScrapeError("Request retries exhausted.")


def fetch_all_cards(
    endpoint: str,
    limit_per_page: int = 1000,
    max_pages: int = 100,
    interval: float = 0.25,
    total_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read cards with 1-based pages and offsets, deduplicating by stable card ID."""
    if limit_per_page < 1:
        raise ValueError("limit_per_page must be positive.")
    if max_pages < 1:
        raise ValueError("max_pages must be positive.")
    if total_limit is not None and total_limit < 1:
        raise ValueError("total_limit must be positive when set.")
    by_id: dict[str, dict[str, Any]] = {}
    seen_card_ids: set[str] = set()
    no_new_card_pages = 0
    for page_number in range(max_pages):
        offset = page_number * limit_per_page
        payload = request_json(
            endpoint,
            {
                "cardSort": "popRank",
                "aggregate": "search",
                "limit": limit_per_page,
                "page": page_number + 1,
                "offset": offset,
                "skip": offset,
            },
        )
        rows = _extract_records(payload)
        if not rows:
            break
        new_on_page = 0
        for row in rows:
            card_id = str(row.get("gameId") or row.get("konamiID") or row.get("_id") or row.get("name"))
            if card_id in seen_card_ids:
                continue
            seen_card_ids.add(card_id)
            by_id[card_id] = dict(row)
            new_on_page += 1
            if total_limit is not None and len(by_id) >= total_limit:
                break
        if total_limit is not None and len(by_id) >= total_limit:
            break
        if new_on_page == 0:
            no_new_card_pages += 1
            if no_new_card_pages >= 2:
                print(
                    "Warning: MDM pagination repeated card IDs; ending page scan without "
                    "raising. Check API pagination and --limit-per-page.",
                    file=sys.stderr,
                )
                break
        else:
            no_new_card_pages = 0
        if len(rows) < limit_per_page:
            break
        if page_number + 1 < max_pages and interval:
            time.sleep(max(0.0, interval))
    else:
        raise ScrapeError(
            f"Reached --max-pages={max_pages}; increase the limit if the card list is larger."
        )
    if not by_id:
        raise ScrapeError("MDM returned zero cards; refusing to sync an empty result.")
    return list(by_id.values())


def _banlist_records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, Mapping)]
    if isinstance(value, Mapping):
        for key in ("cards", "data", "entries", "banlist"):
            records = value.get(key)
            if isinstance(records, list):
                return [entry for entry in records if isinstance(entry, Mapping)]
    raise ScrapeError("Banlist JSON must be an array or contain a cards/data/entries array.")


def load_banlist(path: Path | None) -> dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    try:
        records = _banlist_records(json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise ScrapeError(f"Cannot read banlist file {path}: {exc}") from exc
    index: dict[str, Mapping[str, Any]] = {}
    for row in records:
        for key in ("card_id", "gameId", "konamiID", "id", "_id", "name"):
            value = row.get(key)
            if value not in (None, ""):
                index[str(value).casefold()] = row
    return index


def match_banlist_entry(
    raw: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    for key in ("gameId", "card_id", "konamiID", "id", "_id", "name"):
        value = raw.get(key)
        if value not in (None, "") and str(value).casefold() in index:
            return index[str(value).casefold()]
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_card(card: Card) -> str:
    return hashlib.sha256(_canonical_json(card.to_dict()).encode("utf-8")).hexdigest()


def open_database(path: str | Path) -> DatabaseAdapter:
    path_text = str(path)
    postgres = path_text.startswith(("postgresql://", "postgres://"))
    if postgres:
        try:
            import psycopg
        except ImportError as exc:
            raise ScrapeError(
                "PostgreSQL support needs psycopg 3. Install requirements-postgres.txt."
            ) from exc
        try:
            connection = psycopg.connect(path_text)
        except psycopg.Error as exc:
            raise ScrapeError(f"Could not connect to PostgreSQL: {exc}") from exc
        database = DatabaseAdapter(connection, postgres=True)
    else:
        sqlite_path = Path(path_text)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(sqlite_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        database = DatabaseAdapter(connection, postgres=False)
    try:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS cards (
                card_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )
        database.execute("CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)")
    except Exception as exc:
        database.close()
        if postgres:
            raise ScrapeError(f"PostgreSQL schema setup failed: {exc}") from exc
        raise
    return database


def sync_cards(
    connection: DatabaseAdapter,
    raw_records: Iterable[Mapping[str, Any]],
    banlist_index: Mapping[str, Mapping[str, Any]],
    *,
    force: bool = False,
) -> tuple[int, int, int]:
    inserted = updated = unchanged = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection:
        for raw in raw_records:
            entry = match_banlist_entry(raw, banlist_index)
            card = normalize_mdm_card(raw, entry)
            payload = card.to_dict()
            digest = _hash_card(card)
            existing = connection.execute(
                "SELECT content_hash FROM cards WHERE card_id = ?", (card.card_id,)
            ).fetchone()
            if existing is not None and existing[0] == digest and not force:
                unchanged += 1
                continue
            raw_json = _canonical_json(raw)
            payload_json = _canonical_json(payload)
            if existing is None:
                connection.execute(
                    """INSERT INTO cards
                    (card_id, name, content_hash, payload_json, raw_json, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (card.card_id, card.name, digest, payload_json, raw_json, now, now),
                )
                inserted += 1
            else:
                connection.execute(
                    """UPDATE cards SET name=?, content_hash=?, payload_json=?, raw_json=?, last_seen=?
                    WHERE card_id=?""",
                    (card.name, digest, payload_json, raw_json, now, card.card_id),
                )
                updated += 1
    return inserted, updated, unchanged


def export_json(connection: DatabaseAdapter, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute("SELECT payload_json FROM cards ORDER BY LOWER(name)")
    payload = [json.loads(row[0]) for row in rows]
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(destination)
    return len(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync", help="sync public MDM card data to SQLite and JSON")
    sync.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    sync.add_argument("--db", default="data/cards.db", help="SQLite path or PostgreSQL DSN")
    sync.add_argument("--json", type=Path, default=Path("data/cards.json"))
    sync.add_argument("--banlist-json", type=Path)
    sync.add_argument("--limit-per-page", type=int, default=1000)
    sync.add_argument("--limit", type=int, help="maximum total number of cards to fetch")
    sync.add_argument("--force", action="store_true", help="rewrite matching rows instead of skipping unchanged cards")
    sync.add_argument("--max-pages", type=int, default=100)
    sync.add_argument("--interval", type=float, default=0.25, help="seconds between API pages")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        banlist_index = load_banlist(args.banlist_json)
        raw_records = fetch_all_cards(
            args.endpoint,
            limit_per_page=args.limit_per_page,
            max_pages=args.max_pages,
            interval=args.interval,
            total_limit=args.limit,
        )
        database_path = args.db
        postgres_requested = str(args.db).startswith(("postgresql://", "postgres://"))
        try:
            connection = open_database(database_path)
        except Exception as exc:
            if not postgres_requested:
                raise
            database_path = "data/cards.db"
            print(
                f"PostgreSQL unavailable ({exc}); falling back to local SQLite database {database_path}.",
                file=sys.stderr,
            )
            connection = open_database(database_path)
        try:
            try:
                inserted, updated, unchanged = sync_cards(
                    connection, raw_records, banlist_index, force=args.force
                )
                total = export_json(connection, args.json)
            except Exception as exc:
                if not postgres_requested or database_path != args.db:
                    raise
                connection.close()
                database_path = "data/cards.db"
                print(
                    f"PostgreSQL sync failed ({exc}); falling back to local SQLite database {database_path}.",
                    file=sys.stderr,
                )
                connection = open_database(database_path)
                inserted, updated, unchanged = sync_cards(
                    connection, raw_records, banlist_index, force=args.force
                )
                total = export_json(connection, args.json)
        finally:
            connection.close()
        print(
            f"MDM sync complete: {total} cards; {inserted} inserted, {updated} updated, "
            f"{unchanged} unchanged. JSON: {args.json}; database: {database_path}"
        )
        if not args.banlist_json:
            print("Banlist status is unverified unless present in the card API; see README.md.")
        return 0
    except (ScrapeError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"mdm_scraper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

