"""Structured observations produced while scanning the filtered Echo inventory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import re
from statistics import median
from typing import Iterable, Mapping

from src.echo.name_catalog import EchoNameMatch, match_echo_name
from src.echo.optimizer import (
    EchoIdentity,
    NamedEchoBuild,
    ReplacementOption,
)
from src.echo.simulation import EchoRoll, EchoState, EchoStat


_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[%％])?")
_LEVEL_RE = re.compile(r"\+\s*(\d{1,2})\s*$")
_COST_RE = re.compile(r"COST\s*([134])", re.IGNORECASE)

_STAT_ALIASES: tuple[tuple[tuple[str, ...], EchoStat], ...] = (
    (("暴击伤害", "暴擊傷害"), EchoStat.CRIT_DAMAGE),
    (("暴击", "暴擊"), EchoStat.CRIT_RATE),
    (("共鸣效率", "共鳴效率", "效率"), EchoStat.ENERGY_REGEN),
    (("普攻",), EchoStat.BASIC_ATTACK_DAMAGE),
    (("重击", "重擊"), EchoStat.HEAVY_ATTACK_DAMAGE),
    (("共鸣解放", "共鳴解放", "解放"), EchoStat.RESONANCE_LIBERATION_DAMAGE),
    (("共鸣技能", "共鳴技能", "技能"), EchoStat.RESONANCE_SKILL_DAMAGE),
)


class EchoMainStat(str, Enum):
    """Primary Echo stats that cannot occur as rolled substats."""

    GLACIO_DAMAGE = "glacio_damage"
    FUSION_DAMAGE = "fusion_damage"
    ELECTRO_DAMAGE = "electro_damage"
    AERO_DAMAGE = "aero_damage"
    SPECTRO_DAMAGE = "spectro_damage"
    HAVOC_DAMAGE = "havoc_damage"
    HEALING_BONUS = "healing_bonus"


_MAIN_STAT_ALIASES: tuple[tuple[tuple[str, ...], EchoMainStat], ...] = (
    (("冷凝伤害", "冷凝傷害"), EchoMainStat.GLACIO_DAMAGE),
    (("热熔伤害", "熱熔傷害"), EchoMainStat.FUSION_DAMAGE),
    (("导电伤害", "導電傷害"), EchoMainStat.ELECTRO_DAMAGE),
    (("气动伤害", "氣動傷害"), EchoMainStat.AERO_DAMAGE),
    (("衍射伤害", "衍射傷害"), EchoMainStat.SPECTRO_DAMAGE),
    (("湮灭伤害", "湮滅傷害"), EchoMainStat.HAVOC_DAMAGE),
    (("治疗效果", "治療效果"), EchoMainStat.HEALING_BONUS),
)


def _token_value(token, key: str, default=0):
    if isinstance(token, Mapping):
        if key == "name":
            return token.get("name", token.get("text", default))
        return token.get(key, default)
    if key == "name" and not hasattr(token, "name"):
        return getattr(token, "text", default)
    return getattr(token, key, default)


def _token_text(token) -> str:
    return str(_token_value(token, "name", "")).strip()


def _token_center_y(token) -> float:
    return float(_token_value(token, "y")) + float(_token_value(token, "height")) / 2


def _stat_from_text(label: str, value_text: str) -> EchoStat | EchoMainStat | None:
    for needles, stat in _MAIN_STAT_ALIASES:
        if any(needle in label for needle in needles):
            return stat
    for needles, stat in _STAT_ALIASES:
        if any(needle in label for needle in needles):
            return stat
    is_percent = "%" in value_text or "％" in value_text
    if "攻击" in label or "攻擊" in label:
        return EchoStat.ATTACK_PERCENT if is_percent else EchoStat.ATTACK_FLAT
    if "生命" in label:
        return EchoStat.HP_PERCENT if is_percent else EchoStat.HP_FLAT
    if "防御" in label or "防禦" in label:
        return EchoStat.DEFENSE_PERCENT if is_percent else EchoStat.DEFENSE_FLAT
    return None


def _parse_value(value_text: str) -> float:
    match = _NUMBER_RE.search(value_text.replace(" ", ""))
    if not match:
        raise ValueError(f"missing numeric value in {value_text!r}")
    value = match.group().replace("％", "%").rstrip("%")
    return float(value)


@dataclass(frozen=True)
class ObservedEchoStat:
    stat: EchoStat | EchoMainStat
    value: float
    label: str
    value_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "stat": self.stat.value,
            "value": self.value,
            "label": self.label,
            "value_text": self.value_text,
        }


def pair_echo_stat_rows(tokens: Iterable[object]) -> tuple[ObservedEchoStat, ...]:
    """Group OCR boxes into horizontal stat/value rows.

    The game may emit the leading ``+`` as its own OCR box and may merge a
    label with its value. Grouping by vertical centre handles both layouts.
    """

    source = [token for token in tokens if _token_text(token)]
    if not source:
        return ()
    typical_height = median(max(1, float(_token_value(t, "height", 1))) for t in source)
    tolerance = max(5.0, typical_height * 0.8)
    rows: list[list[object]] = []
    for token in sorted(source, key=lambda item: (_token_center_y(item), _token_value(item, "x"))):
        center = _token_center_y(token)
        if rows:
            row_center = sum(_token_center_y(item) for item in rows[-1]) / len(rows[-1])
            if abs(center - row_center) <= tolerance:
                rows[-1].append(token)
                continue
        rows.append([token])

    parsed: list[ObservedEchoStat] = []
    for row in rows:
        texts = [_token_text(item) for item in sorted(row, key=lambda item: _token_value(item, "x"))]
        joined = " ".join(texts)
        value_matches = list(_NUMBER_RE.finditer(joined))
        if not value_matches:
            continue
        value_text = value_matches[-1].group()
        label = joined[:value_matches[-1].start()].replace("+", " ").strip()
        stat = _stat_from_text(label, value_text)
        if stat is None:
            continue
        parsed.append(ObservedEchoStat(stat, _parse_value(value_text), label, value_text))
    return tuple(parsed)


def _parse_name(tokens: Iterable[object]) -> str:
    ignored = ("声骸", "聲骸", "COST", "合鸣", "合鳴", "技能", "简述", "簡述")
    candidates = []
    for token in tokens:
        text = _token_text(token)
        if not re.search(r"[\u3400-\u9fff]", text):
            continue
        if any(part in text for part in ignored):
            continue
        if _stat_from_text(text, "%") is not None:
            continue
        candidates.append(token)
    if not candidates:
        raise ValueError("could not read Echo name")
    candidates.sort(key=lambda item: (_token_value(item, "y"), -float(_token_value(item, "confidence", 0))))
    return _LEVEL_RE.sub("", _token_text(candidates[0])).strip()


def match_echo_name_tokens(tokens: Iterable[object]) -> EchoNameMatch:
    """Extract one OCR name and constrain it to the legal Echo catalogue."""

    return match_echo_name(_parse_name(tokens))


def _parse_level(tokens: Iterable[object]) -> int:
    for token in tokens:
        match = _LEVEL_RE.search(_token_text(token).replace("＋", "+"))
        if match:
            return int(match.group(1))
    raise ValueError("could not read Echo level")


def _parse_cost(tokens: Iterable[object], cost_hint: int | None) -> int:
    text = " ".join(_token_text(token) for token in tokens)
    compact = re.sub(r"\s+", "", text)
    match = _COST_RE.search(compact)
    observed = int(match.group(1)) if match else None
    if observed is not None and cost_hint is not None and observed != cost_hint:
        raise ValueError(f"Echo COST mismatch: expected {cost_hint}, read {observed}")
    cost = observed if observed is not None else cost_hint
    if cost not in (1, 3, 4):
        raise ValueError("could not read Echo COST")
    return cost


@dataclass(frozen=True)
class EchoInventoryItem:
    identity: EchoIdentity
    level: int
    main_stats: tuple[ObservedEchoStat, ...]
    state: EchoState
    page_index: int
    cell_index: int
    screenshot: str = ""

    @property
    def attribute_key(self) -> tuple[object, ...]:
        """Canonical existence key for overlap and end-of-list detection.

        Inventory position, screenshot, level, the fixed secondary main-stat
        line, and public roll-tier metadata are deliberately excluded.  The
        scanner only needs to know whether the same name/COST/primary-main-stat
        and five substat values have already appeared.  Substat display order
        is not part of that identity.
        """

        primary_main_stat = self.main_stats[0]
        substats = tuple(sorted(
            (roll.stat.value, roll.value) for roll in self.state.rolls
        ))

        return (
            self.identity.name,
            self.identity.cost,
            (primary_main_stat.stat.value, primary_main_stat.value),
            substats,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.attribute_key, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.identity.name,
            "cost": self.identity.cost,
            "compatibility": self.identity.compatibility,
            "level": self.level,
            "main_stats": [line.to_dict() for line in self.main_stats],
            "substats": [
                {"stat": roll.stat.value, "value": roll.value, "tier_index": roll.tier}
                for roll in self.state.rolls
            ],
            "page_index": self.page_index,
            "cell_index": self.cell_index,
            "screenshot": self.screenshot,
            "fingerprint": self.fingerprint,
        }


def parse_echo_inventory_item(
        identity_tokens: Iterable[object],
        main_stat_tokens: Iterable[object],
        substat_tokens: Iterable[object], *,
        cost_hint: int | None,
        page_index: int,
        cell_index: int,
        screenshot: str = "",
        canonical_name: str | None = None) -> EchoInventoryItem:
    identity_source = tuple(identity_tokens)
    name = canonical_name or _parse_name(identity_source)
    level = _parse_level(identity_source)
    cost = _parse_cost(identity_source, cost_hint)
    main_stats = pair_echo_stat_rows(main_stat_tokens)
    if len(main_stats) != 2:
        raise ValueError(f"expected 2 Echo main stats, read {len(main_stats)}")
    substats = pair_echo_stat_rows(substat_tokens)
    if level == 25 and len(substats) != 5:
        raise ValueError(f"expected 5 substats for +25 Echo, read {len(substats)}")
    if any(not isinstance(line.stat, EchoStat) for line in substats):
        raise ValueError("main-stat-only attribute appeared in Echo substats")
    rolls = tuple(EchoRoll.from_value(line.stat, line.value) for line in substats)
    compatibility = f"cost_{cost}:{main_stats[0].stat.value}"
    return EchoInventoryItem(
        EchoIdentity(name, cost, compatibility),
        level,
        main_stats,
        EchoState(rolls),
        page_index,
        cell_index,
        screenshot,
    )


@dataclass(frozen=True)
class EchoInventoryScanIssue:
    cost: int
    page_index: int
    cell_index: int
    reason: str
    screenshot: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "cost": self.cost,
            "page_index": self.page_index,
            "cell_index": self.cell_index,
            "reason": self.reason,
            "screenshot": self.screenshot,
        }


@dataclass(frozen=True)
class EchoInventoryReplacement:
    """A scanned Echo and the equipped slot where it improves the build most."""

    item: EchoInventoryItem
    option: ReplacementOption

    def to_dict(self) -> dict[str, object]:
        return {
            "item": self.item.to_dict(),
            "replacement": {
                "slot": self.option.slot,
                "delta": self.option.delta,
                "final_score": self.option.final_score,
                "energy_feasible": self.option.energy_feasible,
            },
        }


def rank_echo_inventory_replacements(
        build: NamedEchoBuild,
        items: Iterable[EchoInventoryItem], *,
        minimum_gain: float = 0.0,
        required_stats: frozenset[EchoStat] = frozenset(),
) -> tuple[EchoInventoryReplacement, ...]:
    """Score each unique structured inventory item once, best gain first."""

    seen_attribute_keys: set[tuple[object, ...]] = set()
    replacements: list[EchoInventoryReplacement] = []
    for item in items:
        if item.level != 25 or item.state.slot_count != 5:
            continue
        if item.attribute_key in seen_attribute_keys:
            continue
        seen_attribute_keys.add(item.attribute_key)
        option = build.best_replacement(
            item.identity,
            item.state,
            minimum_gain=minimum_gain,
            required_stats=required_stats,
        )
        if option is not None:
            replacements.append(EchoInventoryReplacement(item, option))
    return tuple(sorted(
        replacements,
        key=lambda candidate: (
            -candidate.option.delta,
            candidate.option.slot,
            candidate.item.fingerprint,
        ),
    ))


@dataclass(frozen=True)
class EchoInventoryScanResult:
    items: tuple[EchoInventoryItem, ...]
    issues: tuple[EchoInventoryScanIssue, ...]
    pages_scanned: int
    duplicate_fingerprints: int
    stop_reason: str

    def __post_init__(self) -> None:
        for name, value in (
            ("pages_scanned", self.pages_scanned),
            ("duplicate_fingerprints", self.duplicate_fingerprints),
        ):
            if not isinstance(value, int) or value < 0 or not math.isfinite(value):
                raise ValueError(f"{name} must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "issues": [issue.to_dict() for issue in self.issues],
            "pages_scanned": self.pages_scanned,
            "duplicate_fingerprints": self.duplicate_fingerprints,
            "stop_reason": self.stop_reason,
        }

    def rank_replacements(
            self, build: NamedEchoBuild, *,
            minimum_gain: float = 0.0,
            required_stats: frozenset[EchoStat] = frozenset(),
    ) -> tuple[EchoInventoryReplacement, ...]:
        """Return unique scanned improvements ranked by their score gain."""

        return rank_echo_inventory_replacements(
            build,
            self.items,
            minimum_gain=minimum_gain,
            required_stats=required_stats,
        )


@dataclass(frozen=True)
class EchoCharacterInventoryAnalysis:
    """Set-scoped inventory scan plus its ranked build improvements."""

    sonata_set: str
    scan: EchoInventoryScanResult
    replacements: tuple[EchoInventoryReplacement, ...]
    report: str = ""

    def __post_init__(self) -> None:
        if not self.sonata_set.strip():
            raise ValueError("sonata_set must not be empty")

    @property
    def best_replacement(self) -> EchoInventoryReplacement | None:
        return self.replacements[0] if self.replacements else None

    def to_dict(self) -> dict[str, object]:
        return {
            "sonata_set": self.sonata_set,
            "scan": self.scan.to_dict(),
            "replacements": [item.to_dict() for item in self.replacements],
            "report": self.report,
        }
