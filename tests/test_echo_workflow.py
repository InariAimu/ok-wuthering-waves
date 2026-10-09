import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.echo import (
    CharacterPanel,
    CharacterScoringProfile,
    EchoCharacterInventoryAnalysis,
    EchoEnhancementSession,
    EchoIdentity,
    EchoInventoryCheckResult,
    EchoInventoryItem,
    EchoInventoryScanResult,
    EchoRoll,
    EchoResourceSnapshot,
    EchoState,
    EchoStat,
    NamedEchoBuild,
    NamedEquippedEcho,
    ObservedEchoStat,
    PolicyConfig,
)
from src.task.CheckEchoInventoryTask import (
    CheckEchoInventoryTask,
    EchoInventoryCheckContext,
)
from src.task.EnhanceEchoTask import EnhanceEchoTask
from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask


def sample_build():
    old = EchoIdentity("旧声骸", 1, "cost_1:attack_percent")
    build = NamedEchoBuild(
        CharacterScoringProfile(
            "test",
            {
                EchoStat.CRIT_RATE: 1.8,
                EchoStat.CRIT_DAMAGE: 0.9,
                EchoStat.ATTACK_PERCENT: 0.9,
            },
            energy_regen_target=130,
        ),
        CharacterPanel.base(
            energy_regen=110,
            values={EchoStat.ATTACK_PERCENT: 20},
        ),
        (
            NamedEquippedEcho(
                "1C-1",
                old,
                EchoState((EchoRoll.from_value(EchoStat.HP_FLAT, 320),)),
            ),
        ),
    )
    pool = (
        EchoIdentity("新声骸", 1, "cost_1:attack_percent"),
        EchoIdentity("另一声骸", 1, "cost_1:attack_percent"),
    )
    return build, pool


def sample_five_echo_build():
    build, pool = sample_build()
    echoes = tuple(
        NamedEquippedEcho(
            slot,
            EchoIdentity(name, cost, compatibility),
            EchoState((EchoRoll.from_value(stat, value),)),
        )
        for slot, name, cost, compatibility, stat, value in (
            ("4C", "四费", 4, "cost_4:crit_damage", EchoStat.CRIT_RATE, 6.3),
            ("3C-1", "三费甲", 3, "cost_3:havoc_damage", EchoStat.CRIT_DAMAGE, 12.6),
            ("3C-2", "三费乙", 3, "cost_3:havoc_damage", EchoStat.ENERGY_REGEN, 6.8),
            ("1C-1", "一费甲", 1, "cost_1:attack_percent", EchoStat.ATTACK_PERCENT, 6.4),
            ("1C-2", "一费乙", 1, "cost_1:attack_percent", EchoStat.ATTACK_FLAT, 30),
        )
    )
    return NamedEchoBuild(build.profile, build.base_panel, echoes), pool


