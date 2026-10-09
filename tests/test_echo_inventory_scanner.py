import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from ok.feature.Box import Box

from src.echo import (
    EchoIdentity,
    EchoInventoryScanResult,
    EnhancementAction,
    PolicyConfig,
    parse_echo_inventory_item,
)
from src.task.EnhanceEchoTask import (
    ECHO_INVENTORY_COST_TABS,
    ECHO_INVENTORY_GRID_COLUMNS,
    ECHO_INVENTORY_GRID_ROWS,
    EnhanceEchoTask,
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


def completed_echo():
    return parse_echo_inventory_item(
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


class FakeInventoryScanner:
    scan_filtered_echo_inventory = EnhanceEchoTask.scan_filtered_echo_inventory

    def __init__(self):
        self.item = completed_echo()
        self.clicked = []
        self.selected = []
        self.scrolls = []

    def click_relative(self, x, y, after_sleep=0):
        self.clicked.append((x, y, after_sleep))

    def _read_echo_card_level(self, column_index, row_index):
        cell_index = row_index * 3 + column_index
        if cell_index == 0:
            return 25
        if cell_index == 1:
            return 0
        if cell_index == 2:
            return None
        return 0

    def _read_selected_echo_inventory_item(self, **kwargs):
        self.selected.append(kwargs)
        return self.item

    def scroll_relative(self, x, y, count):
        self.scrolls.append((x, y, count))

    def sleep(self, seconds):
        pass

    def info_set(self, key, value):
        pass

    def log_warning(self, message):
        pass

    def log_info(self, message):
        pass


class TestEchoInventoryScanner(unittest.TestCase):
    def test_detects_optional_capture_title_bar_offset(self):
        client_frame = np.full((100, 160, 3), 40, dtype=np.uint8)
        titled_frame = client_frame.copy()
        titled_frame[1:6] = 250

        self.assertEqual(
            EnhanceEchoTask._echo_inventory_capture_y_offset(client_frame),
            0.0,
        )
        self.assertAlmostEqual(
            EnhanceEchoTask._echo_inventory_capture_y_offset(titled_frame),
            0.06,
        )

    def test_stops_immediately_when_identical_details_repeat(self):
        scanner = FakeInventoryScanner()
        with tempfile.TemporaryDirectory() as directory:
            result = scanner.scan_filtered_echo_inventory(
                output_dir=directory,
                costs=(4,),
                max_pages_per_cost=2,
                settle_time=0,
            )

            manifest = json.loads(
                (Path(directory) / "inventory_scan.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(scanner.selected), 2)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.duplicate_fingerprints, 1)
        self.assertEqual(result.pages_scanned, 1)
        self.assertEqual(
            result.stop_reason,
            "COST 4: repeated identical Echo at page 1, cell 2, 千傀重楼, +25",
        )
        self.assertEqual(scanner.scrolls, [])
        self.assertEqual(len(manifest["items"]), 1)
        self.assertEqual(manifest["duplicate_fingerprints"], 1)
        self.assertEqual(manifest["items"][0]["substats"][0]["tier_index"], 1)

    def test_repeat_stop_key_includes_level(self):
        class MixedLevelScanner(FakeInventoryScanner):
            def _read_selected_echo_inventory_item(self, **kwargs):
                self.selected.append(kwargs)
                level = 25 if kwargs["cell_index"] == 0 else 20
                return replace(self.item, level=level)

        scanner = MixedLevelScanner()
        with tempfile.TemporaryDirectory() as directory:
            result = scanner.scan_filtered_echo_inventory(
                output_dir=directory,
                costs=(4,),
                completed_only=False,
                max_pages_per_cost=2,
                settle_time=0,
            )

        self.assertEqual(len(scanner.selected), 3)
        self.assertEqual(result.pages_scanned, 1)
        self.assertEqual(result.duplicate_fingerprints, 2)
        self.assertEqual(
            result.stop_reason,
            "COST 4: repeated identical Echo at page 1, cell 3, 千傀重楼, +20",
        )

    def test_selects_and_verifies_character_sonata_set(self):
        class FakeSonataSelector:
            select_echo_sonata_set = EnhanceEchoTask.select_echo_sonata_set
            _read_selected_sonata_set = EnhanceEchoTask._read_selected_sonata_set
            _read_sonata_filter_header = (
                EnhanceEchoTask._read_sonata_filter_header
            )
            _read_sonata_option_boxes = EnhanceEchoTask._read_sonata_option_boxes
            _sonata_filter_is_open = EnhanceEchoTask._sonata_filter_is_open
            _click_capture_box_relative = (
                EnhanceEchoTask._click_capture_box_relative
            )
            _normalize_sonata_set_name = staticmethod(
                EnhanceEchoTask._normalize_sonata_set_name
            )
            _echo_inventory_capture_y_offset = staticmethod(
                EnhanceEchoTask._echo_inventory_capture_y_offset
            )

            def __init__(self):
                self.frame = np.full((100, 160, 3), 40, dtype=np.uint8)
                self.selected = "全部"
                self.open = False
                self.clicked_relative = []
                self.clicked_options = []
                self.info = {}
                self.apply_selection = True

            def next_frame(self):
                pass

            def ocr(self, x, y, to_x, to_y, **kwargs):
                if to_y <= 0.19:
                    label = (
                        "合鸣筛选/全部" if self.selected == "全部"
                        else self.selected
                    )
                    return [Box(0, 10, 10, 10, name=label)]
                if self.open:
                    return [
                        Box(0, 30, 10, 10, name="羽落空尘之歌"),
                        Box(0, 50, 10, 10, name="冥途夜行之灯"),
                    ]
                return []

            def click_relative(self, x, y, after_sleep=0):
                self.clicked_relative.append((x, y, after_sleep))
                if y < 0.25:
                    self.open = not self.open
                else:
                    if self.apply_selection:
                        self.selected = "羽落空尘之歌"
                    self.clicked_options.append(self.selected)

            def scroll_relative(self, x, y, count):
                raise AssertionError("visible target should not scroll")

            def sleep(self, seconds):
                pass

            def info_set(self, key, value):
                self.info[key] = value

            def log_info(self, message):
                pass

        selector = FakeSonataSelector()
        selected = selector.select_echo_sonata_set(
            "羽落空尘之歌", settle_time=0
        )

        self.assertEqual(selected, "羽落空尘之歌")
        self.assertEqual(selector.clicked_options, ["羽落空尘之歌"])
        self.assertEqual(len(selector.clicked_relative), 3)
        self.assertFalse(selector.open)
        self.assertEqual(selector.info["目标合鸣套装"], "羽落空尘之歌")

        rejected = FakeSonataSelector()
        rejected.apply_selection = False
        with self.assertRaisesRegex(RuntimeError, "选择校验失败"):
            rejected.select_echo_sonata_set("羽落空尘之歌", settle_time=0)

        already_selected = FakeSonataSelector()
        already_selected.selected = "羽落空尘之歌"
        self.assertEqual(
            already_selected.select_echo_sonata_set(
                "羽落空尘之歌", settle_time=0
            ),
            "羽落空尘之歌",
        )
        self.assertEqual(already_selected.clicked_relative, [])

    def test_character_analysis_selects_set_before_scanning(self):
        class NoImprovementBuild:
            def best_replacement(self, *args, **kwargs):
                return None

        class FakeCharacterAnalyzer:
            analyze_character_echo_inventory = (
                EnhanceEchoTask.analyze_character_echo_inventory
            )

            def __init__(self):
                self.calls = []
                self.info = {}

            def select_echo_sonata_set(self, sonata_set, **kwargs):
                self.calls.append(("select", sonata_set))
                return sonata_set

            def scan_filtered_echo_inventory(self, **kwargs):
                self.calls.append(("scan", kwargs["costs"]))
                return EchoInventoryScanResult(
                    (completed_echo(),), (), 1, 0, "limit"
                )

            def select_echo_inventory_level_descending(self, **kwargs):
                self.calls.append(("sort", "level_descending"))
                return "等级顺序"

            def info_set(self, key, value):
                self.info[key] = value

            def log_info(self, message):
                pass

        analyzer = FakeCharacterAnalyzer()
        with tempfile.TemporaryDirectory() as directory:
            analysis = analyzer.analyze_character_echo_inventory(
                sonata_set="羽落空尘之歌",
                build=NoImprovementBuild(),
                output_dir=directory,
                costs=(4,),
                settle_time=0,
            )
            report = json.loads(
                (Path(directory) / "character_inventory_analysis.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(
            analyzer.calls,
            [
                ("select", "羽落空尘之歌"),
                ("sort", "level_descending"),
                ("scan", (4,)),
            ],
        )
        self.assertEqual(analysis.sonata_set, "羽落空尘之歌")
        self.assertIsNone(analysis.best_replacement)
        self.assertEqual(report["sonata_set"], "羽落空尘之歌")
        self.assertEqual(report["scan"]["items"][0]["name"], "千傀重楼")

    def test_skips_equipped_levelled_cards_before_selecting_fresh_echo(self):
        class FakeFreshSelector:
            _select_first_fresh_echo = EnhanceEchoTask._select_first_fresh_echo

            def __init__(self):
                self.clicks = []
                self.info = {}
                self._echo_enhancement_session = None
                self._selected_model_candidate_identity = None

            def _read_echo_card_level(self, column_index, row_index):
                if (column_index, row_index) == (0, 0):
                    return 25
                if (column_index, row_index) == (1, 0):
                    return 0
                return None

            def click_relative(self, x, y, after_sleep=0):
                self.clicks.append((x, y, after_sleep))

            def _read_selected_echo_level(self):
                return 0

            def info_set(self, key, value):
                self.info[key] = value

            def log_warning(self, *args, **kwargs):
                pass

            def log_info(self, *args, **kwargs):
                pass

        selector = FakeFreshSelector()
        level = selector._select_first_fresh_echo(settle_time=0)

        self.assertEqual(level, 0)
        self.assertEqual(
            selector.clicks,
            [(0.35, 0.917, 0), (0.35, 0.917, 0),
             (ECHO_INVENTORY_GRID_COLUMNS[1], ECHO_INVENTORY_GRID_ROWS[0], 0)],
        )
        self.assertEqual(selector.info["当前声骸等级"], 0)

    def test_skips_fresh_echo_with_wrong_model_main_stat(self):
        eligible = EchoIdentity("可用声骸", 1, "cost_1:attack_percent")
        wrong = EchoIdentity("错误主属性", 1, "cost_1:hp_percent")

        class FakeBuild:
            def compatible_slots(self, identity):
                return ("1C-1",) if identity == eligible else ()

            def best_possible_replacement(self, identity, **kwargs):
                return SimpleNamespace(slot="1C-1", delta=1.0, final_score=1.0)

        class FakeSession:
            candidate_pool = (eligible,)
            build = FakeBuild()
            config = PolicyConfig(rollout_samples=10)

        class FakeSelector:
            _select_first_fresh_echo = EnhanceEchoTask._select_first_fresh_echo

            def __init__(self):
                self._echo_enhancement_session = FakeSession()
                self._selected_model_candidate_identity = None
                self.selected = -1
                self.info = {}
                self.logs = []

            def _read_echo_card_level(self, column_index, row_index):
                return 0 if row_index == 0 and column_index < 2 else None

            def click_relative(self, x, y, after_sleep=0):
                if y in ECHO_INVENTORY_GRID_ROWS:
                    self.selected += 1

            def _read_selected_echo_level(self):
                return 0

            def _read_current_echo_candidate_identity(self):
                return (wrong, eligible)[self.selected]

            def info_set(self, key, value):
                self.info[key] = value

            def log_warning(self, message):
                self.logs.append(message)

            def log_info(self, message):
                self.logs.append(message)

        selector = FakeSelector()

        self.assertEqual(selector._select_first_fresh_echo(settle_time=0), 0)
        self.assertEqual(selector._selected_model_candidate_identity, eligible)
        self.assertEqual(selector.info["跳过不兼容+0声骸"], 1)
        self.assertTrue(any("错误主属性" in line for line in selector.logs))

    def test_scrolls_for_eligible_fresh_echo_below_wrong_main_stats(self):
        eligible = EchoIdentity("可用声骸", 1, "cost_1:attack_percent")
        wrong = EchoIdentity("错误主属性", 1, "cost_1:hp_percent")

        class FakeBuild:
            def compatible_slots(self, identity):
                return ("1C-1",) if identity == eligible else ()

            def best_possible_replacement(self, identity, **kwargs):
                return SimpleNamespace(slot="1C-1", delta=1.0, final_score=1.0)

        class FakeSession:
            candidate_pool = (eligible,)
            build = FakeBuild()
            config = PolicyConfig(rollout_samples=10)

        class FakePagedSelector:
            _select_first_fresh_echo = EnhanceEchoTask._select_first_fresh_echo

            def __init__(self):
                self._echo_enhancement_session = FakeSession()
                self._selected_model_candidate_identity = None
                self.config = {
                    "Max Candidate Search Pages": 3,
                    "Candidate Search Scroll Count": 10,
                }
                self.page = 0
                self.info = {}
                self.scrolls = []

            def _read_echo_card_level(self, column_index, row_index):
                return 0 if (column_index, row_index) == (0, 0) else None

            def click_relative(self, *args, **kwargs):
                pass

            def _read_selected_echo_level(self):
                return 0

            def _read_current_echo_candidate_identity(self):
                return wrong if self.page == 0 else eligible

            def scroll_relative(self, x, y, count):
                self.scrolls.append((x, y, count))
                self.page += 1

            def sleep(self, seconds):
                pass

            def info_set(self, key, value):
                self.info[key] = value

            def log_warning(self, message):
                pass

            def log_info(self, message):
                pass

        selector = FakePagedSelector()

        self.assertEqual(selector._select_first_fresh_echo(settle_time=0), 0)
        self.assertEqual(selector._selected_model_candidate_identity, eligible)
        self.assertEqual(selector.info["候选搜索页"], 2)
        self.assertEqual(selector.scrolls, [(0.25, 0.70, -10)])

    def test_scrolls_past_complete_pages_before_first_fresh_echo(self):
        eligible = EchoIdentity("可用声骸", 1, "cost_1:attack_percent")

        class FakeBuild:
            def compatible_slots(self, identity):
                return ("1C-1",) if identity == eligible else ()

            def best_possible_replacement(self, identity, **kwargs):
                return SimpleNamespace(slot="1C-1", delta=1.0, final_score=1.0)

        class FakeSession:
            candidate_pool = (eligible,)
            build = FakeBuild()
            config = PolicyConfig(rollout_samples=10)

        class FakePagedSelector:
            _select_first_fresh_echo = EnhanceEchoTask._select_first_fresh_echo

            def __init__(self):
                self._echo_enhancement_session = FakeSession()
                self._selected_model_candidate_identity = None
                self.config = {
                    "Max Candidate Search Pages": 4,
                    "Candidate Search Scroll Count": 10,
                }
                self.page = 0
                self.info = {}
                self.scrolls = []

            def _read_echo_card_level(self, column_index, row_index):
                if self.page < 2:
                    return 25
                return 0 if (column_index, row_index) == (0, 0) else None

            def click_relative(self, *args, **kwargs):
                pass

            def _read_selected_echo_level(self):
                return 0

            def _read_current_echo_candidate_identity(self):
                return eligible

            def scroll_relative(self, x, y, count):
                self.scrolls.append((x, y, count))
                self.page += 1

            def sleep(self, seconds):
                pass

            def info_set(self, key, value):
                self.info[key] = value

            def log_warning(self, message):
                pass

            def log_info(self, message):
                pass

        selector = FakePagedSelector()

        self.assertEqual(selector._select_first_fresh_echo(settle_time=0), 0)
        self.assertEqual(selector._selected_model_candidate_identity, eligible)
        self.assertEqual(selector.info["候选搜索页"], 3)
        self.assertEqual(
            selector.scrolls,
            [(0.25, 0.70, -10), (0.25, 0.70, -10)],
        )

    def test_resets_viewport_after_processing_same_cost(self):
        eligible = EchoIdentity("可用声骸", 1, "cost_1:attack_percent")

        class FakeBuild:
            def compatible_slots(self, identity):
                return ("1C-1",)

            def best_possible_replacement(self, identity, **kwargs):
                return SimpleNamespace(slot="1C-1", delta=1.0, final_score=1.0)

        class FakeSession:
            candidate_pool = (eligible,)
            build = FakeBuild()
            config = PolicyConfig(rollout_samples=10)

        class FakeSelector:
            _select_first_fresh_echo = EnhanceEchoTask._select_first_fresh_echo

            def __init__(self):
                self._echo_enhancement_session = FakeSession()
                self._selected_model_candidate_identity = None
                self._active_candidate_cost = None
                self.config = {}
                self.clicks = []
                self.info = {}
                self.logs = []
                self.at_enhanced_echo = False
                self.sort_ascending = True

            def _read_echo_card_level(self, column_index, row_index):
                if self.at_enhanced_echo:
                    return 25
                return 0 if (column_index, row_index) == (0, 0) else None

            def click_relative(self, x, y, after_sleep=0):
                self.clicks.append((x, y))
                if (x, y) == (0.35, 0.917):
                    self.sort_ascending = not self.sort_ascending
                    self.at_enhanced_echo = False

            def _read_selected_echo_level(self):
                return 0

            def _read_current_echo_candidate_identity(self):
                return eligible

            def info_set(self, key, value):
                self.info[key] = value

            def log_warning(self, message):
                self.logs.append(message)

            def log_info(self, message):
                self.logs.append(message)

        selector = FakeSelector()

        self.assertEqual(selector._select_first_fresh_echo(settle_time=0), 0)
        # 3.7 returns to the enhanced item's position, with fresh candidates
        # above the viewport. Searching only downward would miss them.
        selector.at_enhanced_echo = True
        self.assertEqual(selector._select_first_fresh_echo(settle_time=0), 0)

        self.assertEqual(selector.clicks.count(ECHO_INVENTORY_COST_TABS[1]), 1)
        self.assertEqual(selector.clicks.count((0.35, 0.917)), 4)
        self.assertTrue(selector.sort_ascending)
        self.assertEqual(selector._selected_model_candidate_identity, eligible)
        self.assertEqual(
            selector.info["候选搜索起点"],
            "切换两次升降序并回到顶部",
        )

    def test_candidate_limit_counts_successes_and_failures(self):
        class FakeLimitedTask:
            _candidate_limit_reached = EnhanceEchoTask._candidate_limit_reached
            config = {"Max Candidates Per Run": 1}
            info = {"成功声骸数量": 0, "失败声骸数量": 1}

            def info_get(self, key):
                return self.info[key]

        self.assertTrue(FakeLimitedTask()._candidate_limit_reached())

    def test_publishes_each_live_model_decision_to_info_and_log(self):
        class FakeDecisionTask:
            _publish_enhancement_decision = (
                EnhanceEchoTask._publish_enhancement_decision
            )

            def __init__(self):
                self.info = {}
                self.logs = []

            def info_set(self, key, value):
                self.info[key] = value

            def log_info(self, message):
                self.logs.append(message)

        task = FakeDecisionTask()
        task._publish_enhancement_decision(SimpleNamespace(
            action=EnhancementAction.MARK_DISCARD,
            current_slots=4,
            reason="completion efficiency is below fresh-candidate opportunity cost",
            metrics=None,
        ))

        self.assertEqual(task.info["模型决策"], "标记弃置")
        self.assertEqual(task.info["模型决策阶段"], "+20")
        self.assertIn("动作=标记弃置", task.logs[0])

    def test_selects_and_reads_back_level_descending_sort(self):
        class FakeSortSelector:
            select_echo_inventory_level_descending = (
                EnhanceEchoTask.select_echo_inventory_level_descending
            )

            def __init__(self):
                self.frame = np.full((100, 160, 3), 40, dtype=np.uint8)
                self.labels = [None, "等级顺序"]
                self.clicks = []
                self.info = {}

            def _read_echo_inventory_sort_label(self):
                return self.labels.pop(0)

            def click_relative(self, x, y, after_sleep=0):
                self.clicks.append(("button", x, y))

            def next_frame(self):
                pass

            def ocr(self, *args, **kwargs):
                return [
                    Box(20, 20, 20, 10, name="等级顺序"),
                    Box(80, 80, 20, 10, name="确认"),
                ]

            def _click_capture_box_relative(self, box, after_sleep=0):
                self.clicks.append(("ocr", box.name))

            def _normalize_sonata_set_name(self, value):
                return EnhanceEchoTask._normalize_sonata_set_name(value)

            def info_set(self, key, value):
                self.info[key] = value

            def log_info(self, message):
                pass

        selector = FakeSortSelector()
        selected = selector.select_echo_inventory_level_descending(settle_time=0)

        self.assertEqual(selected, "等级顺序")
        self.assertEqual(
            [click[1] for click in selector.clicks if click[0] == "ocr"],
            ["等级顺序", "确认"],
        )
        self.assertEqual(selector.info["声骸排序"], "等级顺序")

    def test_stops_after_scrolled_page_with_only_parse_issues(self):
        class IssueAfterFirstPageScanner(FakeInventoryScanner):
            def _read_selected_echo_inventory_item(self, **kwargs):
                self.selected.append(kwargs)
                if kwargs["page_index"] > 0:
                    raise ValueError("unreadable details")
                identity = EchoIdentity(
                    f"测试声骸{kwargs['cell_index']}",
                    self.item.identity.cost,
                    self.item.identity.compatibility,
                )
                return replace(self.item, identity=identity)

        scanner = IssueAfterFirstPageScanner()
        with tempfile.TemporaryDirectory() as directory:
            result = scanner.scan_filtered_echo_inventory(
                output_dir=directory,
                costs=(4,),
                max_pages_per_cost=5,
                settle_time=0,
            )

        self.assertEqual(result.pages_scanned, 2)
        self.assertEqual(len(result.items), 15)
        self.assertEqual(len(result.issues), 2)
        self.assertEqual(
            result.stop_reason,
            "COST 4: first non-+25 Echo at page 2, cell 2, +0 "
            "(card level after detail OCR failure)",
        )
        self.assertEqual(scanner.scrolls, [(0.25, 0.70, -20)])

    def test_stops_cost_at_first_non_25_detail_without_scrolling(self):
        class DescendingScanner(FakeInventoryScanner):
            def _read_selected_echo_inventory_item(self, **kwargs):
                self.selected.append(kwargs)
                if kwargs["cell_index"] == 1:
                    return replace(self.item, level=20)
                return self.item

        scanner = DescendingScanner()
        with tempfile.TemporaryDirectory() as directory:
            result = scanner.scan_filtered_echo_inventory(
                output_dir=directory,
                costs=(4,),
                max_pages_per_cost=5,
                settle_time=0,
            )

        self.assertEqual(result.pages_scanned, 1)
        self.assertEqual(len(scanner.selected), 2)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(scanner.scrolls, [])
        self.assertEqual(
            result.stop_reason,
            "COST 4: first non-+25 Echo at page 1, cell 2, +20 (details)",
        )

if __name__ == "__main__":
    unittest.main()
