"""FastAPI dashboard for local card search, deck editing, route benchmarking, and vision control.

Run from the repository root with ``python web_app.py`` after installing requirements-web.txt.
The service binds to loopback by default and performs no game input or process-memory access.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from pydantic import BaseModel, Field
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.gzip import GZipMiddleware
except ImportError as exc:  # A clear install message instead of an opaque import failure.
    raise RuntimeError(
        "The dashboard needs FastAPI, Jinja2, and Uvicorn. Install them with: "
        "python -m pip install -r requirements-web.txt"
    ) from exc

from generate_combo import parse_hand
from database_manager import DatabaseManager
from engine.disruptions import disruption_profile
from mdm_banlist import MAX_COPIES, normalize_status
from mcts_search import (
    Action,
    DeckModel,
    apply_action,
    evaluate_board,
    legal_actions,
    load_deck,
    make_initial_state,
    run_mcts,
)
from vision_engine import VisionEngine, VisionWorker, inspect_master_duel_process


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EXAMPLES_DIR = ROOT / "examples"
CARD_JSON = DATA_DIR / "cards.json"
CARD_DB = DATA_DIR / "cards.db"
BANLIST_JSON = DATA_DIR / "banlist.json"
CACHE_DB = DATA_DIR / "combo_cache.db"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DEFAULT_DECK_FILE = "vs_k9_deck.json"
AI_ROUTE_ITERATIONS = 15000
FRONTEND_VERSION = "interactive-evaluator-20261110"
SWAP_EVALUATION_ITERATIONS = 1800


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize_card(raw: dict[str, Any]) -> dict[str, Any]:
    markers = raw.get("link_markers") or []
    if isinstance(markers, dict):
        markers = [key for key, enabled in markers.items() if enabled]
    level = raw.get("level_rank_link")
    return {
        "card_id": _text(raw.get("card_id") or raw.get("gameId") or raw.get("konamiID")),
        "name": _text(raw.get("name")),
        "type": _text(raw.get("type")) or "Monster",
        "frame_type": _text(raw.get("frame_type") or raw.get("frameType")),
        "attribute": _text(raw.get("attribute")),
        "race_type": _text(raw.get("race_type") or raw.get("race")),
        "level_rank_link": level,
        "atk": raw.get("atk"),
        "def": raw.get("def"),
        "scale": raw.get("scale"),
        "link_markers": markers if isinstance(markers, list) else [],
        "archetype": _text(raw.get("archetype")),
        "description": _text(raw.get("description")),
        "ban_status": _text(raw.get("ban_status")) or "Unknown",
        "rarity": _text(raw.get("rarity")),
        "mdm_url": _text(raw.get("mdm_url")),
    }


@lru_cache(maxsize=1)
def load_card_catalog() -> tuple[dict[str, Any], ...]:
    """Read and normalize the entire MDM card file once per process."""
    if CARD_JSON.exists():
        raw_cards = json.loads(CARD_JSON.read_text(encoding="utf-8"))
    elif CARD_DB.exists():
        connection = sqlite3.connect(f"file:{CARD_DB.as_posix()}?mode=ro", uri=True)
        try:
            raw_cards = [json.loads(row[0]) for row in connection.execute("SELECT raw_json FROM cards")]
        finally:
            connection.close()
    else:
        raise RuntimeError(f"Card database not found. Expected {CARD_JSON} or {CARD_DB}.")
    normalized = [_normalize_card(row) for row in raw_cards if _text(row.get("name"))]
    normalized.sort(key=lambda row: row["name"].casefold())
    return tuple(normalized)


@lru_cache(maxsize=16)
def load_deck_model(deck_file: str, modified_ns: int) -> DeckModel:
    del modified_ns
    return load_deck(EXAMPLES_DIR / deck_file, BANLIST_JSON if BANLIST_JSON.exists() else None)


def _safe_deck_file(value: str) -> Path:
    candidate_name = Path(value).name
    if candidate_name != value or not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", candidate_name):
        raise HTTPException(status_code=400, detail="Invalid deck filename.")
    candidate = (EXAMPLES_DIR / candidate_name).resolve()
    if not candidate.is_relative_to(EXAMPLES_DIR.resolve()) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Deck file was not found.")
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Deck JSON could not be read.") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("main_deck"), list):
        raise HTTPException(status_code=404, detail="The selected JSON is not a deck list.")
    return candidate


def _deck_summary(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        main = sum(int(entry.get("count", 0)) for entry in raw.get("main_deck", []))
        extra = sum(int(entry.get("count", 0)) for entry in raw.get("extra_deck", []))
        return {"file": path.name, "name": raw.get("name", path.stem), "main_count": main, "extra_count": extra}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"file": path.name, "name": path.stem, "main_count": 0, "extra_count": 0}


def _available_decks() -> list[dict[str, Any]]:
    available = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        summary = _deck_summary(path)
        if summary["main_count"] or summary["extra_count"]:
            available.append(summary)
    return available


def _deck_main_names(deck_file: str) -> list[str]:
    raw = json.loads(_safe_deck_file(deck_file).read_text(encoding="utf-8"))
    return list(dict.fromkeys(
        _text(entry.get("name")) for entry in raw.get("main_deck", [])
        if isinstance(entry, dict) and _text(entry.get("name"))
    ))


def _banlist_limits() -> dict[str, int]:
    if not BANLIST_JSON.exists():
        return {}
    try:
        raw = json.loads(BANLIST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = raw.get("cards", raw) if isinstance(raw, dict) else raw
    limits: dict[str, int] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("name"):
                status = normalize_status(row.get("ban_status") or row.get("status"))
                if status:
                    limits[str(row["name"]).casefold()] = MAX_COPIES[status]
    return limits


class DeckCardCount(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=1, le=60)


class SaveDeckRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    main_deck: list[DeckCardCount] = Field(max_length=60)
    extra_deck: list[DeckCardCount] = Field(max_length=15)


class BenchmarkRequest(BaseModel):
    deck_file: str = DEFAULT_DECK_FILE
    hand: list[str] = Field(min_length=1, max_length=5)
    route_steps: list[str] = Field(min_length=1, max_length=40)
    mode: str = "going_1st"
    baseline_iterations: int = Field(default=400, ge=50, le=2000)


class GenerateRouteRequest(BaseModel):
    deck_file: str = DEFAULT_DECK_FILE
    hand: list[str] = Field(min_length=5, max_length=5)
    mode: Literal["going_1st", "going_2nd"] = "going_1st"


class EvaluateUserSwapRequest(BaseModel):
    starting_hand: list[str] = Field(min_length=5, max_length=5)
    original_route: list[str | dict[str, Any]] = Field(min_length=1, max_length=40)
    swapped_step_index: int = Field(ge=1, le=40)
    user_alternative_action: str = Field(min_length=2, max_length=500)
    deck_file: str = DEFAULT_DECK_FILE
    mode: Literal["going_1st", "going_2nd"] = "going_1st"


class VisionStartRequest(BaseModel):
    monitor: int = Field(default=1, ge=1, le=8)


def _is_extra_card(card: dict[str, Any]) -> bool:
    frame = _text(card.get("frame_type")).casefold()
    return card.get("type") == "Monster" and any(
        kind in frame for kind in ("fusion", "synchro", "xyz", "link")
    )


def _definition_for(card: dict[str, Any]) -> dict[str, Any]:
    frame = _text(card.get("frame_type"))
    kind = _text(card.get("type")) or "Monster"
    value = card.get("level_rank_link")
    definition: dict[str, Any] = {
        "name": card["name"],
        "card_type": kind,
        "attribute": card.get("attribute") or None,
        "race": card.get("race_type") or None,
        "atk": int(card.get("atk") or 0),
        "effect_monster": kind == "Monster" and frame != "Normal",
        "normal_summon": kind == "Monster" and not _is_extra_card(card) and value is not None and int(value) <= 4,
        "operators": [],
        "archetypes": [part.strip() for part in _text(card.get("archetype")).split(",") if part.strip()],
        "effect_text": _text(card.get("description")),
        "ban_status": _text(card.get("ban_status")) or "Unknown",
    }
    if kind == "Monster" and value is not None:
        number = int(value)
        if "link" in frame.casefold():
            definition.update(link_rating=number, link_min_materials=1 if number <= 1 else 2,
                              link_requirement="effect_monster")
        elif "xyz" in frame.casefold():
            definition.update(rank=number, xyz_min_materials=2, xyz_material_level=number)
        else:
            definition["level"] = number
    return definition


def _save_deck(payload: SaveDeckRequest) -> dict[str, Any]:
    cards = {card["name"]: card for card in load_card_catalog()}
    limits = _banlist_limits()
    main_names: list[str] = []
    extra_names: list[str] = []
    used = Counter()
    for entries, destination, expect_extra in (
        (payload.main_deck, main_names, False),
        (payload.extra_deck, extra_names, True),
    ):
        for entry in entries:
            card = cards.get(entry.name)
            if card is None:
                raise HTTPException(status_code=422, detail=f"Unknown card: {entry.name}")
            if _is_extra_card(card) != expect_extra:
                place = "Extra Deck" if expect_extra else "Main Deck"
                raise HTTPException(status_code=422, detail=f"{entry.name} does not belong in the {place}.")
            status = normalize_status(card.get("ban_status"))
            limit = limits.get(entry.name.casefold(), MAX_COPIES.get(status, 3))
            if used[entry.name.casefold()] + entry.count > limit:
                raise HTTPException(status_code=422, detail=f"{entry.name} exceeds its {status or 'default'} copy limit.")
            used[entry.name.casefold()] += entry.count
            destination.extend([entry.name] * entry.count)
    if len(main_names) > 60 or len(extra_names) > 15:
        raise HTTPException(status_code=422, detail="Main Deck is capped at 60 and Extra Deck at 15.")

    def entries(names: list[str]) -> list[dict[str, Any]]:
        counts = Counter(names)
        return [{"name": name, "count": count} for name, count in counts.items()]

    names = list(dict.fromkeys(main_names + extra_names))
    slug = re.sub(r"[^a-z0-9_-]+", "_", payload.name.casefold()).strip("_-")[:48] or "deck"
    filename = f"custom_{slug}.json"
    target = EXAMPLES_DIR / filename
    raw = {
        "name": payload.name.strip(),
        "format": "Custom local deck",
        "main_deck": entries(main_names),
        "extra_deck": entries(extra_names),
        "card_definitions": [_definition_for(cards[name]) for name in names],
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return _deck_summary(target)


def _normalize_step(value: str) -> str:
    text = value.strip().casefold().replace("’", "'")
    text = re.sub(r"^\s*(?:\*\*)?\s*(?:bước|step)?\s*\d+\s*[:.)-]*\s*", "", text)
    return re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE).strip()


def _kind_hint(text: str) -> str | None:
    if "normal summon" in text or "trieu hoi thuong" in text:
        return "normal_summon"
    if "link summon" in text:
        return "link_summon"
    if "xyz summon" in text or "overlay" in text:
        return "xyz_summon"
    return None


def _route_action(step: str, actions: list[Action]) -> Action | None:
    normalized = _normalize_step(step)
    if not normalized:
        return None
    hint = _kind_hint(normalized)
    candidates = [action for action in actions if hint is None or action.kind == hint or
                  (hint == "xyz_summon" and action.kind in {"nash_rank_up", "k9_forced_release"})]
    if not candidates:
        return None
    best: Action | None = None
    best_score = -1.0
    for action in candidates:
        description = _normalize_step(action.description)
        ratio = difflib.SequenceMatcher(None, normalized, description, autojunk=False).ratio()
        tokens = set(normalized.split())
        action_tokens = set(description.split())
        overlap = len(tokens & action_tokens) / max(1, len(tokens))
        score = 0.55 * ratio + 0.45 * overlap
        if normalized in description or description in normalized:
            score += 0.5
        if hint and action.kind == hint:
            score += 0.2
        if score > best_score:
            best_score, best = score, action
    return best if best_score >= 0.33 else None


def _step_action_text(step: str | dict[str, Any]) -> str:
    if isinstance(step, dict):
        return _text(step.get("action"))
    return _text(step)


def _new_route_state(deck: DeckModel, hand: list[str], mode: str):
    return make_initial_state(
        deck,
        hand,
        opponent_hand_effect_activated=mode == "going_2nd",
        opponent_field_count=3 if mode == "going_2nd" else 0,
    )


def _match_route_action(step: str, actions: list[Action]) -> Action | None:
    exact = next((action for action in actions if str(action).strip() == step.strip()), None)
    return exact or _route_action(step, actions)


def _role_and_rationale(action: Action) -> tuple[str, str]:
    kind = action.kind
    if kind in {"kaiju_tribute", "typhon_summon"} or (
        kind == "xyz_summon" and "TY-PHON" in action.card
    ):
        return "Board Breaker", "Dọn một mối đe dọa trước khi dùng phần tài nguyên còn lại để dựng sân."
    if kind == "k9_jacks_destroy":
        return "Disruption", "Dùng hiệu ứng phá hủy để giảm số quái đối phương còn kiểm soát."
    profile = disruption_profile(action.card)
    if profile and profile.get("zone") == "field" and kind in {"xyz_summon", "link_summon", "typhon_summon"}:
        return "Disruption", f"Triệu hồi {action.card} để thiết lập tương tác: {profile['effect']}"
    if action.costs and ("cost" in action.description.casefold() or "send" in action.description.casefold()):
        return "Bait", "Đổi một tài nguyên lấy hiệu ứng mở đường, đồng thời có thể kéo negate đối thủ ra sớm."
    if kind in {
        "normal_summon", "k9_jokul_pair", "a_case_activate", "wanted_search",
        "diabellstar_self_summon", "vs_stake", "oss_activate",
    }:
        role = "Starter"
    else:
        role = "Extender"

    rationales = {
        "normal_summon": "Đặt starter lên sân để mở các trigger tìm kiếm hoặc triệu hồi của engine.",
        "k9_jokul_pair": "Triệu hồi cặp K9 Level 5 để mở Xyz Rank 5 mà vẫn giữ các lá thuộc tính trên tay.",
        "k9_lantern_revive": "Đưa Lantern và một K9 từ Mộ lên sân để nối thêm vật liệu Xyz.",
        "vs_stake": "Giữ lá được lộ trên tay và gọi Vanquish Soul cùng thuộc tính để tiếp tục engine.",
        "a_case_activate": "Tìm K9 từ Deck để tăng số lựa chọn mở rộng trước khi khóa tài nguyên.",
        "vs_search_monster": "Tìm quái Vanquish Soul cần thiết để giữ thuộc tính và chuẩn bị hiệu ứng tương tác.",
        "vs_search_spelltrap": "Tìm Spell/Trap Vanquish Soul để bổ sung follow-up hoặc tương tác lượt sau.",
        "k9_izuna_send": "Đưa K9 phù hợp xuống Mộ để bật hồi sinh hoặc chuẩn bị các hiệu ứng K9 tiếp theo.",
        "k9_ripper_search": "Tách vật liệu để tìm thêm K9; dùng tài nguyên Xyz đổi lấy follow-up.",
        "xyz_summon": "Chuyển các quái vật thành Xyz để tăng sức ép và mở hiệu ứng theo Rank.",
        "link_summon": "Chuyển vật liệu thành Link để mở vùng triển khai hoặc dựng tương tác trên sân.",
        "vs_hollie_sue_deck_summon": "Lộ FIRE và DARK nhưng giữ chúng trên tay để duy trì tương tác Vanquish Soul.",
        "vs_tag_out": "Đổi loại quái Vanquish Soul trên sân để bật hiệu ứng và giữ nhịp tài nguyên.",
        "typhon_summon": "Dùng TY-PHON để trả một mối đe dọa lớn về tay và làm chậm sân đối thủ.",
    }
    return role, rationales.get(
        kind,
        f"Dùng hiệu ứng của {action.card} để mở rộng tài nguyên và tiến gần hơn tới End-Board mục tiêu.",
    )


def _annotate_route(
    deck: DeckModel,
    hand: list[str],
    steps: list[str | dict[str, Any]],
    *,
    mode: str,
) -> list[dict[str, Any]]:
    state = _new_route_state(deck, hand, mode)
    output: list[dict[str, Any]] = []
    for step_num, raw_step in enumerate(steps, start=1):
        text = _step_action_text(raw_step)
        available = legal_actions(state, deck.cards)
        action = _match_route_action(text, available) if text else None
        if action is None:
            output.append({
                "step_num": step_num,
                "action": text,
                "role": "Extender",
                "rationale": "Bước này không khớp operator đang mô hình hóa; hãy kiểm tra lại với OCG Core.",
                "alternatives": [],
            })
            continue
        role, rationale = _role_and_rationale(action)
        output.append({
            "step_num": step_num,
            "action": str(action),
            "role": role,
            "rationale": rationale,
            "alternatives": [str(candidate) for candidate in available if candidate != action][:60],
        })
        state = apply_action(state, action, deck.cards)
    return output


def _replay_route(
    deck: DeckModel,
    hand: list[str],
    steps: list[str | dict[str, Any]],
    *,
    mode: str,
) -> tuple[Any, list[str]]:
    state = _new_route_state(deck, hand, mode)
    applied: list[str] = []
    for step_num, raw_step in enumerate(steps, start=1):
        text = _step_action_text(raw_step)
        actions = legal_actions(state, deck.cards)
        action = _match_route_action(text, actions) if text else None
        if action is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "step": step_num,
                    "message": f"Route step is not a modeled legal action: {text}",
                    "available_actions": [str(candidate) for candidate in actions[:20]],
                },
            )
        applied.append(str(action))
        state = apply_action(state, action, deck.cards)
    return state, applied


def _endboard_disruption_breakdown(state: Any, deck: DeckModel) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for unit in state.field:
        if unit.temporary:
            continue
        spec = deck.cards[unit.name]
        profile = disruption_profile(unit.name)
        if profile and profile.get("zone") == "field":
            key = profile.get("activation_key")
            if key and f"{unit.name}::{key}" in state.effects_used:
                continue
            if spec.interruption_requirements:
                hand_attributes = {deck.cards[name].attribute for name in state.hand}
                if not any(all(attribute in hand_attributes for attribute in group)
                           for group in spec.interruption_requirements):
                    continue
            counts[(unit.name, "Sân", str(profile["type"]), str(profile["effect"]))] += int(profile.get("count", 1))
        elif spec.interruptions:
            hand_attributes = {deck.cards[name].attribute for name in state.hand}
            if spec.interruption_requirements and not any(
                all(attribute in hand_attributes for attribute in group)
                for group in spec.interruption_requirements
            ):
                continue
            counts[(unit.name, "Sân", "Tương tác", "Hiệu ứng tương tác được khai báo trong card model.")] += spec.interruptions
    for spell in state.spell_trap_zone:
        profile = disruption_profile(spell.name)
        if profile:
            counts[(spell.name, "Spell/Trap", str(profile["type"]), str(profile["effect"]))] += int(profile.get("count", 1))
    for name in state.hand:
        profile = disruption_profile(name)
        if profile and profile.get("zone") == "hand":
            counts[(name, "Tay", str(profile["type"]), str(profile["effect"]))] += int(profile.get("count", 1))
    return [
        {"card": card, "zone": zone, "type": kind, "effect": effect, "count": count}
        for (card, zone, kind, effect), count in sorted(counts.items())
    ]


def _strategy_label(mode: str, components: dict[str, Any]) -> str:
    if mode != "going_2nd":
        return "[🛡️ STRATEGY: SET UP DISRUPTIONS]"
    if float(components.get("otk_damage", 0)) >= 8000:
        return "[🔥 STRATEGY: OTK ROUTE]"
    return "[🛡️ BOARD BREAK & RE-CONTROL]"


def evaluate_human_route(request: BenchmarkRequest) -> dict[str, Any]:
    if request.mode not in {"going_1st", "going_2nd"}:
        raise HTTPException(status_code=422, detail="Mode must be going_1st or going_2nd.")
    path = _safe_deck_file(request.deck_file)
    deck = load_deck_model(path.name, path.stat().st_mtime_ns)
    try:
        hand = parse_hand(deck, "|".join(request.hand))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state = _new_route_state(deck, hand, request.mode)
    applied: list[str] = []
    for index, raw_step in enumerate(request.route_steps, start=1):
        step = raw_step.strip()
        if not step:
            continue
        candidates = legal_actions(state, deck.cards)
        action = _route_action(step, candidates)
        if action is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "step": index,
                    "message": f"Could not match this step to a modeled legal action: {step}",
                    "available_actions": [str(candidate) for candidate in candidates[:12]],
                },
            )
        state = apply_action(state, action, deck.cards)
        applied.append(str(action))
    if not applied:
        raise HTTPException(status_code=422, detail="Enter at least one actionable route step.")

    score, components = evaluate_board(state, deck.cards, mode=request.mode)
    baseline = run_mcts(
        deck,
        hand,
        iterations=request.baseline_iterations,
        rollout_depth=min(24, max(10, len(applied) + 5)),
        seed=17,
        mode=request.mode,
    )
    is_gold = score >= baseline.score
    end_board = [unit.name for unit in state.field]
    cache_result = DatabaseManager(CACHE_DB).evaluate_human_route(
        applied,
        hand=hand,
        end_board=end_board,
        score=score,
        mcts_score=baseline.score,
        deck_name=deck.name,
        mode=request.mode,
        metadata={"source": "human_benchmark", "raw_steps": request.route_steps},
    )
    return {
        "hand": hand,
        "steps": _annotate_route(deck, hand, applied, mode=request.mode),
        "end_board": end_board,
        "end_board_disruptions": _endboard_disruption_breakdown(state, deck),
        "score": round(score, 4),
        "baseline_score": round(baseline.score, 4),
        "score_components": components,
        "strategy_label": _strategy_label(request.mode, components),
        "mode": request.mode,
        "is_gold_route": cache_result["is_gold_route"],
        "hand_hash": cache_result["hand_hash"],
        "cache_db": str(CACHE_DB),
    }


def generate_ai_route(request: GenerateRouteRequest) -> dict[str, Any]:
    """Solve a five-card hand, using a compatible saved route before running MCTS."""
    path = _safe_deck_file(request.deck_file)
    deck = load_deck_model(path.name, path.stat().st_mtime_ns)
    try:
        hand = parse_hand(deck, "|".join(request.hand))
        initial_state = _new_route_state(deck, hand, request.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    cache = DatabaseManager(CACHE_DB)
    cached = cache.get_combo(hand, deck_name=deck.name, mode=request.mode)
    if cached:
        cached_route = cached.get("route") or {}
        source = cached_route.get("source")
        if cached["is_gold_route"] or source == "mcts":
            cached_steps = cached_route.get("steps", [])
            try:
                cached_state, _ = _replay_route(deck, hand, cached_steps, mode=request.mode)
            except HTTPException:
                cached_state = None
            if cached_state is not None:
                cached_score, score_components = evaluate_board(cached_state, deck.cards, mode=request.mode)
                return {
                    "hand": hand,
                    "steps": _annotate_route(deck, hand, cached_steps, mode=request.mode),
                    "end_board": [unit.name for unit in cached_state.field],
                    "end_board_disruptions": _endboard_disruption_breakdown(cached_state, deck),
                    "score": round(cached_score, 4),
                    "score_components": score_components,
                    "mode": request.mode,
                    "strategy_label": _strategy_label(request.mode, score_components),
                    "cache_hit": True,
                    "is_gold_route": cached["is_gold_route"],
                    "simulations": cached_route.get("simulations", 0),
                    "expanded_nodes": cached_route.get("expanded_nodes", 0),
                }

    result = run_mcts(
        deck,
        hand,
        iterations=AI_ROUTE_ITERATIONS,
        rollout_depth=24,
        seed=17,
        mode=request.mode,
        initial_state=initial_state,
    )
    steps = [str(action) for action in result.actions]
    end_board = [unit.name for unit in result.final_state.field]
    cache.save_combo(
        hand=hand,
        deck_name=deck.name,
        route_steps=steps,
        end_board=end_board,
        score=result.score,
        is_gold_route=False,
        mode=request.mode,
        metadata={
            "source": "mcts",
            "score_components": result.score_components,
            "simulations": result.simulations,
            "expanded_nodes": result.expanded_nodes,
        },
    )
    return {
        "hand": hand,
        "steps": _annotate_route(deck, hand, steps, mode=request.mode),
        "end_board": end_board,
        "end_board_disruptions": _endboard_disruption_breakdown(result.final_state, deck),
        "score": round(result.score, 4),
        "score_components": result.score_components,
        "mode": request.mode,
        "strategy_label": _strategy_label(request.mode, result.score_components),
        "cache_hit": False,
        "is_gold_route": False,
        "simulations": result.simulations,
        "expanded_nodes": result.expanded_nodes,
    }


def _swap_feedback(
    user_score: float,
    ai_score: float,
    user_components: dict[str, float],
    ai_components: dict[str, float],
) -> tuple[bool, str]:
    if user_score >= ai_score:
        return True, "Nước đi của bạn rất xuất sắc! Phương án này tối ưu hơn / tiết kiệm tài nguyên hơn AI."
    losses: list[str] = []
    for key, label in (
        ("interruptions", "lượt ngắt ở End-Board"),
        ("board_breaks", "lượt phá board được mô hình hóa"),
        ("card_advantage", "lợi thế bài trên tay/sân"),
        ("total_atk", "sát thương tiềm năng trên sân"),
    ):
        difference = float(ai_components.get(key, 0)) - float(user_components.get(key, 0))
        if difference > 0.01:
            amount = int(difference) if difference.is_integer() else round(difference, 1)
            losses.append(f"phương án mất {amount} {label}")
    resource_loss = float(user_components.get("resources_used", 0)) - float(ai_components.get("resources_used", 0))
    if resource_loss > 0.01:
        amount = int(resource_loss) if resource_loss.is_integer() else round(resource_loss, 1)
        losses.append(f"tiêu tốn thêm {amount} tài nguyên Deck/Extra Deck")
    reason = "; ".join(losses) if losses else "điểm End-Board tổng thể thấp hơn theo trọng số của chế độ đang chọn"
    return False, f"Phương án này đang thấp hơn AI ({reason})."


def evaluate_user_swap(request: EvaluateUserSwapRequest) -> dict[str, Any]:
    """Replace one route action, optimize the remaining line, compare and learn Gold routes."""
    if request.swapped_step_index > len(request.original_route):
        raise HTTPException(status_code=422, detail="swapped_step_index is outside original_route.")
    path = _safe_deck_file(request.deck_file)
    deck = load_deck_model(path.name, path.stat().st_mtime_ns)
    try:
        hand = parse_hand(deck, "|".join(request.starting_hand))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    original_state, original_actions = _replay_route(
        deck, hand, request.original_route, mode=request.mode
    )
    ai_score, ai_components = evaluate_board(original_state, deck.cards, mode=request.mode)

    prefix = request.original_route[: request.swapped_step_index - 1]
    prefix_state, prefix_actions = _replay_route(deck, hand, prefix, mode=request.mode)
    legal = legal_actions(prefix_state, deck.cards)
    selected = _match_route_action(request.user_alternative_action, legal)
    if selected is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Nước thay thế chưa khớp với action hợp lệ tại bước này.",
                "available_actions": [str(action) for action in legal[:30]],
            },
        )

    swapped_state = apply_action(prefix_state, selected, deck.cards)
    continuation = run_mcts(
        deck,
        hand,
        iterations=SWAP_EVALUATION_ITERATIONS,
        rollout_depth=24,
        seed=29,
        mode=request.mode,
        initial_state=swapped_state,
    )
    user_state = continuation.final_state
    user_score, user_components = evaluate_board(user_state, deck.cards, mode=request.mode)
    user_actions = prefix_actions + [str(selected)] + [str(action) for action in continuation.actions]
    is_superior, comment = _swap_feedback(user_score, ai_score, user_components, ai_components)
    end_board = [unit.name for unit in user_state.field]
    gold_route_saved = False
    hand_hash_value = None
    if is_superior:
        hand_hash_value = DatabaseManager(CACHE_DB).save_combo(
            hand=hand,
            deck_name=deck.name,
            route_steps=user_actions,
            end_board=end_board,
            score=user_score,
            is_gold_route=True,
            mode=request.mode,
            metadata={
                "source": "interactive_user_swap",
                "score_components": user_components,
                "baseline_score": ai_score,
                "swapped_step_index": request.swapped_step_index,
                "user_alternative_action": str(selected),
                "iterations": continuation.simulations,
            },
        )
        gold_route_saved = True
    route = {
        "hand": hand,
        "steps": _annotate_route(deck, hand, user_actions, mode=request.mode),
        "end_board": end_board,
        "end_board_disruptions": _endboard_disruption_breakdown(user_state, deck),
        "score": round(user_score, 4),
        "score_components": user_components,
        "strategy_label": _strategy_label(request.mode, user_components),
        "mode": request.mode,
        "simulations": continuation.simulations,
        "expanded_nodes": continuation.expanded_nodes,
        "cache_hit": False,
        "is_gold_route": is_superior,
    }
    return {
        "is_superior": is_superior,
        "comment": comment,
        "V_user": round(user_score, 4),
        "V_ai": round(ai_score, 4),
        "score_delta": round(user_score - ai_score, 4),
        "user_score_components": user_components,
        "ai_score_components": ai_components,
        "gold_route_saved": gold_route_saved,
        "hand_hash": hand_hash_value,
        "steps": route["steps"],
        "route": route,
        "original_route_steps": original_actions,
    }


app = FastAPI(title="Yu-Gi-Oh! AI Duel Assistant", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1200)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
vision_engine = VisionEngine()
vision_worker = VisionWorker(vision_engine)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    try:
        count = len(await run_in_threadpool(load_card_catalog))
    except Exception as exc:
        count = 0
        catalog_error = str(exc)
    else:
        catalog_error = ""
    deck_options = await run_in_threadpool(_available_decks)
    initial_deck_file = deck_options[0]["file"] if deck_options else ""
    initial_hand_cards = await run_in_threadpool(_deck_main_names, initial_deck_file) if initial_deck_file else []
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "card_count": count,
            "catalog_error": catalog_error,
            "deck_options": deck_options,
            "initial_deck_file": initial_deck_file,
            "initial_hand_cards": initial_hand_cards,
            "asset_version": FRONTEND_VERSION,
        },
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    try:
        cards = len(await run_in_threadpool(load_card_catalog))
        catalog_ok = True
        error = None
    except Exception as exc:
        cards, catalog_ok, error = 0, False, str(exc)
    vision = await run_in_threadpool(vision_engine.status)
    game = await run_in_threadpool(inspect_master_duel_process)
    return {"status": "ok" if catalog_ok else "degraded", "cards": cards, "catalog_error": error,
            "vision": vision, "game": game}


@app.get("/api/cards")
async def cards() -> JSONResponse:
    catalog = await run_in_threadpool(load_card_catalog)
    compact = list(catalog)
    etag = hashlib.sha1(f"{CARD_JSON.stat().st_mtime_ns if CARD_JSON.exists() else 0}:{len(compact)}".encode()).hexdigest()
    return JSONResponse(compact, headers={"Cache-Control": "public, max-age=900", "ETag": f'"{etag}"'})


@app.get("/api/decks")
async def decks() -> dict[str, Any]:
    return {"decks": await run_in_threadpool(_available_decks)}


@app.get("/api/decks/{deck_file}")
async def get_deck(deck_file: str) -> dict[str, Any]:
    path = _safe_deck_file(deck_file)
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/decks")
async def save_deck(payload: SaveDeckRequest) -> dict[str, Any]:
    return await run_in_threadpool(_save_deck, payload)


@app.post("/api/benchmark-route")
async def benchmark_route(payload: BenchmarkRequest) -> dict[str, Any]:
    return await run_in_threadpool(evaluate_human_route, payload)


@app.post("/api/generate-route")
async def generate_route(payload: GenerateRouteRequest) -> dict[str, Any]:
    return await run_in_threadpool(generate_ai_route, payload)


@app.post("/api/evaluate-user-swap")
async def evaluate_user_route_swap(payload: EvaluateUserSwapRequest) -> dict[str, Any]:
    return await run_in_threadpool(evaluate_user_swap, payload)


@app.get("/api/vision/status")
async def vision_status() -> dict[str, Any]:
    game = await run_in_threadpool(inspect_master_duel_process)
    vision = await run_in_threadpool(vision_engine.status)
    return {
        "worker_running": vision_worker.running,
        "game": game,
        **vision,
    }


@app.post("/api/vision/scan")
async def vision_scan(payload: VisionStartRequest | None = None) -> dict[str, Any]:
    monitor = payload.monitor if payload else 1
    game = await run_in_threadpool(inspect_master_duel_process)
    if not game["running"] or not game["foreground"]:
        raise HTTPException(
            status_code=409,
            detail="Vision scan is locked until Master Duel.exe is running in the foreground.",
        )
    try:
        return await run_in_threadpool(vision_engine.scan_screen, monitor=monitor)
    except (VisionUnavailableError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/vision/worker/start")
async def start_vision_worker(payload: VisionStartRequest | None = None) -> dict[str, Any]:
    if payload:
        vision_worker.monitor = payload.monitor
    started = vision_worker.start()
    return {"running": vision_worker.running, "started": started, "latest": vision_worker.latest()}


@app.post("/api/vision/worker/stop")
async def stop_vision_worker() -> dict[str, Any]:
    vision_worker.stop()
    return {"running": vision_worker.running, "latest": vision_worker.latest()}


@app.get("/api/vision/latest")
async def vision_latest() -> dict[str, Any]:
    return {"running": vision_worker.running, "latest": vision_worker.latest()}


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install Uvicorn with: python -m pip install -r requirements-web.txt") from exc
    host = os.environ.get("YGO_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("YGO_WEB_PORT", "8000"))
    uvicorn.run("web_app:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()

