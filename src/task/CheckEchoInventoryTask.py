"""Step 1: inspect one Sonata set without spending enhancement resources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Iterable, Mapping

from src.echo.character_templates import (
    DEFAULT_ECHO_CHARACTER_TEMPLATE,
    echo_character_template_names,
    load_echo_character_template,
)
from src.echo.inventory import (
    EchoCharacterInventoryAnalysis,
    EchoInventoryItem,
    EchoInventoryScanResult,
)
from src.echo.optimizer import (
    EchoIdentity,
    NamedEchoBuild,
    NamedEquippedEcho,
    PolicyConfig,
)
from src.echo.workflow import EchoInventoryCheckResult
from src.echo.simulation import EchoStat
from src.task.EnhanceEchoTask import EnhanceEchoTask


MAIN_STAT_OPTIONS = {
    4: {
        "Crit Rate": "crit_rate",
        "Crit Damage": "crit_damage",
        "Attack %": "attack_percent",
        "Defense %": "defense_percent",
        "HP %": "hp_percent",
        "Healing Bonus": "healing_bonus",
    },
    3: {
        "Glacio DMG Bonus": "glacio_damage",
        "Fusion DMG Bonus": "fusion_damage",
        "Electro DMG Bonus": "electro_damage",
        "Aero DMG Bonus": "aero_damage",
        "Spectro DMG Bonus": "spectro_damage",
        "Havoc DMG Bonus": "havoc_damage",
        "Attack %": "attack_percent",
        "Defense %": "defense_percent",
        "HP %": "hp_percent",
        "Energy Regen": "energy_regen",
    },
    1: {
        "Attack %": "attack_percent",
        "Defense %": "defense_percent",
        "HP %": "hp_percent",
    },
}

WEIGHT_CONFIG = {
    "Weight Crit Rate": (EchoStat.CRIT_RATE, 1.8),
    "Weight Crit Damage": (EchoStat.CRIT_DAMAGE, 0.9),
    "Weight Attack %": (EchoStat.ATTACK_PERCENT, 0.9),
    "Weight Defense %": (EchoStat.DEFENSE_PERCENT, 0.0),
    "Weight HP %": (EchoStat.HP_PERCENT, 0.0),
    "Weight Flat Attack": (EchoStat.ATTACK_FLAT, 0.1),
    "Weight Flat Defense": (EchoStat.DEFENSE_FLAT, 0.0),
    "Weight Flat HP": (EchoStat.HP_FLAT, 0.0),
    "Weight Energy Regen": (EchoStat.ENERGY_REGEN, 0.5),
    "Weight Basic Attack": (EchoStat.BASIC_ATTACK_DAMAGE, 0.0),
    "Weight Heavy Attack": (EchoStat.HEAVY_ATTACK_DAMAGE, 0.675),
    "Weight Resonance Skill": (EchoStat.RESONANCE_SKILL_DAMAGE, 0.0),
    "Weight Resonance Liberation": (
        EchoStat.RESONANCE_LIBERATION_DAMAGE, 0.0,
    ),
}


@dataclass(frozen=True)
class EchoInventoryCheckContext:
    sonata_set: str
    build: NamedEchoBuild
    candidate_pool: tuple[EchoIdentity, ...]
    policy: PolicyConfig
    output_dir: str | None = None


class CheckEchoInventoryTask(EnhanceEchoTask):
    """Read-only inventory check that produces the input for step 2."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "① 检查套装与声骸（不强化）"
        self.description = (
            "严格选择并读回角色对应合鸣套装，设置等级从高到低后扫描满级声骸；"
            "只生成供用户检查的结果文件，不进入培养。"
        )
        self._echo_check_context: EchoInventoryCheckContext | None = None
        try:
            character_options = list(echo_character_template_names(
                DEFAULT_ECHO_CHARACTER_TEMPLATE
            ))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            character_options = ["秧秧·玄翎"]
        initial_main_stats = {
            4: "Crit Damage",
            3: "Havoc DMG Bonus",
            1: "Attack %",
        }
        initial_weights = {
            key: default for key, (_, default) in WEIGHT_CONFIG.items()
        }
        try:
            initial_template = load_echo_character_template(
                DEFAULT_ECHO_CHARACTER_TEMPLATE,
                character_options[0],
            )
            for cost in (1, 3, 4):
                echo = next(
                    item for item in initial_template.build.echoes
                    if item.identity.cost == cost
                )
                main_stat = echo.identity.compatibility.rsplit(":", 1)[-1]
                initial_main_stats[cost] = next(
                    label for label, code in MAIN_STAT_OPTIONS[cost].items()
                    if code == main_stat
                )
            initial_weights = {
                key: initial_template.build.profile.weights[stat]
                for key, (stat, _) in WEIGHT_CONFIG.items()
            }
        except (OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            pass
        self.default_config = {
            "Template File": "echo_character_templates.json",
            "Character Template": character_options[0],
            "4C Main Stat": initial_main_stats[4],
            "3C Main Stat": initial_main_stats[3],
            "1C Main Stat": initial_main_stats[1],
            "Weight Crit Rate": initial_weights["Weight Crit Rate"],
            "Weight Crit Damage": initial_weights["Weight Crit Damage"],
            "Weight Attack %": initial_weights["Weight Attack %"],
            "Weight Defense %": initial_weights["Weight Defense %"],
            "Weight HP %": initial_weights["Weight HP %"],
            "Weight Flat Attack": initial_weights["Weight Flat Attack"],
            "Weight Flat Defense": initial_weights["Weight Flat Defense"],
            "Weight Flat HP": initial_weights["Weight Flat HP"],
            "Weight Energy Regen": initial_weights["Weight Energy Regen"],
            "Weight Basic Attack": initial_weights["Weight Basic Attack"],
            "Weight Heavy Attack": initial_weights["Weight Heavy Attack"],
            "Weight Resonance Skill": initial_weights["Weight Resonance Skill"],
            "Weight Resonance Liberation": initial_weights[
                "Weight Resonance Liberation"
            ],
            "Output Directory": "artifacts/echo_inventory/checks",
            "COSTs": ["1", "3", "4"],
            "Max Pages Per COST": 20,
            "Scroll Count": 20,
            "Settle Seconds": 3.5,
        }
        self.config_type = {
            "Character Template": {
                "type": "drop_down",
                "options": character_options,
            },
            "4C Main Stat": {
                "type": "drop_down",
                "options": [
                    "Crit Rate", "Crit Damage", "Attack %", "Defense %",
                    "HP %", "Healing Bonus",
                ],
            },
            "3C Main Stat": {
                "type": "drop_down",
                "options": [
                    "Glacio DMG Bonus", "Fusion DMG Bonus",
                    "Electro DMG Bonus", "Aero DMG Bonus",
                    "Spectro DMG Bonus", "Havoc DMG Bonus", "Attack %",
                    "Defense %", "HP %", "Energy Regen",
                ],
            },
            "1C Main Stat": {
                "type": "drop_down",
                "options": ["Attack %", "Defense %", "HP %"],
            },
            "COSTs": {
                "type": "multi_selection",
                "options": ["1", "3", "4"],
            },
        }
        self.config_description = {
            "Template File": (
                "角色默认模板 JSON；相对路径按 OK-WW 项目根目录解析"
            ),
            "Character Template": (
                "选择角色及其套装、当前五件、候选声骸和模型策略"
            ),
            "4C Main Stat": "本次检查接受的 4C 主属性",
            "3C Main Stat": "本次检查接受的 3C 主属性",
            "1C Main Stat": "本次检查接受的 1C 主属性",
            "Weight Crit Rate": "暴击副属性每一点的模型权重",
            "Weight Crit Damage": "暴击伤害副属性每一点的模型权重",
            "Weight Attack %": "百分比攻击副属性每一点的模型权重",
            "Weight Defense %": "百分比防御副属性每一点的模型权重",
            "Weight HP %": "百分比生命副属性每一点的模型权重",
            "Weight Flat Attack": "固定攻击副属性每一点的模型权重",
            "Weight Flat Defense": "固定防御副属性每一点的模型权重",
            "Weight Flat HP": "固定生命副属性每一点的模型权重",
            "Weight Energy Regen": "共鸣效率副属性每一点的模型权重",
            "Weight Basic Attack": "普攻伤害副属性每一点的模型权重",
            "Weight Heavy Attack": "重击伤害副属性每一点的模型权重",
            "Weight Resonance Skill": "共鸣技能伤害副属性每一点的模型权重",
            "Weight Resonance Liberation": (
                "共鸣解放伤害副属性每一点的模型权重"
            ),
            "Output Directory": "检查结果根目录；每次运行会创建独立时间目录",
            "COSTs": "要检查的 COST 页签",
            "Max Pages Per COST": "每个 COST 的安全页数上限",
            "Scroll Count": "每次向下滚动的格数",
            "Settle Seconds": "切换、点击或滚动后的等待秒数",
        }

    @staticmethod
    def _project_path(value: object) -> Path:
        raw = str(value or "echo_character_templates.json").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = DEFAULT_ECHO_CHARACTER_TEMPLATE.parent / path
        return path.resolve()

    def _configured_value(self, key: str) -> object:
        config = getattr(self, "config", None)
        value = config.get(key) if config is not None else None
        return self.default_config[key] if value is None else value

    def validate_config(self, key, value):
        if key == "Template File":
            try:
                echo_character_template_names(self._project_path(value))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                return f"角色模板 JSON 无法读取：{error}"
        elif key == "Character Template":
            # Config invokes validators while it is still constructing itself,
            # before assigning task.config.  Cross-field validation is only
            # possible after that point; run() performs the authoritative load.
            if getattr(self, "config", None) is None:
                return None
            try:
                names = echo_character_template_names(self._project_path(
                    self._configured_value("Template File")
                ))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                return f"角色模板 JSON 无法读取：{error}"
            if str(value) not in names:
                return "所选角色不在角色模板 JSON 中"
        elif key in {"4C Main Stat", "3C Main Stat", "1C Main Stat"}:
            cost = int(key[0])
            if str(value) not in MAIN_STAT_OPTIONS[cost]:
                return f"COST {cost} 主属性无效"
        elif key in WEIGHT_CONFIG:
            try:
                weight = float(value)
            except (TypeError, ValueError):
                return "副属性权重必须是数字"
            if not math.isfinite(weight) or weight < 0:
                return "副属性权重必须是有限的非负数"
        elif key == "COSTs":
            values = tuple(str(item) for item in (value or ()))
            if not values or any(item not in {"1", "3", "4"} for item in values):
                return "至少选择一个有效 COST"
        return None

    def _context_from_ui_config(self) -> EchoInventoryCheckContext:
        main_stats = {
            cost: MAIN_STAT_OPTIONS[cost][str(self._configured_value(
                f"{cost}C Main Stat"
            ))]
            for cost in (1, 3, 4)
        }
        weights = {
            stat: float(self._configured_value(key))
            for key, (stat, _) in WEIGHT_CONFIG.items()
        }
        template = load_echo_character_template(
            self._project_path(self._configured_value("Template File")),
            str(self._configured_value("Character Template")),
            main_stat_overrides=main_stats,
            weight_overrides=weights,
        )
        self.info_set("角色模板", template.character)
        self.info_set("目标套装", template.sonata_set)
        self.info_set("当前模型分", round(template.build.score, 4))
        return EchoInventoryCheckContext(
            template.sonata_set,
            template.build,
            template.candidate_pool,
            template.policy,
        )

    def configure_echo_check(
            self, *, sonata_set: str, build: NamedEchoBuild,
            candidate_pool: Iterable[EchoIdentity] = (),
            policy: PolicyConfig | None = None,
            output_dir: str | None = None) -> None:
        """Supply character/model data before the user starts step 1."""

        pool = tuple(candidate_pool)
        self._echo_check_context = EchoInventoryCheckContext(
            sonata_set,
            build,
            pool,
            policy or PolicyConfig(),
            output_dir,
        )

    @staticmethod
    def reconcile_equipped_identities(
            build: NamedEchoBuild,
            items: Iterable[EchoInventoryItem]) -> NamedEchoBuild:
        """Replace stale fixture names with unique exact inventory matches.

        Equipped substat states are already part of the user-supplied current
        build.  When exactly one scanned item has the same COST and five exact
        public rolls, its OCR name/main-stat compatibility is a safer identity
        than an old report label.  Ambiguous or missing matches are left alone.
        """

        inventory = tuple(items)
        reconciled: list[NamedEquippedEcho] = []
        for echo in build.echoes:
            matches = {
                item.identity for item in inventory
                if item.identity.cost == echo.identity.cost
                and item.state == echo.state
            }
            identity = next(iter(matches)) if len(matches) == 1 else echo.identity
            reconciled.append(NamedEquippedEcho(echo.slot, identity, echo.state))
        return NamedEchoBuild(build.profile, build.base_panel, tuple(reconciled))

    @staticmethod
    def canonicalize_truncated_names(
            scan: EchoInventoryScanResult) -> EchoInventoryScanResult:
        """Merge an OCR-truncated name with its unique longer overlap reading."""

        canonicalized: list[EchoInventoryItem] = []
        source = scan.items
        for item in source:
            longer = {
                other.identity for other in source
                if other.identity.cost == item.identity.cost
                and other.identity.compatibility == item.identity.compatibility
                and other.state == item.state
                and len(other.identity.name) > len(item.identity.name)
                and other.identity.name.startswith(item.identity.name)
            }
            if len(longer) == 1:
                item = replace(item, identity=next(iter(longer)))
            canonicalized.append(item)

        unique: list[EchoInventoryItem] = []
        keys: set[tuple[object, ...]] = set()
        merged = 0
        for item in canonicalized:
            if item.attribute_key in keys:
                merged += 1
                continue
            keys.add(item.attribute_key)
            unique.append(item)
        if not merged:
            return scan
        return EchoInventoryScanResult(
            tuple(unique),
            scan.issues,
            scan.pages_scanned,
            scan.duplicate_fingerprints + merged,
            scan.stop_reason,
        )

    def run(self) -> EchoInventoryCheckResult:
        context = self._echo_check_context
        if context is None:
            context = self._context_from_ui_config()

        configured_root = context.output_dir or str(
            self.config.get("Output Directory") or "artifacts/echo_inventory/checks"
        )
        root = Path(configured_root).expanduser().resolve()
        if context.output_dir is None:
            root /= datetime.now().strftime("%Y%m%d_%H%M%S")
        selected_costs = tuple(
            int(cost) for cost in (self.config.get("COSTs") or ["1", "3", "4"])
        )
        analysis = self.analyze_character_echo_inventory(
            sonata_set=context.sonata_set,
            build=context.build,
            output_dir=root,
            costs=selected_costs,
            minimum_gain=context.policy.minimum_gain,
            required_stats=context.policy.required_stats,
            max_pages_per_cost=int(self.config.get("Max Pages Per COST") or 20),
            settle_time=float(self.config.get("Settle Seconds") or 3.5),
            scroll_count=int(self.config.get("Scroll Count") or 20),
        )
        canonical_scan = self.canonicalize_truncated_names(analysis.scan)
        if canonical_scan is not analysis.scan:
            analysis = EchoCharacterInventoryAnalysis(
                analysis.sonata_set,
                canonical_scan,
                canonical_scan.rank_replacements(
                    context.build,
                    minimum_gain=context.policy.minimum_gain,
                    required_stats=context.policy.required_stats,
                ),
                analysis.report,
            )
        reconciled_build = self.reconcile_equipped_identities(
            context.build,
            analysis.scan.items,
        )
        if reconciled_build != context.build:
            replacements = analysis.scan.rank_replacements(
                reconciled_build,
                minimum_gain=context.policy.minimum_gain,
                required_stats=context.policy.required_stats,
            )
            analysis = EchoCharacterInventoryAnalysis(
                analysis.sonata_set,
                analysis.scan,
                replacements,
                analysis.report,
            )
            self.info_set("已校正装备名称", sum(
                before.identity != after.identity
                for before, after in zip(
                    context.build.echoes,
                    reconciled_build.echoes,
                    strict=True,
                )
            ))
        Path(analysis.report).write_text(
            json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        candidate_pool = context.candidate_pool
        if not candidate_pool:
            candidate_pool = tuple(sorted(
                {
                    item.identity for item in analysis.scan.items
                    if reconciled_build.compatible_slots(item.identity)
                },
                key=lambda identity: (
                    identity.cost,
                    identity.compatibility,
                    identity.name,
                ),
            ))
        if not candidate_pool:
            raise RuntimeError("扫描结果中没有与当前五件套兼容的候选类型")
        result = EchoInventoryCheckResult.from_analysis(
            analysis,
            build=reconciled_build,
            candidate_pool=candidate_pool,
            policy=context.policy,
        )
        result_path = result.write(root / "echo_inventory_check.json")
        self.info_set("检查结果", str(result_path))
        self.info_set("待检视提升项", result.replacement_count)
        self.log_info(
            f"检查完成，请先检视 {result_path}；确认后再手动运行第二步强化",
            notify=True,
        )
        return EchoInventoryCheckResult.read(result_path)
