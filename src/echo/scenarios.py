"""Reproducible example scenarios for Echo-model validation."""

from __future__ import annotations

from dataclasses import dataclass

from src.echo.optimizer import EchoIdentity, NamedEchoBuild, NamedEquippedEcho
from src.echo.panel import (
    CharacterPanel,
    CharacterScoringProfile,
    EnergyRegenPolicy,
    MainDpsWeightClass,
)
from src.echo.simulation import EchoRoll, EchoState, EchoStat


@dataclass(frozen=True)
class Scenario:
    name: str
    build: NamedEchoBuild
    candidate_pool: tuple[EchoIdentity, ...]
    assumptions: tuple[str, ...]


def _echo(*rolls: tuple[EchoStat, float]) -> EchoState:
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in rolls))


def xuanling_scenario(*, heavy_damage_coverage: float = 0.75,
                      required_er: float = 130.0,
                      comfort_er: float = 135.0,
                      overflow_er: float = 145.0,
                      weight_class: MainDpsWeightClass | None = None) -> Scenario:
    """The Xuanling set transcribed from the supplied WutheringWavesUID report."""

    energy_policy = EnergyRegenPolicy(
        required_er,
        comfort_er,
        overflow_er,
        tolerance_weight_ratio=5 / 9,
    )
    if weight_class is None:
        profile = CharacterScoringProfile.normalized_main_dps(
            "xuanling_normalized_main_dps",
            damage_coverage={
                EchoStat.BASIC_ATTACK_DAMAGE: 0.0,
                EchoStat.HEAVY_ATTACK_DAMAGE: heavy_damage_coverage,
                EchoStat.RESONANCE_SKILL_DAMAGE: 0.0,
                EchoStat.RESONANCE_LIBERATION_DAMAGE: 0.0,
            },
            energy_policy=energy_policy,
        )
        scoring_assumption = f"重击伤害覆盖率={heavy_damage_coverage:.1%}"
    else:
        weight_class = MainDpsWeightClass(weight_class)
        profile = CharacterScoringProfile.classified_main_dps(
            f"xuanling_{weight_class.value}",
            weight_class=weight_class,
            primary_damage_stat=EchoStat.HEAVY_ATTACK_DAMAGE,
            energy_policy=energy_policy,
            energy_is_required=True,
        )
        scoring_assumption = (
            "1类：双暴>大攻击=重击>小攻击"
            if weight_class == MainDpsWeightClass.TYPE_1
            else "2类：双暴>大攻击>重击=小攻击"
        )

    four_a = EchoIdentity("4C-A/千偃重楼", 4, "crit_damage_main")
    three_a = EchoIdentity("3C-A/雾息兽尊", 3, "havoc_damage_main")
    three_b = EchoIdentity("3C-B/封庭栖凰", 3, "havoc_damage_main")
    one_a = EchoIdentity("1C-A/不熄猎手", 1, "attack_percent_main")
    one_b = EchoIdentity("1C-B/雾息兽尊·身", 1, "attack_percent_main")
    one_c = EchoIdentity("1C-C/未装备类型", 1, "attack_percent_main")
    one_d = EchoIdentity("1C-D/未装备类型", 1, "attack_percent_main")
    one_e = EchoIdentity("1C-E/未装备类型", 1, "attack_percent_main")

    echoes = (
        NamedEquippedEcho(
            "4C", four_a,
            _echo(
                (EchoStat.CRIT_RATE, 6.9),
                (EchoStat.CRIT_DAMAGE, 15.0),
                (EchoStat.HP_PERCENT, 8.6),
                (EchoStat.DEFENSE_FLAT, 50),
                (EchoStat.ATTACK_PERCENT, 9.4),
            ),
        ),
        NamedEquippedEcho(
            "3C-1", three_a,
            _echo(
                (EchoStat.ENERGY_REGEN, 8.4),
                (EchoStat.ATTACK_FLAT, 40),
                (EchoStat.CRIT_RATE, 8.1),
                (EchoStat.RESONANCE_LIBERATION_DAMAGE, 6.4),
                (EchoStat.CRIT_DAMAGE, 17.4),
            ),
        ),
        NamedEquippedEcho(
            "3C-2", three_b,
            _echo(
                (EchoStat.HEAVY_ATTACK_DAMAGE, 6.4),
                (EchoStat.CRIT_RATE, 6.3),
                (EchoStat.ENERGY_REGEN, 9.2),
                (EchoStat.CRIT_DAMAGE, 13.8),
                (EchoStat.RESONANCE_LIBERATION_DAMAGE, 10.1),
            ),
        ),
        NamedEquippedEcho(
            "1C-1", one_a,
            _echo(
                (EchoStat.BASIC_ATTACK_DAMAGE, 8.6),
                (EchoStat.CRIT_DAMAGE, 12.6),
                (EchoStat.RESONANCE_LIBERATION_DAMAGE, 9.4),
                (EchoStat.HEAVY_ATTACK_DAMAGE, 9.4),
                (EchoStat.CRIT_RATE, 8.7),
            ),
        ),
        NamedEquippedEcho(
            "1C-2", one_b,
            _echo(
                (EchoStat.CRIT_DAMAGE, 13.8),
                (EchoStat.CRIT_RATE, 7.5),
                (EchoStat.RESONANCE_LIBERATION_DAMAGE, 11.6),
                (EchoStat.ENERGY_REGEN, 10.0),
                (EchoStat.BASIC_ATTACK_DAMAGE, 8.6),
            ),
        ),
    )

    # Visible per-Echo ER lines total 27.6%, while the report's final panel is
    # 137.6%.  The residual non-replaceable panel is therefore represented as
    # 110%.  This also covers a possible hidden/global 10% source in the report.
    build = NamedEchoBuild(profile, CharacterPanel.base(energy_regen=110), echoes)
    pool = (four_a, three_a, three_b, one_a, one_b, one_c, one_d, one_e)
    assumptions = (
        f"{scoring_assumption}；技能/解放/普攻权重=0",
        f"效率必需/容错/溢出阈值={required_er:g}/{comfort_er:g}/{overflow_er:g}%",
        "效率在必需线前等同大攻击；必需线后等同小攻击并在容错区递减",
        "截图逐件效率合计27.6%；不可替换效率按110%计，以复现总面板137.6%",
        "合格胚子无限；4C有1种名称、3C有2种名称、1C有5种名称",
        "候选均已满足对应兼容组的套装和主词条要求",
        "成功替换后保留旧声骸，不将其作为材料回收",
    )
    return Scenario("玄翎43311", build, pool, assumptions)