class TestEchoCheckHandoff(unittest.TestCase):
    def test_round_trips_reviewed_model_input(self):
        build, pool = sample_build()
        analysis = EchoCharacterInventoryAnalysis(
            "羽落空尘之歌",
            EchoInventoryScanResult((), (), 3, 2, "end"),
            (),
            "analysis.json",
        )
        policy = PolicyConfig(
            rollout_samples=123,
            minimum_gain=1.25,
            required_stats=frozenset({EchoStat.CRIT_RATE}),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "echo_inventory_check.json"
            written = EchoInventoryCheckResult.from_analysis(
                analysis,
                build=build,
                candidate_pool=pool,
                policy=policy,
            ).write(path)
            loaded = EchoInventoryCheckResult.read(written)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(raw["kind"], "ok-ww.echo-inventory-check")
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(loaded.sonata_set, "羽落空尘之歌")
        self.assertEqual(loaded.build, build)
        self.assertEqual(loaded.candidate_pool, pool)
        self.assertEqual(loaded.policy, policy)
        self.assertEqual(loaded.inspection["scan"]["pages_scanned"], 3)
        offline_session = EchoEnhancementSession(
            loaded.build,
            loaded.candidate_pool,
            resources=EchoResourceSnapshot.manual(
                tuners=100,
                echo_exp=500_000,
            ),
            config=loaded.policy,
        )
        self.assertEqual(offline_session.candidate_pool, pool)

    def test_rejects_wrong_schema_before_enhancement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.json"
            path.write_text(
                json.dumps({"kind": "other", "schema_version": 1}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not an OK-WW"):
                EchoInventoryCheckResult.read(path)

    def test_rejects_candidate_without_a_compatible_equipped_slot(self):
        build, _ = sample_build()
        analysis = EchoCharacterInventoryAnalysis(
            "羽落空尘之歌",
            EchoInventoryScanResult((), (), 1, 0, "end"),
            (),
        )
        with self.assertRaisesRegex(ValueError, "no compatible slot"):
            EchoInventoryCheckResult.from_analysis(
                analysis,
                build=build,
                candidate_pool=(EchoIdentity("不兼容", 4, "cost_4:crit_rate"),),
            )

    def test_step_one_only_calls_analysis_and_writes_handoff(self):
        build, pool = sample_build()

        class FakeCheckTask:
            run = CheckEchoInventoryTask.run
            reconcile_equipped_identities = staticmethod(
                CheckEchoInventoryTask.reconcile_equipped_identities
            )
            canonicalize_truncated_names = staticmethod(
                CheckEchoInventoryTask.canonicalize_truncated_names
            )

            def __init__(self, output_dir):
                self._echo_check_context = EchoInventoryCheckContext(
                    "羽落空尘之歌",
                    build,
                    pool,
                    PolicyConfig(rollout_samples=10),
                    output_dir,
                )
                self.config = {
                    "COSTs": ["1", "3"],
                    "Max Pages Per COST": 4,
                    "Scroll Count": 10,
                    "Settle Seconds": 0,
                }
                self.calls = []
                self.info = {}

            def analyze_character_echo_inventory(self, **kwargs):
                self.calls.append(kwargs)
                return EchoCharacterInventoryAnalysis(
                    kwargs["sonata_set"],
                    EchoInventoryScanResult((), (), 1, 0, "end"),
                    (),
                    str(Path(kwargs["output_dir"]) / "analysis.json"),
                )

            def info_set(self, key, value):
                self.info[key] = value

            def log_info(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as directory:
            task = FakeCheckTask(directory)
            result = task.run()
            result_path = Path(task.info["检查结果"])
            self.assertTrue(result_path.exists())

        self.assertEqual(len(task.calls), 1)
        self.assertEqual(task.calls[0]["costs"], (1, 3))
        self.assertEqual(result.candidate_pool, pool)

    def test_step_one_uses_ui_context_when_not_programmatically_configured(self):
        build, pool = sample_build()

        class FakeCheckTask:
            run = CheckEchoInventoryTask.run
            reconcile_equipped_identities = staticmethod(
                CheckEchoInventoryTask.reconcile_equipped_identities
            )
            canonicalize_truncated_names = staticmethod(
                CheckEchoInventoryTask.canonicalize_truncated_names
            )

            def __init__(self, output_dir):
                self._echo_check_context = None
                self.ui_context_calls = 0
                self.context = EchoInventoryCheckContext(
                    "羽落空尘之歌",
                    build,
                    pool,
                    PolicyConfig(rollout_samples=10),
                    output_dir,
                )
                self.config = {
                    "COSTs": ["1"],
                    "Max Pages Per COST": 1,
                    "Scroll Count": 1,
                    "Settle Seconds": 0,
                }
                self.info = {}

            def _context_from_ui_config(self):
                self.ui_context_calls += 1
                return self.context

            def analyze_character_echo_inventory(self, **kwargs):
                return EchoCharacterInventoryAnalysis(
                    kwargs["sonata_set"],
                    EchoInventoryScanResult((), (), 1, 0, "end"),
                    (),
                    str(Path(kwargs["output_dir"]) / "analysis.json"),
                )

            def info_set(self, key, value):
                self.info[key] = value

            def log_info(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as directory:
            task = FakeCheckTask(directory)
            task.run()

        self.assertEqual(task.ui_context_calls, 1)
        self.assertIn("检查结果", task.info)

    def test_reconciles_stale_equipped_name_from_exact_scanned_rolls(self):
        build, _ = sample_build()
        scanned_identity = EchoIdentity(
            "OCR实际名称", 1, "cost_1:attack_percent"
        )
        item = type("ScannedItem", (), {
            "identity": scanned_identity,
            "state": build.echoes[0].state,
        })()

        reconciled = CheckEchoInventoryTask.reconcile_equipped_identities(
            build,
            (item,),
        )

        self.assertEqual(reconciled.echoes[0].identity, scanned_identity)

    def test_merges_unique_longer_name_for_identical_overlap(self):
        build, _ = sample_build()
        state = build.echoes[0].state
        main_stats = (
            ObservedEchoStat(
                EchoStat.ATTACK_PERCENT, 18.0, "攻击", "18.0%"
            ),
            ObservedEchoStat(EchoStat.HP_FLAT, 456, "生命", "456"),
        )
        short = EchoInventoryItem(
            EchoIdentity("霁息兽尊·", 1, "cost_1:attack_percent"),
            25,
            main_stats,
            state,
            0,
            0,
        )
        full = replace(
            short,
            identity=EchoIdentity(
                "霁息兽尊·身", 1, "cost_1:attack_percent"
            ),
            page_index=1,
        )
        scan = EchoInventoryScanResult((short, full), (), 2, 0, "end")

        merged = CheckEchoInventoryTask.canonicalize_truncated_names(scan)

        self.assertEqual(len(merged.items), 1)
        self.assertEqual(merged.items[0].identity.name, "霁息兽尊·身")
        self.assertEqual(merged.duplicate_fingerprints, 1)

    def test_step_two_loads_reviewed_file_without_inventory_analysis(self):
        build, pool = sample_five_echo_build()
        analysis = EchoCharacterInventoryAnalysis(
            "羽落空尘之歌",
            EchoInventoryScanResult((), (), 1, 0, "end"),
            (),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = EchoInventoryCheckResult.from_analysis(
                analysis,
                build=build,
                candidate_pool=pool,
                policy=PolicyConfig(rollout_samples=10),
            ).write(Path(directory) / "echo_inventory_check.json")

            task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
            task._echo_enhancement_session = None
            task._echo_enhancement_context = None
            task._echo_candidate_provider = None
            task._session_is_external = False
            task._selected_sonata_set = None
            task._loaded_inspection_result = None
            task.config = {
                "Inspection Result File": str(path),
                "Simulation Only": False,
            }
            task.info_set = lambda *args: None
            task.log_info = lambda *args, **kwargs: None
            selected_sets = []
            task.select_echo_sonata_set = lambda value, **kwargs: selected_sets.append(value)
            task.analyze_character_echo_inventory = lambda **kwargs: self.fail(
                "step 2 must not scan or analyze inventory"
            )

            with patch.object(EnhanceEchoTask, "run", return_value="enhanced") as run:
                value = ModelEnhanceEchoTask.run(task)

        self.assertEqual(value, "enhanced")
        self.assertEqual(task._echo_enhancement_context.build, build)
        self.assertEqual(task._echo_enhancement_context.candidate_pool, pool)
        self.assertEqual(task._selected_sonata_set, "羽落空尘之歌")
        self.assertEqual(selected_sets, ["羽落空尘之歌"])
        run.assert_called_once_with()

    def test_step_two_preflight_publishes_five_slots_panel_and_best_position(self):
        build, pool = sample_five_echo_build()
        inspection = {
            "sonata_set": "羽落空尘之歌",
            "replacements": [
                {
                    "item": {
                        "name": "提升声骸",
                        "cost": 1,
                        "compatibility": "cost_1:attack_percent",
                    },
                    "replacement": {
                        "slot": "1C-1",
                        "delta": 2.975,
                        "final_score": build.score + 2.975,
                        "energy_feasible": True,
                    },
                }
            ],
        }
        result = EchoInventoryCheckResult(
            "羽落空尘之歌",
            build,
            pool,
            PolicyConfig(rollout_samples=10),
            inspection,
            "now",
            "checked.json",
        )
        task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
        task.config = {"Simulation Only": True}
        task.info = {}
        task.info_set = lambda key, value: task.info.__setitem__(key, value)
        task.log_info = lambda *args, **kwargs: None

        task._publish_preflight(result)

        for slot in ("4C", "3C-1", "3C-2", "1C-1", "1C-2"):
            self.assertIn(f"当前位置 {slot}", task.info)
        self.assertIn("主属性 暴击伤害", task.info["当前位置 4C"])
        self.assertIn("暴击 6.3%", task.info["当前位置 4C"])
        self.assertIn("共鸣效率", task.info["当前模型总属性"])
        self.assertEqual(task.info["目前最大提升位置"], "1C-1")
        self.assertEqual(task.info["目前最大提升分数"], 2.975)
        self.assertEqual(
            task.info["目标COST分页"],
            "COST 1（来自最大提升位置 1C-1）",
        )

    def test_step_two_rejects_incomplete_current_build(self):
        build, pool = sample_build()
        result = EchoInventoryCheckResult(
            "羽落空尘之歌",
            build,
            pool,
            PolicyConfig(rollout_samples=10),
            {"sonata_set": "羽落空尘之歌", "replacements": []},
            "now",
            "checked.json",
        )
        task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
        task.config = {"Simulation Only": True}

        with self.assertRaisesRegex(RuntimeError, "当前五个位置"):
            task._publish_preflight(result)

    def test_step_two_simulation_writes_status_and_log_without_game_actions(self):
        build, pool = sample_five_echo_build()
        analysis = EchoCharacterInventoryAnalysis(
            "羽落空尘之歌",
            EchoInventoryScanResult((), (), 1, 0, "end"),
            (),
        )
        with tempfile.TemporaryDirectory() as directory:
            check_path = EchoInventoryCheckResult.from_analysis(
                analysis,
                build=build,
                candidate_pool=pool,
                policy=PolicyConfig(rollout_samples=10),
            ).write(Path(directory) / "echo_inventory_check.json")
            output = Path(directory) / "simulation"
            task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
            task._echo_enhancement_session = None
            task._echo_enhancement_context = None
            task._echo_candidate_provider = None
            task._session_is_external = False
            task._selected_sonata_set = None
            task._loaded_inspection_result = None
            task.config = {
                "Inspection Result File": str(check_path),
                "Simulation Only": True,
                "Simulation Attempts": 2,
                "Simulation Rollout Samples": 10,
                "Simulation Tuners": 100,
                "Simulation Premium Echo EXP Tubes": 30,
                "Simulation Output Directory": str(output),
            }
            task.info = {}
            task.logs = []
            task.info_set = lambda key, value: task.info.__setitem__(key, value)
            task.log_info = lambda message, **kwargs: task.logs.append(message)
            task.select_echo_sonata_set = lambda *args, **kwargs: self.fail(
                "simulation must not select or click a Sonata set"
            )

            with patch.object(
                    EnhanceEchoTask,
                    "run",
                    side_effect=AssertionError("simulation must not run live enhancement")):
                campaign = ModelEnhanceEchoTask.run(task)

            report = Path(task.info["模拟报告"])
            raw = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(task.info["运行模式"], "模拟（不会操作游戏）")
        self.assertEqual(task.info["模拟状态"], "完成")
        self.assertEqual(raw["kind"], "ok-ww.echo-enhancement-simulation")
        self.assertFalse(raw["safety"]["game_ui_touched"])
        self.assertFalse(raw["safety"]["game_resources_consumed"])
        self.assertEqual(raw["summary"]["attempts"], campaign.attempts)
        event_logs = [line for line in task.logs if line.startswith("[模拟强化 #")]
        self.assertEqual(len(event_logs), len(campaign.events))


if __name__ == "__main__":
    unittest.main()
