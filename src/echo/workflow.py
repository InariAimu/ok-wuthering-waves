"""Persistent hand-off between Echo inventory checking and live enhancement."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping

from src.echo.inventory import EchoCharacterInventoryAnalysis
from src.echo.optimizer import (
    EchoIdentity,
    NamedEchoBuild,
    NamedEquippedEcho,
    PolicyConfig,
)
from src.echo.panel import CharacterPanel, CharacterScoringProfile, EnergyRegenPolicy
from src.echo.simulation import EchoRoll, EchoState, EchoStat


ECHO_CHECK_RESULT_KIND = "ok-ww.echo-inventory-check"
ECHO_CHECK_RESULT_SCHEMA_VERSION = 1


def _identity_to_dict(identity: EchoIdentity) -> dict[str, object]:
    return {
        "name": identity.name,
        "cost": identity.cost,
        "compatibility": identity.compatibility,
    }


def _identity_from_dict(data: Mapping[str, object]) -> EchoIdentity:
    return EchoIdentity(
        str(data["name"]),
        int(data["cost"]),
        str(data.get("compatibility", "default")),
    )


def named_echo_build_to_dict(build: NamedEchoBuild) -> dict[str, object]:
    energy_policy = build.profile.energy_policy
    return {
        "profile": {
            "name": build.profile.name,
            "weights": {
                stat.value: weight for stat, weight in build.profile.weights.items()
            },
            "energy_regen_target": build.profile.energy_regen_target,
            "energy_policy": asdict(energy_policy) if energy_policy else None,
        },
        "base_panel": {
            stat.value: value for stat, value in build.base_panel.values.items()
        },
        "echoes": [
            {
                "slot": echo.slot,
                "identity": _identity_to_dict(echo.identity),
                "rolls": [
                    {
                        "stat": roll.stat.value,
                        "tier": roll.tier,
                        "value": roll.value,
                    }
                    for roll in echo.state.rolls
                ],
            }
            for echo in build.echoes
        ],
    }


def named_echo_build_from_dict(data: Mapping[str, object]) -> NamedEchoBuild:
    profile_data = data["profile"]
    if not isinstance(profile_data, Mapping):
        raise TypeError("build.profile must be an object")
    weights_data = profile_data["weights"]
    if not isinstance(weights_data, Mapping):
        raise TypeError("build.profile.weights must be an object")
    energy_data = profile_data.get("energy_policy")
    if energy_data is not None and not isinstance(energy_data, Mapping):
        raise TypeError("build.profile.energy_policy must be an object or null")
    energy_policy = EnergyRegenPolicy(**energy_data) if energy_data else None
    profile = CharacterScoringProfile(
        str(profile_data["name"]),
        {EchoStat(str(stat)): float(value) for stat, value in weights_data.items()},
        (
            float(profile_data["energy_regen_target"])
            if profile_data.get("energy_regen_target") is not None
            else None
        ),
        energy_policy,
    )

    panel_data = data["base_panel"]
    if not isinstance(panel_data, Mapping):
        raise TypeError("build.base_panel must be an object")
    panel = CharacterPanel({
        EchoStat(str(stat)): float(value) for stat, value in panel_data.items()
    })

    echoes_data = data["echoes"]
    if not isinstance(echoes_data, list):
        raise TypeError("build.echoes must be an array")
    echoes: list[NamedEquippedEcho] = []
    for echo_data in echoes_data:
        if not isinstance(echo_data, Mapping):
            raise TypeError("each build Echo must be an object")
        identity_data = echo_data["identity"]
        rolls_data = echo_data["rolls"]
        if not isinstance(identity_data, Mapping) or not isinstance(rolls_data, list):
            raise TypeError("Echo identity/rolls have invalid types")
        rolls = tuple(
            EchoRoll(EchoStat(str(roll["stat"])), int(roll["tier"]))
            for roll in rolls_data
            if isinstance(roll, Mapping)
        )
        if len(rolls) != len(rolls_data):
            raise TypeError("each Echo roll must be an object")
        echoes.append(NamedEquippedEcho(
            str(echo_data["slot"]),
            _identity_from_dict(identity_data),
            EchoState(rolls),
        ))
    return NamedEchoBuild(profile, panel, tuple(echoes))


def policy_config_to_dict(config: PolicyConfig) -> dict[str, object]:
    data = asdict(config)
    data["required_stats"] = sorted(stat.value for stat in config.required_stats)
    return data


def policy_config_from_dict(data: Mapping[str, object]) -> PolicyConfig:
    return PolicyConfig(
        rollout_samples=int(data.get("rollout_samples", 2_000)),
        minimum_gain=float(data.get("minimum_gain", 0.0)),
        continuation_efficiency_ratio=float(
            data.get("continuation_efficiency_ratio", 0.90)
        ),
        max_attempts=int(data.get("max_attempts", 2_000)),
        analysis_seed=int(data.get("analysis_seed", 15_100)),
        outcome_seed=int(data.get("outcome_seed", 151)),
        required_stats=frozenset(
            EchoStat(str(stat)) for stat in data.get("required_stats", [])
        ),
        deferred_recycling=bool(data.get("deferred_recycling", False)),
    )


@dataclass(frozen=True)
class EchoInventoryCheckResult:
    """Reviewable, versioned input for the separate enhancement step."""

    sonata_set: str
    build: NamedEchoBuild
    candidate_pool: tuple[EchoIdentity, ...]
    policy: PolicyConfig
    inspection: Mapping[str, object]
    created_at: str
    source_path: str = ""

    def __post_init__(self) -> None:
        if not self.sonata_set.strip():
            raise ValueError("sonata_set must not be empty")
        if not self.candidate_pool:
            raise ValueError("candidate_pool must not be empty")
        if len(self.candidate_pool) != len(set(self.candidate_pool)):
            raise ValueError("candidate_pool must not contain duplicates")
        incompatible = [
            identity for identity in self.candidate_pool
            if not self.build.compatible_slots(identity)
        ]
        if incompatible:
            names = ", ".join(identity.name for identity in incompatible)
            raise ValueError(
                f"candidate_pool contains identities with no compatible slot: {names}"
            )
        inspected_set = self.inspection.get("sonata_set")
        if inspected_set is not None and inspected_set != self.sonata_set:
            raise ValueError("inspection Sonata set does not match model input")

    @classmethod
    def from_analysis(
            cls, analysis: EchoCharacterInventoryAnalysis, *,
            build: NamedEchoBuild,
            candidate_pool: tuple[EchoIdentity, ...] | list[EchoIdentity],
            policy: PolicyConfig | None = None) -> "EchoInventoryCheckResult":
        return cls(
            analysis.sonata_set,
            build,
            tuple(candidate_pool),
            policy or PolicyConfig(),
            analysis.to_dict(),
            datetime.now(timezone.utc).isoformat(),
        )

    @property
    def replacement_count(self) -> int:
        replacements = self.inspection.get("replacements", [])
        return len(replacements) if isinstance(replacements, list) else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": ECHO_CHECK_RESULT_KIND,
            "schema_version": ECHO_CHECK_RESULT_SCHEMA_VERSION,
            "created_at": self.created_at,
            "sonata_set": self.sonata_set,
            "review": {
                "instruction": "Review this file, then explicitly select it in step 2.",
                "replacement_count": self.replacement_count,
            },
            "model_input": {
                "build": named_echo_build_to_dict(self.build),
                "candidate_pool": [
                    _identity_to_dict(identity) for identity in self.candidate_pool
                ],
                "policy": policy_config_to_dict(self.policy),
            },
            "inspection": dict(self.inspection),
        }

    @classmethod
    def from_dict(
            cls, data: Mapping[str, object], *, source_path: str = ""
    ) -> "EchoInventoryCheckResult":
        if data.get("kind") != ECHO_CHECK_RESULT_KIND:
            raise ValueError("not an OK-WW Echo inventory check result")
        if data.get("schema_version") != ECHO_CHECK_RESULT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported Echo check schema version: {data.get('schema_version')}"
            )
        model_input = data.get("model_input")
        inspection = data.get("inspection")
        if not isinstance(model_input, Mapping) or not isinstance(inspection, Mapping):
            raise TypeError("model_input and inspection must be objects")
        build_data = model_input.get("build")
        pool_data = model_input.get("candidate_pool")
        policy_data = model_input.get("policy")
        if not isinstance(build_data, Mapping):
            raise TypeError("model_input.build must be an object")
        if not isinstance(pool_data, list):
            raise TypeError("model_input.candidate_pool must be an array")
        if not isinstance(policy_data, Mapping):
            raise TypeError("model_input.policy must be an object")
        pool = tuple(
            _identity_from_dict(identity)
            for identity in pool_data
            if isinstance(identity, Mapping)
        )
        if len(pool) != len(pool_data):
            raise TypeError("each candidate_pool item must be an object")
        return cls(
            str(data["sonata_set"]),
            named_echo_build_from_dict(build_data),
            pool,
            policy_config_from_dict(policy_data),
            dict(inspection),
            str(data["created_at"]),
            source_path,
        )

    @classmethod
    def read(cls, path: str | Path) -> "EchoInventoryCheckResult":
        source = Path(path).expanduser().resolve()
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise TypeError("Echo check result root must be an object")
        return cls.from_dict(data, source_path=str(source))

    def write(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target
