"""Fetch and normalize Master Duel Meta Forbidden/Limited List changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mdm_scraper import ScrapeError, _extract_records, request_json


DEFAULT_ENDPOINT = "https://www.masterduelmeta.com/api/v1/banlist-changes"
MAX_COPIES = {"Forbidden": 0, "Limited": 1, "Semi-Limited": 2, "Unlimited": 3}


def normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    aliases = {
        "forbidden": "Forbidden",
        "banned": "Forbidden",
        "0": "Forbidden",
        "limited": "Limited",
        "limited 1": "Limited",
        "1": "Limited",
        "semi-limited": "Semi-Limited",
        "semi limited": "Semi-Limited",
        "limited 2": "Semi-Limited",
        "2": "Semi-Limited",
        "unlimited": "Unlimited",
        "limited 3": "Unlimited",
        "3": "Unlimited",
    }
    return aliases.get(text)


def _flatten_changes(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    flattened: list[Mapping[str, Any]] = []
    for row in rows:
        nested = row.get("changes")
        if isinstance(nested, list):
            for change in nested:
                if isinstance(change, Mapping):
                    merged = dict(row)
                    merged.pop("changes", None)
                    merged.update(change)
                    flattened.append(merged)
        else:
            flattened.append(row)
    return flattened


def _card_info(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    nested = row.get("card") if isinstance(row.get("card"), Mapping) else {}
    card_id = next(
        (
            str(value)
            for value in (
                row.get("gameId"),
                row.get("card_id"),
                row.get("konamiID"),
                nested.get("gameId"),
                nested.get("card_id"),
                nested.get("konamiID"),
                nested.get("_id"),
            )
            if value not in (None, "")
        ),
        None,
    )
    name = next(
        (
            str(value)
            for value in (row.get("name"), row.get("cardName"), nested.get("name"))
            if value not in (None, "")
        ),
        None,
    )
    return card_id, name


def _row_status(row: Mapping[str, Any]) -> str | None:
    for key in ("ban_status", "banStatus", "newStatus", "new_status", "toStatus", "to", "status", "limit"):
        status = normalize_status(row.get(key))
        if status:
            return status
    return None


def fetch_banlist(
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    limit_per_page: int = 1000,
    max_pages: int = 30,
    interval: float = 0.25,
    total_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch the change feed and retain each card's most recent known status."""
    if limit_per_page < 1 or max_pages < 1:
        raise ValueError("Pagination limits must be positive.")
    latest: dict[str, tuple[str, int, dict[str, Any]]] = {}
    seen_event_ids: set[str] = set()
    processed = 0
    no_new_event_pages = 0
    for page_index in range(max_pages):
        offset = page_index * limit_per_page
        payload = request_json(
            endpoint,
            {"limit": limit_per_page, "page": page_index + 1, "offset": offset, "skip": offset},
        )
        rows = _flatten_changes(_extract_records(payload))
        if not rows:
            break
        new_on_page = 0
        for row_index, row in enumerate(rows):
            stable_row = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            event_id = str(row.get("_id") or row.get("id") or hashlib.sha256(stable_row.encode("utf-8")).hexdigest())
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            new_on_page += 1
            processed += 1
            card_id, name = _card_info(row)
            status = _row_status(row)
            if not (card_id or name) or not status:
                continue
            key = (card_id or name or "").casefold()
            effective = str(
                row.get("effectiveDate")
                or row.get("effective_date")
                or row.get("date")
                or row.get("createdAt")
                or row.get("created")
                or ""
            )
            record = {
                "card_id": card_id,
                "name": name,
                "ban_status": status,
                "effective_date": effective or None,
            }
            previous = latest.get(key)
            if previous is None or (effective, processed) >= (previous[0], previous[1]):
                latest[key] = (effective, processed, record)
            if total_limit is not None and len(latest) >= total_limit:
                break
        if total_limit is not None and len(latest) >= total_limit:
            break
        if new_on_page == 0:
            no_new_event_pages += 1
            if no_new_event_pages >= 2:
                print("Warning: MDM banlist pagination repeated change IDs; stopping the scan.", file=sys.stderr)
                break
        else:
            no_new_event_pages = 0
        if len(rows) < limit_per_page:
            break
        if interval:
            time.sleep(max(0.0, interval))
    if not latest:
        sample_payload_shape = "unknown"
        if isinstance(payload, Mapping):
            sample_payload_shape = f"object keys={list(payload)[:20]}"
        elif isinstance(payload, list):
            sample_payload_shape = "array"
        sample_row_keys = list(rows[0])[:30] if rows else []
        sample_row_fields = {
            key: rows[0].get(key)
            for key in ("card", "to", "date", "announced")
            if rows and key in rows[0]
        }
        raise ScrapeError(
            "No banlist card statuses were recognized. Check the endpoint response or provide "
            f"a reviewed JSON snapshot with --input-json. Response: {sample_payload_shape}; "
            f"first record keys: {sample_row_keys}; fields: {sample_row_fields}."
        )
    return [item[2] for item in sorted(latest.values(), key=lambda item: (item[2]["name"] or "").casefold())]


