"""Card schema and normalization helpers for Master Duel Meta records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping
from urllib.parse import quote


BAN_STATUSES = {"Unlimited", "Semi-Limited", "Limited", "Forbidden"}
RARITIES = {"UR", "SR", "R", "N"}
FRAME_ORDER = ("Link", "Xyz", "Synchro", "Fusion", "Ritual", "Pendulum", "Normal", "Effect")
LINK_ARROW_NAMES = {
    "1": "Bottom-Left",
    "2": "Bottom",
    "3": "Bottom-Right",
    "4": "Left",
    "6": "Right",
    "7": "Top-Left",
    "8": "Top",
    "9": "Top-Right",
}


@dataclass(slots=True)
class Card:
    card_id: str
    name: str
    type: str
    frame_type: str
    attribute: str | None
    race_type: str | None
    level_rank_link: int | None
    atk: int | None
    def_: int | None
    scale: int | None
    link_markers: list[str]
    archetype: str
    archetype_source: str
    description: str
    ban_status: str
    rarity: str | None
    mdm_url: str
    ban_status_verified: bool = False
    source_metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the public JSON shape, using `def` as the external field name."""
        result = asdict(self)
        result["def"] = result.pop("def_")
        return result


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, "", []):
            return value
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return []


def _name_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        label = _first(value, "name", "title", "archetype")
        return [str(label)] if label else []
    if isinstance(value, (list, tuple, set)):
        return [name for item in value for name in _name_values(item)]
    return []


def _frame_type(raw: Mapping[str, Any], card_type: str) -> str:
    subtypes = {item.casefold() for item in _strings(_first(raw, "monsterType", "frameType", "frame_type"))}
    # Some MDM records encode a single frame in `type` rather than `monsterType`.
    subtypes.update(item.casefold() for item in _strings(raw.get("type")))
    for frame in FRAME_ORDER:
        if frame.casefold() in subtypes:
            return frame
    if card_type == "Monster":
        return "Normal" if "normal" in subtypes else "Effect"
    return "Normal"


def _link_markers(value: Any) -> list[str]:
    result: list[str] = []
    for arrow in _strings(value):
        normalized = LINK_ARROW_NAMES.get(arrow, arrow)
        if normalized not in result:
            result.append(normalized)
    return result


def _ban_status(raw: Mapping[str, Any], supplied: Mapping[str, Any] | None) -> tuple[str, bool]:
    candidates: list[Any] = []
    if supplied:
        candidates.extend(supplied.get(key) for key in ("ban_status", "banStatus", "status", "limit"))
    candidates.extend(raw.get(key) for key in ("ban_status", "banStatus", "banlistStatus", "limitStatus"))
    aliases = {
        "unlimited": "Unlimited",
        "unlimited 3": "Unlimited",
        "semi-limited": "Semi-Limited",
        "semi limited": "Semi-Limited",
        "limited 2": "Semi-Limited",
        "limited": "Limited",
        "limited 1": "Limited",
        "forbidden": "Forbidden",
        "banned": "Forbidden",
        "0": "Forbidden",
        "1": "Limited",
        "2": "Semi-Limited",
        "3": "Unlimited",
    }
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        canonical = aliases.get(text.casefold(), text)
        if canonical in BAN_STATUSES:
            return canonical, True
    return "Unlimited", False


def normalize_mdm_card(
    raw: Mapping[str, Any], banlist_entry: Mapping[str, Any] | None = None
) -> Card:
    """Normalize one public MDM API record to the project's stable card schema."""
    name = str(_first(raw, "name", "name_en", "englishName") or "Unknown card")
    card_type = str(_first(raw, "type", "cardType") or "Monster")
    if card_type not in {"Monster", "Spell", "Trap"}:
        card_type = "Monster" if "monster" in card_type.casefold() else card_type.title()
    card_id = str(_first(raw, "gameId", "card_id", "konamiID", "id", "_id") or name)
    mdm_id = str(_first(raw, "_id", "id") or "")
    encoded_name = quote(name, safe="")
    mdm_url = f"https://www.masterduelmeta.com/cards/{encoded_name}"
    if mdm_id:
        mdm_url += f"/{quote(mdm_id, safe='')}"

    rarity_value = _first(raw, "rarity", "craftRarity")
    rarity = str(rarity_value).upper() if rarity_value is not None else None
    if rarity not in RARITIES:
        rarity = None
    ban_status, ban_status_verified = _ban_status(raw, banlist_entry)

    archetypes = _name_values(_first(raw, "archetype", "archetypes"))
    archetype_source = "archetype" if archetypes else "unavailable"
    if not archetypes:
        # MDM commonly exposes card/deck classifications as deckTypes.
        archetypes = _name_values(raw.get("deckTypes"))
        archetype_source = "deckTypes" if archetypes else "unavailable"
    metadata_keys = (
        "konamiID",
        "gameId",
        "monsterType",
        "deckTypes",
        "obtain",
        "release",
        "ocgRelease",
        "tcgRelease",
        "alternateArt",
        "isUpdated",
    )
    source_metadata = {key: raw[key] for key in metadata_keys if key in raw}
    return Card(
        card_id=card_id,
        name=name,
        type=card_type,
        frame_type=_frame_type(raw, card_type),
        attribute=_first(raw, "attribute", "attributeName"),
        race_type=_first(raw, "race", "race_type", "raceType"),
        level_rank_link=_as_int(_first(raw, "level", "rank", "linkRating", "link")),
        atk=_as_int(raw.get("atk")),
        def_=_as_int(_first(raw, "def", "defense")),
        scale=_as_int(_first(raw, "scale", "pendulumScale")),
        link_markers=_link_markers(_first(raw, "linkArrows", "link_markers", "linkMarkers")),
        archetype=", ".join(archetypes),
        archetype_source=archetype_source,
        description=str(_first(raw, "description", "text", "effect") or ""),
        ban_status=ban_status,
        rarity=rarity,
        mdm_url=mdm_url,
        ban_status_verified=ban_status_verified,
        source_metadata=source_metadata,
    )

