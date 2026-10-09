import unittest
from unittest.mock import patch

from src.echo.character_templates import (
    DEFAULT_ECHO_CHARACTER_TEMPLATE,
    echo_character_template_names,
    load_echo_character_template,
)
from src.echo.simulation import EchoStat
from src.task.CheckEchoInventoryTask import (
    CheckEchoInventoryTask,
    WEIGHT_CONFIG,
)
from src.task.EnhanceEchoTask import EnhanceEchoTask


class EchoCharacterTemplateTests(unittest.TestCase):
    def test_task_ui_defaults_are_loaded_from_default_json(self):
        with patch.object(EnhanceEchoTask, "__init__", return_value=None):
            task = CheckEchoInventoryTask()

        self.assertEqual(task.default_config["Character Template"], "秧秧·玄翎")
        self.assertEqual(task.default_config["4C Main Stat"], "Crit Damage")
        self.assertEqual(task.default_config["3C Main Stat"], "Havoc DMG Bonus")
        self.assertEqual(task.default_config["1C Main Stat"], "Attack %")
        self.assertEqual(task.default_config["Weight Crit Rate"], 1.8)
        self.assertIn(
            "秧秧·玄翎",
            task.config_type["Character Template"]["options"],
        )

    def test_character_validation_is_safe_before_config_is_assigned(self):
        with patch.object(EnhanceEchoTask, "__init__", return_value=None):
            task = CheckEchoInventoryTask()
        task.config = None

        self.assertIsNone(
            task.validate_config("Character Template", "秧秧·玄翎")
        )

    def test_default_template_builds_reviewed_xuanling_context(self):
        self.assertIn(
            "秧秧·玄翎",
            echo_character_template_names(DEFAULT_ECHO_CHARACTER_TEMPLATE),
        )

        context = load_echo_character_template(
            DEFAULT_ECHO_CHARACTER_TEMPLATE,
            "秧秧·玄翎",
        )

        self.assertEqual(context.sonata_set, "羽落空尘之歌")
        self.assertEqual(
            {echo.slot for echo in context.build.echoes},
            {"4C", "3C-1", "3C-2", "1C-1", "1C-2"},
        )
        self.assertEqual(len(context.candidate_pool), 7)
        self.assertEqual(
            next(echo for echo in context.build.echoes if echo.slot == "1C-2")
            .identity.name,
            "霁息兽尊·身",
        )
        for identity in context.candidate_pool:
            self.assertTrue(context.build.compatible_slots(identity))

    def test_main_stat_and_weight_overrides_apply_to_build_and_pool(self):
        context = load_echo_character_template(
            DEFAULT_ECHO_CHARACTER_TEMPLATE,
            "秧秧·玄翎",
            main_stat_overrides={
                4: "crit_rate",
                3: "energy_regen",
                1: "hp_percent",
            },
            weight_overrides={EchoStat.HEAVY_ATTACK_DAMAGE: 1.25},
        )

        expected = {
            4: "cost_4:crit_rate",
            3: "cost_3:energy_regen",
            1: "cost_1:hp_percent",
        }
        for echo in context.build.echoes:
            self.assertEqual(
                echo.identity.compatibility,
                expected[echo.identity.cost],
            )
        for identity in context.candidate_pool:
            self.assertEqual(identity.compatibility, expected[identity.cost])
        self.assertEqual(
            context.build.profile.weights[EchoStat.HEAVY_ATTACK_DAMAGE],
            1.25,
        )

    def test_ui_config_constructs_context_without_programmatic_injection(self):
        task = CheckEchoInventoryTask.__new__(CheckEchoInventoryTask)
        task.default_config = {
            "Template File": "echo_character_templates.json",
            "Character Template": "秧秧·玄翎",
            "4C Main Stat": "Crit Damage",
            "3C Main Stat": "Havoc DMG Bonus",
            "1C Main Stat": "Attack %",
            **{key: default for key, (_, default) in WEIGHT_CONFIG.items()},
        }
        task.config = dict(task.default_config)
        task.info = {}
        task.info_set = lambda key, value: task.info.__setitem__(key, value)

        context = task._context_from_ui_config()

        self.assertEqual(context.sonata_set, "羽落空尘之歌")
        self.assertEqual(len(context.build.echoes), 5)
        self.assertEqual(task.info["角色模板"], "秧秧·玄翎")
        self.assertEqual(task.info["目标套装"], "羽落空尘之歌")
        self.assertIsInstance(task.info["当前模型分"], float)


if __name__ == "__main__":
    unittest.main()