def save_snapshot(entries: list[dict[str, Any]], output: Path, endpoint: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "format": "Master Duel",
        "source": endpoint,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cards": entries,
    }
    temp_path = output.with_suffix(output.suffix + ".tmp")
    temp_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(output)


def filter_legal_cards(cards: list[Mapping[str, Any]], banlist: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return card records that are not forbidden under the supplied format snapshot."""
    by_key: dict[str, Mapping[str, Any]] = {}
    for row in banlist:
        status = normalize_status(row.get("ban_status") or row.get("status"))
        if status:
            for key in ("card_id", "gameId", "konamiID", "name"):
                value = row.get(key)
                if value not in (None, ""):
                    by_key[str(value).casefold()] = {"ban_status": status}
    legal: list[Mapping[str, Any]] = []
    for card in cards:
        keys = [str(card[key]).casefold() for key in ("card_id", "gameId", "konamiID", "name") if card.get(key)]
        status = next((by_key[key]["ban_status"] for key in keys if key in by_key), "Unlimited")
        if status != "Forbidden":
            legal.append(card)
    return legal


def validate_deck(card_names: list[str], banlist: list[Mapping[str, Any]]) -> list[str]:
    """Report per-card copy-limit violations for a simple Master Duel deck list."""
    limits: dict[str, int] = {}
    for row in banlist:
        name = row.get("name")
        status = normalize_status(row.get("ban_status") or row.get("status"))
        if name and status:
            limits[str(name).casefold()] = MAX_COPIES[status]
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for name in card_names:
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
        display[key] = name
    return [
        f"{display[key]}: {count} copies, maximum {limits[key]}"
        for key, count in counts.items()
        if key in limits and count > limits[key]
    ]


def _load_snapshot(path: Path) -> list[Mapping[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, Mapping):
        value = value.get("cards", value.get("data", []))
    if not isinstance(value, list):
        raise ScrapeError("Banlist input JSON must contain a card array.")
    return [row for row in value if isinstance(row, Mapping)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output", type=Path, default=Path("data/banlist.json"))
    parser.add_argument("--input-json", type=Path, help="normalize an existing MDM/export JSON snapshot")
    parser.add_argument("--limit", type=int, help="maximum number of restricted cards to retain")
    parser.add_argument("--limit-per-page", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        entries = (
            [dict(row) for row in _load_snapshot(args.input_json)]
            if args.input_json
            else fetch_banlist(
                args.endpoint,
                limit_per_page=args.limit_per_page,
                total_limit=args.limit,
            )
        )
        save_snapshot(entries, args.output, args.endpoint)
        print(f"Banlist snapshot saved: {len(entries)} entries at {args.output}")
        return 0
    except (ScrapeError, OSError, ValueError) as exc:
        print(f"mdm_banlist: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

