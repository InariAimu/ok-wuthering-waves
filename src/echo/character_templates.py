"""Human-editable character templates for GUI-started Echo checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Mapping

from src.echo.name_catalog import match_echo_name
from src.echo.optimizer import (
    EchoIdentity,
    NamedEchoBuild,
    NamedEquippedEcho,
    PolicyConfig,
)
from src.echo.panel import CharacterPanel, CharacterScoringProfile, EnergyRegenPolicy
from src.echo.simulation import ALL_STATS, EchoRoll, EchoState, EchoStat


ECHO_CHARACTER_TEMPLATE_KIND = "ok-ww.echo-character-templates"
ECHO_CHARACTER_TEMPLATE_SCHEMA_VERSION = 1
DEFAULT_ECHO_CHARACTER_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "echo_character_templates.json"
)

VALID_MAIN_STATS_BY_COST = {
    4: frozenset({
        "crit_rate", "crit_damage", "attack_percent", "defense_percent",
        "hp_percent", "healing_bonus",
    }),
    3: frozenset({
        "glacio_damage", "fusion_damage", "electro_damage", "aero_damage",
        "spectro_damage", "havoc_damage", "attack_percent",
        "defense_percent", "hp_percent", "energy_regen",
    }),
    1: frozenset({"attack_percent", "defense_percent", "hp_percent"}),
}


@dataclass(frozen=True)
class EchoCharacterTemplateContext:
    character: str
    sonata_set: str
    build: NamedEchoBuild
    candidate_pool: tuple[EchoIdentity, ...]
    policy: PolicyConfig


def _document(path: str | Path) -> Mapping[str, object]:
    source = Path(path).expanduser().resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise TypeError("Echo character template root must be an object")
    if data.get("kind") != ECHO_CHARACTER_TEMPLATE_KIND:
        raise ValueError("not an OK-WW Echo character template file")
    if data.get("schema_version") != ECHO_CHARACTER_TEMPLATE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported Echo character template schema: {data.get('schema_version')}"
        )
    characters = data.get("characters")
    if not isinstance(characters, Mapping) or not characters:
        raise ValueError("Echo character template file has no characters")
    return data


def echo_character_template_names(path: str | Path) -> tuple[str, ...]:
    characters = _document(path)["characters"]
    assert isinstance(characters, Mapping)
    return tuple(str(name) for name in characters)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _main_stat_for_cost(
        cost: int, defaults: Mapping[str, object],
        overrides: Mapping[int, str] | None) -> str:
    value = str((overrides or {}).get(cost) or defaults.get(str(cost)) or "")
    if value not in VALID_MAIN_STATS_BY_COST[cost]:
        raise ValueError(f"invalid COST {cost} main stat: {value!r}")
    return value


def load_echo_character_template(
        path: str | Path, character: str, *,
        main_stat_overrides: Mapping[int, str] | None = None,
        weight_overrides: Mapping[EchoStat, float] | None = None,
) -> EchoCharacterTemplateContext:
    """Load one GUI template and apply stable-code UI overrides."""

    document = _document(path)
    characters = document["characters"]
    assert isinstance(characters, Mapping)
    raw_template = characters.get(character)
    template = _mapping(raw_template, f"character {character!r}")
    sonata_set = str(template.get("sonata_set") or "").strip()
    if not sonata_set:
        raise ValueError(f"character {character!r} has no Sonata set")

    main_stats = _mapping(template.get("main_stats"), "main_stats")
    selected_main_stats = {
        cost: _main_stat_for_cost(cost, main_stats, main_stat_overrides)
        for cost in (1, 3, 4)
    }

    raw_weights = _mapping(template.get("weights"), "weights")
    weights = {
        stat: float(raw_weights.get(stat.value, 0.0)) for stat in ALL_STATS
    }
    weights.update(weight_overrides or {})
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError("substat weights must be finite and non-negative")

    energy_data = template.get("energy_policy")
    energy_policy = (
        EnergyRegenPolicy(**_mapping(energy_data, "energy_policy"))
        if energy_data is not None else None
    )
    profile = CharacterScoringProfile(
        str(template.get("profile_name") or character),
        weights,
        energy_policy=energy_policy,
    )
    base_panel_data = _mapping(template.get("base_panel", {}), "base_panel")
    base_panel = CharacterPanel({
        EchoStat(str(stat)): float(value)
        for stat, value in base_panel_data.items()
    })

    raw_echoes = template.get("equipped_echoes")
    if not isinstance(raw_echoes, list):
        raise TypeError("equipped_echoes must be an array")
    echoes: list[NamedEquippedEcho] = []
    for raw_echo in raw_echoes:
        echo = _mapping(raw_echo, "equipped Echo")
        cost = int(echo["cost"])
        canonical_name = match_echo_name(str(echo["name"])).canonical
        raw_rolls = echo.get("rolls")
        if not isinstance(raw_rolls, list):
            raise TypeError("equipped Echo rolls must be an array")
        rolls: list[EchoRoll] = []
        for raw_roll in raw_rolls:
            roll = _mapping(raw_roll, "Echo roll")
            rolls.append(EchoRoll.from_value(
                EchoStat(str(roll["stat"])),
                float(roll["value"]),
            ))
        echoes.append(NamedEquippedEcho(
            str(echo["slot"]),
            EchoIdentity(
                canonical_name,
                cost,
                f"cost_{cost}:{selected_main_stats[cost]}",
            ),
            EchoState(tuple(rolls)),
        ))
    if {echo.slot for echo in echoes} != {"4C", "3C-1", "3C-2", "1C-1", "1C-2"}:
        raise ValueError("equipped_echoes must define 4C, 3C-1, 3C-2, 1C-1 and 1C-2")
    build = NamedEchoBuild(profile, base_panel, tuple(echoes))

    raw_candidates = _mapping(template.get("candidate_names"), "candidate_names")
    pool: list[EchoIdentity] = []
    for cost in (1, 3, 4):
        names = raw_candidates.get(str(cost), [])
        if not isinstance(names, list):
            raise TypeError(f"candidate_names.{cost} must be an array")
        for name in names:
            pool.append(EchoIdentity(
                match_echo_name(str(name)).canonical,
                cost,
                f"cost_{cost}:{selected_main_stats[cost]}",
            ))
    candidate_pool = tuple(dict.fromkeys(pool))
    if not candidate_pool:
        raise ValueError("candidate_names must contain at least one Echo")
    incompatible = tuple(
        identity for identity in candidate_pool
        if not build.compatible_slots(identity)
    )
    if incompatible:
        raise ValueError(
            "candidate names have no compatible equipped slot: "
            + ", ".join(identity.name for identity in incompatible)
        )

    policy_data = _mapping(template.get("policy", {}), "policy")
    policy = PolicyConfig(
        rollout_samples=int(policy_data.get("rollout_samples", 2_000)),
        minimum_gain=float(policy_data.get("minimum_gain", 0.0)),
        continuation_efficiency_ratio=float(
            policy_data.get("continuation_efficiency_ratio", 0.9)
        ),
        max_attempts=int(policy_data.get("max_attempts", 2_000)),
        analysis_seed=int(policy_data.get("analysis_seed", 15_100)),
        outcome_seed=int(policy_data.get("outcome_seed", 151)),
        required_stats=frozenset(
            EchoStat(str(stat)) for stat in policy_data.get("required_stats", [])
        ),
        deferred_recycling=bool(policy_data.get("deferred_recycling", False)),
    )
    return EchoCharacterTemplateContext(
        character,
        sonata_set,
        build,
        candidate_pool,
        policy,
    )
