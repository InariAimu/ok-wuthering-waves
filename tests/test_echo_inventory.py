import unittest
from dataclasses import replace

from src.echo import (
    CharacterPanel,
    CharacterScoringProfile,
    EchoIdentity,
    EchoMainStat,
    EchoInventoryScanResult,
    EchoRoll,
    EchoState,
    EchoStat,
    NamedEchoBuild,
    NamedEquippedEcho,
    pair_echo_stat_rows,
    parse_echo_inventory_item,
)


def token(text, x, y, width=60, height=12, confidence=0.99):
    return {
        "text": text,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": confidence,
    }


class TestEchoInventoryOcr(unittest.TestCase):
    def test_parses_level_merged_with_long_echo_name(self):
        item = parse_echo_inventory_item(
            (
                token("共鸣回响·芙露德莉斯 +25", 0, 0, 180),
                token("COST 4", 0, 28),
            ),
            (
                token("暴击伤害", 0, 0), token("44.0%", 150, 0),
                token("攻击", 0, 24), token("150", 150, 24),
            ),
            (
                token("暴击伤害", 0, 0), token("17.4%", 150, 0),
                token("防御", 0, 24), token("50", 150, 24),
                token("共鸣解放伤害加成", 0, 48, 130), token("9.4%", 150, 48),
                token("共鸣技能伤害加成", 0, 72, 130), token("10.9%", 150, 72),
                token("共鸣效率", 0, 96), token("10.8%", 150, 96),
            ),
            cost_hint=4,
            page_index=0,
            cell_index=0,
        )

        self.assertEqual(item.identity.name, "共鸣回响·芙露德莉斯")
        self.assertEqual(item.level, 25)

    def test_pairs_split_and_merged_stat_rows(self):
        rows = pair_echo_stat_rows((
            token("+", 0, 10, 5),
            token("暴击", 10, 10),
            token("6.9%", 120, 11),
            token("暴击伤害 15.0%", 10, 35, 150),
            token("攻击", 10, 60),
            token("50", 120, 61),
        ))

        self.assertEqual(
            [(row.stat, row.value) for row in rows],
            [
                (EchoStat.CRIT_RATE, 6.9),
                (EchoStat.CRIT_DAMAGE, 15.0),
                (EchoStat.ATTACK_FLAT, 50.0),
            ],
        )

    def test_parses_full_inventory_detail_into_model_item(self):
        identity = (
            token("千傀重楼", 0, 0),
            token("+25", 180, 0),
            token("COST", 0, 28),
            token("4", 55, 28, 10),
        )
        main = (
            token("暴击伤害", 0, 0), token("44.0%", 150, 0),
            token("攻击", 0, 24), token("150", 150, 24),
        )
        sub = (
            token("暴击", 0, 0), token("6.9%", 150, 0),
            token("暴击伤害", 0, 24), token("15.0%", 150, 24),
            token("生命", 0, 48), token("8.6%", 150, 48),
            token("防御", 0, 72), token("50", 150, 72),
            token("攻击", 0, 96), token("9.4%", 150, 96),
        )

        item = parse_echo_inventory_item(
            identity,
            main,
            sub,
            cost_hint=4,
            page_index=1,
            cell_index=2,
            screenshot="scan.png",
        )

        self.assertEqual(item.identity.name, "千傀重楼")
        self.assertEqual(item.identity.cost, 4)
        self.assertEqual(item.identity.compatibility, "cost_4:crit_damage")
        self.assertEqual(item.level, 25)
        self.assertEqual(item.state.stats, frozenset({
            EchoStat.CRIT_RATE,
            EchoStat.CRIT_DAMAGE,
            EchoStat.HP_PERCENT,
            EchoStat.DEFENSE_FLAT,
            EchoStat.ATTACK_PERCENT,
        }))
        self.assertEqual(item.screenshot, "scan.png")
        self.assertEqual(item.fingerprint, item.fingerprint)

    def test_preserves_elemental_primary_stat_in_compatibility_group(self):
        item = parse_echo_inventory_item(
            (
                token("封庭械囿", 0, 0),
                token("+25", 180, 0),
                token("COST 3", 0, 28),
            ),
            (
                token("湮灭伤害加成", 0, 0, 110),
                token("30.0%", 150, 0),
                token("攻击", 0, 24),
                token("100", 150, 24),
            ),
            (
                token("重击伤害加成", 0, 0, 110), token("6.4%", 150, 0),
                token("暴击", 0, 24), token("6.3%", 150, 24),
                token("共鸣效率", 0, 48), token("9.2%", 150, 48),
                token("暴击伤害", 0, 72), token("13.8%", 150, 72),
                token("共鸣解放伤害加成", 0, 96, 130), token("10.1%", 150, 96),
            ),
            cost_hint=3,
            page_index=0,
            cell_index=0,
        )

        self.assertEqual(item.main_stats[0].stat, EchoMainStat.HAVOC_DAMAGE)
        self.assertEqual(item.identity.compatibility, "cost_3:havoc_damage")

    def test_existence_key_uses_canonical_structured_attributes(self):
        item = parse_echo_inventory_item(
            (
                token("千傀重楼", 0, 0),
                token("+25", 180, 0),
                token("COST 4", 0, 28),
            ),
            (
                token("暴击伤害", 0, 0), token("44.0%", 150, 0),
                token("攻击", 0, 24), token("150", 150, 24),
            ),
            (
                token("暴击", 0, 0), token("6.9%", 150, 0),
                token("暴击伤害", 0, 24), token("15.0%", 150, 24),
                token("生命", 0, 48), token("8.6%", 150, 48),
                token("防御", 0, 72), token("50", 150, 72),
                token("攻击", 0, 96), token("9.4%", 150, 96),
            ),
            cost_hint=4,
            page_index=0,
            cell_index=0,
        )
        reordered = replace(
            item,
            level=20,
            main_stats=(
                item.main_stats[0],
                replace(item.main_stats[1], value=999.0),
            ),
            state=EchoState(tuple(reversed(item.state.rolls))),
            page_index=9,
            cell_index=14,
            screenshot="other.png",
        )

        self.assertEqual(item.attribute_key, reordered.attribute_key)
        self.assertEqual(item.fingerprint, reordered.fingerprint)

    def test_ranks_unique_inventory_replacements_by_maximum_gain(self):
        item = parse_echo_inventory_item(
            (
                token("千傀重楼", 0, 0),
                token("+25", 180, 0),
                token("COST 4", 0, 28),
            ),
            (
                token("暴击伤害", 0, 0), token("44.0%", 150, 0),
                token("攻击", 0, 24), token("150", 150, 24),
            ),
            (
                token("暴击", 0, 0), token("6.9%", 150, 0),
                token("暴击伤害", 0, 24), token("15.0%", 150, 24),
                token("生命", 0, 48), token("8.6%", 150, 48),
                token("防御", 0, 72), token("50", 150, 72),
                token("攻击", 0, 96), token("9.4%", 150, 96),
            ),
            cost_hint=4,
            page_index=0,
            cell_index=0,
        )
        old_identity = EchoIdentity(
            "旧声骸",
            4,
            item.identity.compatibility,
        )
        build = NamedEchoBuild(
            CharacterScoringProfile("crit-only", {EchoStat.CRIT_RATE: 1.0}),
            CharacterPanel.base(),
            (NamedEquippedEcho(
                "cost_4_slot",
                old_identity,
                EchoState((EchoRoll.from_value(EchoStat.HP_FLAT, 320),)),
            ),),
        )
        duplicate_overlap = replace(
            item,
            page_index=1,
            cell_index=3,
            screenshot="overlap.png",
        )
        better_item = replace(
            item,
            identity=EchoIdentity(
                "更优声骸",
                4,
                item.identity.compatibility,
            ),
            state=EchoState((
                EchoRoll.from_value(EchoStat.CRIT_RATE, 10.5),
                *item.state.rolls[1:],
            )),
            cell_index=4,
        )
        incomplete_item = replace(
            better_item,
            identity=EchoIdentity(
                "未满级声骸",
                4,
                item.identity.compatibility,
            ),
            level=10,
            state=EchoState(better_item.state.rolls[:2]),
            cell_index=5,
        )
        scan_result = EchoInventoryScanResult(
            (item, duplicate_overlap, better_item, incomplete_item),
            (),
            2,
            1,
            "bottom",
        )

        replacements = scan_result.rank_replacements(build)

        self.assertEqual(len(replacements), 2)
        self.assertEqual(replacements[0].item.identity.name, "更优声骸")
        self.assertEqual(replacements[0].option.slot, "cost_4_slot")
        self.assertAlmostEqual(replacements[0].option.delta, 10.5)
        self.assertEqual(replacements[1].item.fingerprint, item.fingerprint)
        self.assertAlmostEqual(replacements[1].option.delta, 6.9)

    def test_rejects_missing_primary_main_stat_instead_of_using_fixed_stat(self):
        with self.assertRaisesRegex(ValueError, "expected 2 Echo main stats"):
            parse_echo_inventory_item(
                (
                    token("封庭械囿", 0, 0),
                    token("+25", 180, 0),
                    token("COST 3", 0, 28),
                ),
                (token("攻击", 0, 24), token("100", 150, 24)),
                (
                    token("重击伤害加成", 0, 0, 110), token("6.4%", 150, 0),
                    token("暴击", 0, 24), token("6.3%", 150, 24),
                    token("共鸣效率", 0, 48), token("9.2%", 150, 48),
                    token("暴击伤害", 0, 72), token("13.8%", 150, 72),
                    token("共鸣解放伤害加成", 0, 96, 130), token("10.1%", 150, 96),
                ),
                cost_hint=3,
                page_index=0,
                cell_index=0,
            )

    def test_rejects_incomplete_plus_25_detail(self):
        with self.assertRaisesRegex(ValueError, "expected 5 substats"):
            parse_echo_inventory_item(
                (token("不熄猎手", 0, 0), token("+25", 100, 0),
                 token("COST", 0, 20), token("1", 50, 20)),
                (
                    token("攻击", 0, 0), token("18.0%", 100, 0),
                    token("生命", 0, 24), token("2280", 100, 24),
                ),
                (token("暴击", 0, 0), token("8.7%", 100, 0)),
                cost_hint=1,
                page_index=0,
                cell_index=0,
            )

    def test_rejects_cost_tab_mismatch(self):
        with self.assertRaisesRegex(ValueError, "COST mismatch"):
            parse_echo_inventory_item(
                (token("封庭械囿", 0, 0), token("+0", 100, 0),
                 token("COST", 0, 20), token("3", 50, 20)),
                (token("湮灭伤害加成", 0, 0), token("30.0%", 100, 0)),
                (),
                cost_hint=1,
                page_index=0,
                cell_index=0,
            )


if __name__ == "__main__":
    unittest.main()
