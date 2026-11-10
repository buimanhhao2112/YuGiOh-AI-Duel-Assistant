"""Heuristic PSCT-to-symbolic-structure parser (not a complete rules interpreter)."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SymbolicAction:
    operator: str
    source_text: str
    scope: str
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class EffectClause:
    card_name: str
    index: int
    raw_text: str
    condition: str
    costs: str
    targets: str
    resolution: str
    usage_limit: str | None
    hopt_id: str | None
    quick_effect: bool
    spell_speed: int
    effect_kind: str
    continuous_effect: bool
    preconditions: list[str]
    cost_actions: list[SymbolicAction]
    resolve_actions: list[SymbolicAction]
    confidence: float
    needs_manual_review: bool
    parser_source: str
    llm_suggestion: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SearchToHand", re.compile(r"(?:add|take)\s+.+?\s+from (?:your )?deck to (?:your )?hand", re.I)),
    ("SpecialSummon", re.compile(r"special summon", re.I)),
    ("SendToGY", re.compile(r"send\s+.+?\s+to the (?:gy|graveyard)", re.I)),
    ("Discard", re.compile(r"discard\s+", re.I)),
    ("Draw", re.compile(r"draw\s+\d+", re.I)),
    ("Destroy", re.compile(r"destroy\s+", re.I)),
    ("Banish", re.compile(r"banish\s+", re.I)),
    ("ReturnToHand", re.compile(r"return\s+.+?\s+to the hand", re.I)),
    ("Negate", re.compile(r"negate\s+(?:the|that|it|its|their)?\s*(?:effect|activation)?", re.I)),
    ("PayLP", re.compile(r"pay\s+\d[\d,]*\s+life points?", re.I)),
    ("Tribute", re.compile(r"tribute\s+", re.I)),
)


def _split_clauses(description: str) -> list[str]:
    parts = re.split(r"(?:\r?\n|(?=[①-⑳]))+", description)
    clauses: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("●") and clauses:
            clauses[-1] = f"{clauses[-1]} {part}"
        else:
            clauses.append(part)
    return clauses


def _get_usage_limit(text: str, card_name: str, index: int) -> tuple[str | None, str | None]:
    folded = text.casefold()
    if not re.search(r"\b(?:once )?per turn\b", folded):
        return None, None
    # "You can only use..." / "You can only activate..." is a hard OPT clause.
    hard = bool(
        re.search(r"\bonly\s+(?:use|activate)\b.*?\b(?:once )?per turn\b", text, re.I)
        or re.search(r"\bonly once per turn\b", text, re.I)
    )
    scope = "HOPT" if hard else "OPT"
    return scope, f"{card_name.casefold().replace(' ', '_')}::effect_{index}"


def _is_standalone_usage_restriction(text: str) -> bool:
    return bool(
        re.search(r"\b(?:once )?per turn\b", text, re.I)
        and re.match(r"^(?:you can only|you can use only|you can activate only)", text.strip(), re.I)
        and ":" not in text
        and ";" not in text
    )


def _symbolic_actions(text: str, scope: str) -> list[SymbolicAction]:
    actions: list[SymbolicAction] = []
    for operator, pattern in ACTION_PATTERNS:
        match = pattern.search(text)
        if match:
            actions.append(
                SymbolicAction(
                    operator=operator,
                    source_text=match.group(0),
                    scope=scope,
                )
            )
    return actions


def _extract_preconditions(condition: str, raw: str) -> list[str]:
    predicates: list[str] = []
    text = f"{condition} {raw}"
    if re.search(r"\bhand\b", text, re.I):
        predicates.append("Check_Zone(Hand)")
    if re.search(r"\bmain phase\b", condition, re.I):
        predicates.append("Check_Phase(Main)")
    if re.search(r"\bonce per turn\b", text, re.I):
        predicates.append("Check_HOPT_Available")
    if re.search(r"\bif you control\b", condition, re.I):
        predicates.append("Check_Control_Condition")
    if re.search(r"\bwhile this card is equipped\b", condition, re.I):
        predicates.append("Check_Equipped_State")
    return predicates


def _effect_timing(raw: str, card_kind: str) -> tuple[bool, int, bool, str]:
    kind = card_kind.casefold()
    quick = bool(
        re.search(r"\bquick effect\b", raw, re.I)
        or re.search(r"\bduring (?:either player's|your opponent's) turn\b", raw, re.I)
    )
    if "counter trap" in kind:
        speed = 3
    elif quick or "quick-play" in kind or "trap" in kind:
        speed = 2
    else:
        speed = 1
    static_text = bool(
        re.match(r"^(?:while|as long as|cannot|this card is unaffected)", raw.strip(), re.I)
        and ":" not in raw
        and ";" not in raw
    )
    continuous_card = any(term in kind for term in ("continuous", "field spell", "equip"))
    continuous = static_text or (continuous_card and ":" not in raw and ";" not in raw)
    if continuous:
        effect_kind = "continuous"
    elif quick:
        effect_kind = "quick"
    elif ":" in raw and re.match(r"^(?:if|when|while)\b", raw.strip(), re.I):
        effect_kind = "trigger"
    elif "spell" in kind or "trap" in kind:
        effect_kind = "activation"
    else:
        effect_kind = "ignition"
    return quick, speed, continuous, effect_kind


def parse_clause(
    card_name: str, index: int, raw_text: str, *, card_kind: str = ""
) -> EffectClause:
    """Split one PSCT clause and recognize a small set of common action verbs."""
    raw = raw_text.strip()
    condition = ""
    tail = raw
    confidence = 0.55
    if ":" in raw:
        condition, tail = raw.split(":", 1)
        condition = condition.strip()
        tail = tail.strip()
        confidence += 0.2

    costs = ""
    targets = ""
    resolution = tail
    if ";" in tail:
        activation, resolution = tail.split(";", 1)
        activation = activation.strip()
        resolution = resolution.strip()
        target_match = re.search(r"\btarget\b", activation, re.I)
        if target_match:
            costs = activation[: target_match.start()].strip(" ,")
            targets = activation[target_match.start() :].strip()
        else:
            costs = activation
        confidence += 0.2
    elif ":" in raw:
        # No semicolon: keep the complete post-colon clause as a resolution candidate.
        resolution = tail

    # Older/abbreviated text can put a target in the resolution sentence.
    if not targets:
        target_match = re.search(r"\btarget\b[^.;]*", resolution, re.I)
        if target_match:
            targets = target_match.group(0).strip()

    cost_actions = _symbolic_actions(costs, "cost")
    resolution_actions = _symbolic_actions(resolution, "resolve")
    usage_limit, hopt_id = _get_usage_limit(raw, card_name, index)
    quick_effect, spell_speed, continuous_effect, effect_kind = _effect_timing(raw, card_kind)
    preconditions = _extract_preconditions(condition, raw)
    supported = bool(cost_actions or resolution_actions or targets or condition)
    if not supported:
        confidence = min(confidence, 0.35)
    confidence = min(1.0, confidence)
    return EffectClause(
        card_name=card_name,
        index=index,
        raw_text=raw,
        condition=condition,
        costs=costs,
        targets=targets,
        resolution=resolution,
        usage_limit=usage_limit,
        hopt_id=hopt_id,
        quick_effect=quick_effect,
        spell_speed=spell_speed,
        effect_kind=effect_kind,
        continuous_effect=continuous_effect,
        preconditions=preconditions,
        cost_actions=cost_actions,
        resolve_actions=resolution_actions,
        confidence=round(confidence, 2),
        needs_manual_review=(
            confidence < 0.8
            or not resolution_actions
            or "●" in raw
            or "includes any of these effects" in raw.casefold()
        ),
        parser_source="regex",
        llm_suggestion=None,
    )


def parse_psct(
    card_name: str,
    description: str,
    *,
    card_kind: str = "",
    llm_suggester: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> list[EffectClause]:
    """Parse clauses and optionally attach an LLM AST proposal for manual review.

    The callback receives (card_name, raw_clause) and returns a JSON-serializable
    mapping. Its output remains a suggestion and never overrides the regex AST or
    clears the manual-review flag.
    """
    source_clauses = _split_clauses(description)
    restrictions = [clause for clause in source_clauses if _is_standalone_usage_restriction(clause)]
    clauses = [clause for clause in source_clauses if clause not in restrictions]
    parsed: list[EffectClause] = []
    for index, clause in enumerate(clauses, start=1):
        effect = parse_clause(card_name, index, clause, card_kind=card_kind)
        if effect.needs_manual_review and llm_suggester is not None:
            suggestion = llm_suggester(card_name, clause)
            effect.llm_suggestion = json.loads(json.dumps(dict(suggestion), ensure_ascii=False))
            effect.parser_source = "regex+llm_proposal"
        parsed.append(effect)

    for restriction in restrictions:
        scope, _ = _get_usage_limit(restriction, card_name, 1)
        if scope is None or not parsed:
            continue
        folded = restriction.casefold()
        if "each effect" in folded or "every effect" in folded:
            affected = parsed
            shared_id = None
        elif re.search(r"\bonly\s+(?:activate|use)\s+1\b", restriction, re.I):
            affected = parsed
            shared_id = f"{card_name.casefold().replace(' ', '_')}::card_once_per_turn"
        else:
            affected = [parsed[-1]]
            shared_id = None
        for effect in affected:
            effect.usage_limit = scope
            effect.hopt_id = shared_id or f"{card_name.casefold().replace(' ', '_')}::effect_{effect.index}"
            if "Check_HOPT_Available" not in effect.preconditions:
                effect.preconditions.append("Check_HOPT_Available")
            effect.raw_text = f"{effect.raw_text}\n{restriction}"
            effect.needs_manual_review = True
    return parsed


def parse_card_record(record: dict[str, Any]) -> dict[str, Any]:
    name = str(record.get("name") or record.get("card_name") or "Unknown card")
    description = str(record.get("description") or record.get("text") or "")
    card_kind = " ".join(
        str(record.get(key) or "")
        for key in ("type", "race_type", "race", "frame_type", "frameType")
    )
    return {
        "card_id": record.get("card_id"),
        "name": name,
        "effects": [
            effect.to_dict()
            for effect in parse_psct(name, description, card_kind=card_kind)
        ],
    }


def parse_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [parse_card_record(record) for record in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON array of card records")
    parser.add_argument("--output", type=Path, help="write parsed JSON to this path")
    args = parser.parse_args(argv)
    records = json.loads(args.input.read_text(encoding="utf-8"))
    result = parse_records(records)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

