# EnhanceEchoTask.py
import re
import time
import os
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import cv2

from ok import FindFeature, Logger
from ok.feature.Box import get_bounding_box
from src.echo.api import (
    EchoEnhancementSession,
    EchoResourceSnapshot,
    EnhancementAction,
    ResourceProviderLike,
    echo_state_from_ocr_pairs,
    resolve_echo_resources,
)
from src.echo.inventory import (
    EchoCharacterInventoryAnalysis,
    EchoInventoryScanIssue,
    EchoInventoryScanResult,
    match_echo_name_tokens,
    pair_echo_stat_rows,
    parse_echo_inventory_item,
)
from src.echo.name_catalog import EchoNameRecognitionError
from src.echo.optimizer import EchoIdentity, NamedEchoBuild, PolicyConfig, ResourceLedger
from src.echo.simulation import EchoState, EchoStat, ResourceCostModel
from src.scene.WWScene import WWScene
from src.task.BaseWWTask import BaseWWTask

logger = Logger.get_logger(__name__)

number_pattern = re.compile(r"^[\d.%％ ]+$")
property_pattern = re.compile(r"[\u4e00-\u9fff]{2,}")

ECHO_INVENTORY_COST_TABS = {
    1: (0.191, 0.074),
    3: (0.239, 0.074),
    4: (0.288, 0.074),
}
ECHO_INVENTORY_GRID_COLUMNS = (0.140, 0.210, 0.280)
# Click positions are relative to the game client. The title bar captured by
# BitBlt is not part of the PostMessage client coordinate system.
ECHO_INVENTORY_GRID_ROWS = (0.225, 0.382, 0.537, 0.692, 0.847)
# OCR positions are relative to the game client. Some capture modes prepend a
# Windows title bar, which is detected and added at runtime. Card-level OCR is
# only a cheap +25 pre-filter; unreadable cells are still clicked and validated
# against the right-hand details panel.
ECHO_INVENTORY_LEVEL_ROWS = (0.255, 0.406, 0.555, 0.704, 0.852)
ECHO_INVENTORY_LEVEL_RE = re.compile(r"(?:\+|\b)(\d{1,2})\s*$")
ECHO_SELECTED_LEVEL_RE = re.compile(r"[+＋]\s*(\d{1,2})")
ECHO_SELECTED_COST_RE = re.compile(r"^[134]$")
ECHO_SONATA_HEADER_BOX = (0.07, 0.03, 0.33, 0.17)
ECHO_SONATA_OPTION_BOX = (0.13, 0.18, 0.31, 0.86)
ECHO_INVENTORY_SORT_BUTTON = (0.205, 0.925)
ECHO_INVENTORY_SORT_LABEL_BOX = (0.10, 0.86, 0.32, 0.99)


@dataclass(frozen=True)
class EchoEnhancementContext:
    """Model inputs supplied by ok-ww's character/build reader."""

    build: NamedEchoBuild
    candidate_pool: tuple[EchoIdentity, ...]
    candidate_provider: Callable[["EnhanceEchoTask"], EchoIdentity] | None = None
    config: PolicyConfig | None = None
    cost_model: ResourceCostModel | None = None


class NoEligibleFreshEchoError(RuntimeError):
    """The reviewed target COST has no compatible +0 candidate."""


class EnhanceEchoTask(BaseWWTask, FindFeature):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "⬆️ 批量强化声骸(游戏与okww语言必须为简体/繁体中文)"
        self.description = "点击B进入背包, 在过滤器中选择需要强化的声骸, 并按照等级从0排序后开始."
        self.fail_reason = ""
        self.supported_languages = ["zh_CN", "zh_TW"]
        self._echo_resource_provider: ResourceProviderLike | None = None
        self._echo_enhancement_context: EchoEnhancementContext | None = None
        self._echo_enhancement_session: EchoEnhancementSession | None = None
        self._session_is_external = False
        self._echo_candidate_provider: Callable[["EnhanceEchoTask"], EchoIdentity] | None = None
        self._manual_resource_ledger: ResourceLedger | None = None
        self._selected_sonata_set: str | None = None
        self._selected_model_candidate_identity: EchoIdentity | None = None
        self._candidate_cost_order: tuple[int, ...] | None = None
        self._active_candidate_cost: int | None = None
        self.default_config.update({
            '必须有双爆': True,
            '双爆出现之前必须全有效词条': True,
            '双爆总计>=': 13.8,
            '首条双爆>=': 6.9,
            '有效词条>=': 3,
            '第一条必须为有效词条': True,
            '有效词条': ['暴击', '暴击伤害', '攻击百分比'],
            'Pause after Success': True,
            'Resource Source': 'OK-WW or Manual',
            'Manual Tuners': 0,
            'Manual Premium Echo EXP Tubes': 0,
            'Manual Echo EXP': 0,
            'Enforce Resource Budget': False,
            'Max Candidates Per Run': 0,
            'Max Candidate Search Pages': 10,
            'Candidate Search Scroll Count': 10,
        })
        self.config_type["有效词条"] = {'type': "multi_selection",
                                        'options': ['暴击伤害', '暴击', '攻击百分比', '生命百分比', '防御百分比',
                                                    '攻击', '生命', '防御',
                                                    '共鸣效率', '普攻伤害加成',
                                                    '重击伤害加成', '共鸣解放伤害加成',
                                                     '共鸣技能伤害加成']}
        self.config_type['Resource Source'] = {
            'type': 'drop_down',
            'options': ['OK-WW or Manual', 'Manual'],
        }
        self.config_description = {
            '必须有双爆': '如果开启，声骸最终必须同时拥有暴击和暴击伤害。如果剩余孔位不足以凑齐双爆，则丢弃',
            '双爆出现之前必须全有效词条': '开启后，在暴击或暴击伤害词条出现之前，前面的所有词条必须都在有效词条列表中',
            '双爆总计>=': '当声骸同时存在暴击和爆伤时，需要满足 暴击 + (爆伤/2) >= 此数值',
            '首条双爆>=': '仅检查第一条出现的暴击或暴击伤害是否满足条件, 爆伤/2',
            '有效词条>=': '声骸满级时需达到的有效词条数量，若剩余孔位无法凑齐该数量，则停止强化并丢弃',
            '第一条必须为有效词条': '如果开启，第一个副词条必须在有效词条列表中且符合数值要求，否则直接丢弃',
            '有效词条': '定义哪些属性被视为有效',
            'Pause after Success': 'When a success occurs, send notification and pause task',
            'Resource Source': 'Prefer resources read by an injected OK-WW provider, or use manual values',
            'Manual Tuners': 'Fallback number of tuners available to this run',
            'Manual Premium Echo EXP Tubes': 'Fallback EXP inventory in 5000-EXP tube equivalents',
            'Manual Echo EXP': 'Additional raw Echo EXP available to this run',
            'Enforce Resource Budget': 'Stop before an upgrade that exceeds the configured resources',
            'Max Candidates Per Run': '0 means unlimited; use 1 for a controlled live infrastructure test',
            'Max Candidate Search Pages': 'Maximum visible pages searched for a compatible +0 Echo',
            'Candidate Search Scroll Count': 'Down-scroll amount between candidate search pages',
        }

    def set_echo_resource_provider(self, provider: ResourceProviderLike | None) -> None:
        """Inject an ok-ww inventory reader used before the task starts."""

        self._echo_resource_provider = provider

    def configure_echo_enhancement_model(
            self, build: NamedEchoBuild, candidate_pool: Iterable[EchoIdentity], *,
            candidate_provider: Callable[["EnhanceEchoTask"], EchoIdentity] | None = None,
            config: PolicyConfig | None = None,
            cost_model: ResourceCostModel | None = None) -> None:
        """Enable the probability model with character/build context from ok-ww."""

        pool = tuple(candidate_pool)
        if not pool:
            raise ValueError('candidate_pool must not be empty')
        self._echo_enhancement_context = EchoEnhancementContext(
            build, pool, candidate_provider, config, cost_model
        )
        self._echo_enhancement_session = None
        self._session_is_external = False
        self._echo_candidate_provider = candidate_provider

    def set_echo_enhancement_session(
            self, session: EchoEnhancementSession, *,
            candidate_provider: Callable[["EnhanceEchoTask"], EchoIdentity] | None = None) -> None:
        """Inject a pre-built session for callers that manage model lifecycle."""

        self._echo_enhancement_session = session
        self._session_is_external = True
        self._echo_candidate_provider = candidate_provider

    def set_candidate_cost_order(self, costs: Iterable[int]) -> None:
        """Restrict model candidate search to reviewed COST priorities."""

        normalized = tuple(dict.fromkeys(int(cost) for cost in costs))
        if not normalized:
            raise ValueError('candidate COST order must not be empty')
        invalid = tuple(
            cost for cost in normalized if cost not in ECHO_INVENTORY_COST_TABS
        )
        if invalid:
            raise ValueError(f'unsupported candidate COST tabs: {invalid}')
        self._candidate_cost_order = normalized
        self.info_set(
            '候选COST优先级',
            ' → '.join(f'COST {cost}' for cost in normalized),
        )

    def _resolve_resources(self) -> EchoResourceSnapshot:
        prefer_ok_ww = self.config.get('Resource Source') != 'Manual'
        return resolve_echo_resources(
            provider=self._echo_resource_provider,
            manual_tuners=float(self.config.get('Manual Tuners') or 0),
            manual_premium_tubes=float(
                self.config.get('Manual Premium Echo EXP Tubes') or 0
            ),
            manual_echo_exp=float(self.config.get('Manual Echo EXP') or 0),
            prefer_ok_ww=prefer_ok_ww,
        )

    def _prepare_enhancement_runtime(self) -> None:
        self._manual_resource_ledger = None
        if self._echo_enhancement_session is not None and self._session_is_external:
            self._publish_resources(self._echo_enhancement_session.ledger)
            return

        resources = self._resolve_resources()
        self.info_set('资源来源', resources.source.value)
        if resources.detail:
            self.log_info(f'声骸强化资源来源: {resources.source.value} ({resources.detail})')
        if self._echo_enhancement_context is not None:
            context = self._echo_enhancement_context
            self._echo_enhancement_session = EchoEnhancementSession(
                context.build,
                context.candidate_pool,
                resources=resources,
                config=context.config,
                cost_model=context.cost_model,
            )
            self._session_is_external = False
            self._echo_candidate_provider = context.candidate_provider
            self._publish_resources(self._echo_enhancement_session.ledger)
        elif self.config.get('Enforce Resource Budget'):
            self._manual_resource_ledger = ResourceLedger(
                resources.tuners, resources.echo_exp
            )
            self._publish_resources(self._manual_resource_ledger)
        else:
            self.info_set('剩余调谐器', resources.tuners)
            self.info_set('剩余声骸经验', resources.echo_exp)

    def _publish_resources(self, ledger: ResourceLedger) -> None:
        self.info_set('剩余调谐器', round(ledger.tuners, 1))
        self.info_set('剩余声骸经验', round(ledger.echo_exp, 1))
        self.info_set('等价特级密音筒', round(ledger.echo_exp / 5000, 2))
        if self._echo_enhancement_session is not None:
            self.info_set(
                '待用户处理弃置声骸',
                len(self._echo_enhancement_session.pending_discards),
            )

    @staticmethod
    def _pair_ocr_stats(properties, values) -> list[tuple[str, str]]:
        pairs = []
        unmatched_values = values.copy()
        for prop in properties:
            matched_value = '0'
            if unmatched_values:
                closest = min(unmatched_values, key=lambda value: abs(prop.y - value.y))
                matched_value = closest.name
                unmatched_values.remove(closest)
            pairs.append((prop.name, matched_value))
        return pairs

    @staticmethod
    def _write_echo_inventory_png(path: Path, frame) -> None:
        encoded, data = cv2.imencode('.png', frame)
        if not encoded:
            raise RuntimeError(f'无法保存声骸遍历截图: {path}')
        path.write_bytes(data.tobytes())

    @staticmethod
    def _echo_inventory_capture_y_offset(frame) -> float:
        """Return a relative Y offset when BitBlt includes a light title bar."""

        height = int(frame.shape[0])
        if height < 2:
            return 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        row_means = gray.mean(axis=1)
        # The first row can be the dark window border. A titled capture observed
        # in OK-WW has light rows immediately after it; client-only captures do
        # not. Limit the search so light game content cannot be mistaken for a
        # title bar.
        search_end = min(height, max(12, int(round(height * 0.08))))
        if row_means[1] < 180:
            return 0.0
        for row_index in range(2, search_end):
            if row_means[row_index] < 180:
                return row_index / height
        return 0.0

    @staticmethod
    def _normalize_sonata_set_name(value: str) -> str:
        return ''.join(re.findall(r'[A-Za-z0-9\u3400-\u9fff]', value or ''))

    def _read_sonata_filter_header(self):
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        x, y, to_x, to_y = ECHO_SONATA_HEADER_BOX
        boxes = self.ocr(
            x,
            y + y_offset,
            to_x,
            to_y + y_offset,
            threshold=0.1,
            frame=frame,
        )
        self._last_sonata_header_texts = tuple(box.name for box in boxes)
        named = tuple(
            (box.name.strip(), box) for box in boxes
            if self._normalize_sonata_set_name(box.name)
        )
        for text, box in named:
            normalized = self._normalize_sonata_set_name(text)
            if normalized.startswith(('合鸣筛选', '合鳴篩選')):
                return text, box
        for text, box in named:
            if len(re.findall(r'[\u3400-\u9fff]', text)) >= 4:
                return text, box
        for text, box in named:
            if self._normalize_sonata_set_name(text) == '全部':
                return text, box
        return None, None

    def _read_selected_sonata_set(self) -> str | None:
        text, _ = self._read_sonata_filter_header()
        if text is not None:
            normalized = self._normalize_sonata_set_name(text)
            for prefix in ('合鸣筛选', '合鳴篩選'):
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):]
                    break
            cjk_count = len(re.findall(r'[\u3400-\u9fff]', normalized))
            if cjk_count >= 4 and normalized != '全部':
                return normalized
        return None

    def _read_sonata_option_boxes(self):
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        x, y, to_x, to_y = ECHO_SONATA_OPTION_BOX
        return self.ocr(
            x,
            y + y_offset,
            to_x,
            to_y + y_offset,
            threshold=0.1,
            frame=frame,
        )

    def _sonata_filter_is_open(self, boxes) -> bool:
        normalized = tuple(
            self._normalize_sonata_set_name(box.name) for box in boxes
        )
        return any(
            value.startswith(('合鸣筛选', '合鳴篩選')) or len(value) >= 6
            for value in normalized
        )

    def _click_capture_box_relative(self, box, *, after_sleep: float = 0) -> None:
        """Click an OCR box after converting capture Y to client-relative Y."""

        frame = self.frame
        height, width = frame.shape[:2]
        y_offset = self._echo_inventory_capture_y_offset(frame)
        center_x = (box.x + box.width / 2) / width
        center_y = (box.y + box.height / 2) / height - y_offset
        self.click_relative(
            center_x,
            max(0.0, center_y),
            after_sleep=after_sleep,
        )

    def select_echo_sonata_set(
            self, sonata_set: str, *, settle_time: float = 1.0,
            max_scrolls: int = 8, scroll_count: int = 8) -> str:
        """Select one character's Sonata set and verify the closed filter label."""

        target = self._normalize_sonata_set_name(sonata_set)
        if not target:
            raise ValueError('sonata_set must not be empty')
        if settle_time < 0:
            raise ValueError('settle_time must be non-negative')
        if max_scrolls < 0:
            raise ValueError('max_scrolls must be non-negative')
        if scroll_count <= 0:
            raise ValueError('scroll_count must be positive')

        if self._normalize_sonata_set_name(
                getattr(self, '_selected_sonata_set', None) or '') == target:
            selected = self._read_selected_sonata_set()
            if self._normalize_sonata_set_name(selected or '') == target:
                return sonata_set
            self._selected_sonata_set = None

        header_text, header_box = self._read_sonata_filter_header()
        selected = header_text
        if self._normalize_sonata_set_name(selected or '') in {
            '合鸣筛选全部',
            '合鳴篩選全部',
            '全部',
        }:
            selected = None
        option_boxes = self._read_sonata_option_boxes()
        panel_open = self._sonata_filter_is_open(option_boxes)
        if (self._normalize_sonata_set_name(selected or '') == target and
                not panel_open):
            self.info_set('目标合鸣套装', selected)
            return selected

        if not panel_open:
            if header_box is None:
                raise RuntimeError('找不到合鸣筛选入口，请确认当前位于声骸库存页面')
            header_key = self._normalize_sonata_set_name(header_text or '')
            if header_key == '全部':
                # The compact inventory first exposes a top-level ``全部``
                # button. It reveals the Sonata filter dropdown rather than
                # the actual set options.
                self._click_capture_box_relative(
                    header_box,
                    after_sleep=settle_time,
                )
                header_text, header_box = self._read_sonata_filter_header()
                header_key = self._normalize_sonata_set_name(header_text or '')
                header_cjk_count = len(re.findall(
                    r'[\u3400-\u9fff]', header_text or ''
                ))
                if (header_box is None or (
                        not header_key.startswith(('合鸣筛选', '合鳴篩選')) and
                        header_cjk_count < 4)):
                    raise RuntimeError(
                        '展开筛选面板后找不到合鸣筛选下拉框: '
                        f'{getattr(self, "_last_sonata_header_texts", ())}'
                    )
                option_boxes = self._read_sonata_option_boxes()
                panel_open = self._sonata_filter_is_open(option_boxes)
            if not panel_open:
                self._click_capture_box_relative(header_box, after_sleep=settle_time)
        found = None
        previous_signature = None
        try:
            for scroll_index in range(max_scrolls + 1):
                boxes = self._read_sonata_option_boxes()
                signature = tuple(
                    self._normalize_sonata_set_name(box.name) for box in boxes
                    if self._normalize_sonata_set_name(box.name)
                )
                found = next(
                    (box for box in boxes
                     if self._normalize_sonata_set_name(box.name) == target),
                    None,
                )
                if found is not None:
                    self._click_capture_box_relative(found, after_sleep=settle_time)
                    break
                if scroll_index >= max_scrolls or signature == previous_signature:
                    break
                previous_signature = signature
                self.scroll_relative(0.22, 0.72, -scroll_count)
                self.sleep(settle_time)
        finally:
            remaining_options = self._read_sonata_option_boxes()
            if self._sonata_filter_is_open(remaining_options):
                _, current_header_box = self._read_sonata_filter_header()
                if current_header_box is not None:
                    self._click_capture_box_relative(
                        current_header_box,
                        after_sleep=settle_time,
                    )

        if found is None:
            raise RuntimeError(f'找不到合鸣套装: {sonata_set}')
        selected = self._read_selected_sonata_set()
        selected_key = self._normalize_sonata_set_name(selected or '')
        if selected_key != target:
            raise RuntimeError(
                f'合鸣套装选择校验失败: 目标 {sonata_set}, 当前 {selected or "未识别"}'
            )
        self._selected_sonata_set = sonata_set
        self.info_set('目标合鸣套装', sonata_set)
        self.log_info(f'已选择并读回校验合鸣套装: {sonata_set}')
        return sonata_set

    def _read_echo_inventory_sort_label(self) -> str | None:
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        x, y, to_x, to_y = ECHO_INVENTORY_SORT_LABEL_BOX
        boxes = self.ocr(
            x,
            y + y_offset,
            to_x,
            min(1.0, to_y + y_offset),
            threshold=0.1,
            frame=frame,
        )
        for box in boxes:
            normalized = self._normalize_sonata_set_name(box.name)
            if normalized in {'等级顺序', '等級順序'}:
                return normalized
        return None

    def select_echo_inventory_level_descending(
            self, *, settle_time: float = 1.0) -> str:
        """Select and read back the game's high-level-first inventory order."""

        if settle_time < 0:
            raise ValueError('settle_time must be non-negative')
        current = self._read_echo_inventory_sort_label()
        if current in {'等级顺序', '等級順序'}:
            self.info_set('声骸排序', current)
            return current

        self.click_relative(
            ECHO_INVENTORY_SORT_BUTTON[0],
            ECHO_INVENTORY_SORT_BUTTON[1],
            after_sleep=settle_time,
        )
        self.next_frame()
        frame = self.frame
        boxes = self.ocr(0, 0, 1, 1, threshold=0.1, frame=frame)
        level_box = next((
            box for box in boxes
            if self._normalize_sonata_set_name(box.name) in {
                '等级顺序', '等級順序'
            }
        ), None)
        if level_box is None:
            raise RuntimeError('排序面板中找不到等级顺序')
        self._click_capture_box_relative(level_box, after_sleep=settle_time)

        self.next_frame()
        frame = self.frame
        boxes = self.ocr(0, 0, 1, 1, threshold=0.1, frame=frame)
        confirm_box = next((
            box for box in boxes
            if self._normalize_sonata_set_name(box.name) in {'确认', '確認'}
        ), None)
        if confirm_box is None:
            raise RuntimeError('排序面板中找不到确认按钮')
        self._click_capture_box_relative(confirm_box, after_sleep=settle_time)

        selected = self._read_echo_inventory_sort_label()
        if selected not in {'等级顺序', '等級順序'}:
            raise RuntimeError('声骸排序校验失败: 未读回等级顺序')
        self.info_set('声骸排序', selected)
        self.log_info('已选择并读回校验声骸等级从高到低排序')
        return selected

    def _read_echo_card_level(self, column_index: int, row_index: int) -> int | None:
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        center_x = ECHO_INVENTORY_GRID_COLUMNS[column_index]
        center_y = ECHO_INVENTORY_LEVEL_ROWS[row_index]
        boxes = self.ocr(
            center_x - 0.002,
            center_y - 0.012 + y_offset,
            center_x + 0.036,
            center_y + 0.028 + y_offset,
            match=ECHO_INVENTORY_LEVEL_RE,
            threshold=0.1,
            frame=frame,
        )
        for box in boxes:
            match = ECHO_INVENTORY_LEVEL_RE.search(box.name.replace('＋', '+'))
            if match:
                level = int(match.group(1))
                if 0 <= level <= 25:
                    return level
        return None

    def _read_selected_echo_inventory_item(
            self, *, cost: int | None, page_index: int, cell_index: int,
            screenshot: str = ''):
        self.next_frame()
        frame = self.frame.copy()
        y_offset = self._echo_inventory_capture_y_offset(frame)
        identity = self.ocr(
            0.65, 0.10 + y_offset, 0.98, 0.32 + y_offset, frame=frame
        )
        name_attempts: list[str] = []
        name_match = None
        for attempt_index in range(3):
            tokens = identity
            if attempt_index:
                self.next_frame()
                retry_frame = self.frame
                retry_offset = self._echo_inventory_capture_y_offset(retry_frame)
                retry_boxes = (
                    (0.74, 0.09, 0.99, 0.23, 0.05),
                    (0.68, 0.08, 0.99, 0.26, 0.15),
                )
                x, y, to_x, to_y, threshold = retry_boxes[attempt_index - 1]
                tokens = self.ocr(
                    x,
                    y + retry_offset,
                    to_x,
                    to_y + retry_offset,
                    threshold=threshold,
                    frame=retry_frame,
                )
            try:
                name_match = match_echo_name_tokens(tokens)
                if attempt_index:
                    identity = tuple(identity) + tuple(tokens)
                break
            except (EchoNameRecognitionError, ValueError) as error:
                name_attempts.append(str(error))
        if name_match is None:
            self.info_set('声骸名称OCR状态', '失败')
            self.info_set('声骸名称OCR重试', len(name_attempts))
            raise ValueError(
                '声骸名称不在合法列表或匹配存在歧义，已重试3次: '
                + ' | '.join(name_attempts)
            )
        self.info_set('声骸名称OCR状态', '成功')
        self.info_set('声骸名称OCR原文', name_match.raw)
        self.info_set('声骸名称规范化', name_match.canonical)
        self.info_set('声骸名称匹配方式', name_match.method)
        self.info_set('声骸名称相似度', round(name_match.similarity, 4))
        self.info_set('声骸名称OCR重试', len(name_attempts))
        if name_match.method != 'exact':
            self.log_warning(
                f'声骸名称OCR规范化: {name_match.raw!r} -> '
                f'{name_match.canonical!r} ({name_match.method}, '
                f'{name_match.similarity:.3f})'
            )
        main_stats = self.ocr(
            0.78, 0.205 + y_offset, 0.975, 0.285 + y_offset, frame=frame
        )
        if len(pair_echo_stat_rows(main_stats)) != 2:
            main_stats = self.ocr(
                0.65, 0.35 + y_offset, 0.98, 0.50 + y_offset, frame=frame
            )
        observed_level = next(
            (int(match.group(1)) for token in identity
             if (match := ECHO_SELECTED_LEVEL_RE.search(token.name))),
            None,
        )
        if cost is None:
            for x, y, to_x, to_y in (
                (0.80, 0.18, 0.87, 0.25),
                (0.88, 0.17, 0.95, 0.25),
            ):
                cost_boxes = self.ocr(
                    x,
                    y + y_offset,
                    to_x,
                    to_y + y_offset,
                    match=ECHO_SELECTED_COST_RE,
                    threshold=0.1,
                    frame=frame,
                )
                cost = next(
                    (int(box.name) for box in cost_boxes
                     if ECHO_SELECTED_COST_RE.fullmatch(box.name)),
                    None,
                )
                if cost is not None:
                    break
        if cost is None and observed_level == 0:
            # Some layouts render the COST digit as an icon. OCR then reads
            # only the adjacent ``COST`` label, while the fixed secondary
            # main stat remains unambiguous for a fresh Echo.
            parsed_main_stats = pair_echo_stat_rows(main_stats)
            if len(parsed_main_stats) == 2:
                fixed_stat = parsed_main_stats[1]
                if fixed_stat.stat == EchoStat.HP_FLAT and fixed_stat.value == 456:
                    cost = 1
                elif fixed_stat.stat == EchoStat.ATTACK_FLAT and fixed_stat.value == 20:
                    cost = 3
                elif fixed_stat.stat == EchoStat.ATTACK_FLAT and fixed_stat.value == 30:
                    cost = 4
        substats = () if observed_level == 0 else self.ocr(
            0.78, 0.282 + y_offset, 0.975, 0.455 + y_offset, frame=frame
        )
        if screenshot:
            self._write_echo_inventory_png(Path(screenshot), frame)
        return parse_echo_inventory_item(
            identity,
            main_stats,
            substats,
            cost_hint=cost,
            page_index=page_index,
            cell_index=cell_index,
            screenshot=screenshot,
            canonical_name=name_match.canonical,
        )

    def _read_selected_echo_level(self) -> int | None:
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        # The compact 1366px layout places the level beside the name; the
        # 1920px inventory layout places it below COST. Narrow crops make the
        # small glyph large enough for OCR in both layouts.
        for x, y, to_x, to_y in (
            (0.89, 0.145, 0.98, 0.215),
            (0.89, 0.225, 0.98, 0.295),
        ):
            boxes = self.ocr(
                x,
                y + y_offset,
                to_x,
                to_y + y_offset,
                match=ECHO_SELECTED_LEVEL_RE,
                threshold=0.1,
                frame=frame,
            )
            for box in boxes:
                match = ECHO_SELECTED_LEVEL_RE.search(box.name)
                if match:
                    level = int(match.group(1))
                    if 0 <= level <= 25:
                        return level
        return None

    def _read_current_echo_candidate_identity(self) -> EchoIdentity:
        item = self._read_selected_echo_inventory_item(
            cost=None,
            page_index=0,
            cell_index=0,
        )
        if item.level != 0:
            raise RuntimeError(f'当前声骸不是 +0: +{item.level}')
        return item.identity

    def scan_filtered_echo_inventory(
            self, *, output_dir: str | os.PathLike | None = None,
            costs: Iterable[int] = (1, 3, 4), completed_only: bool = True,
            max_pages_per_cost: int = 20, settle_time: float = 3.5,
            scroll_count: int = 20) -> EchoInventoryScanResult:
        """Traverse the current Sonata-filtered inventory without modifying it.

        Every selected card is validated from the right-hand details panel.
        Failed OCR is retained as a screenshot-backed issue instead of silently
        becoming model input. Structured attributes are de-duplicated across
        overlapping rows. With high-level-first sorting, the first confirmed
        non-+25 item ends that COST immediately. A second item with the same
        level and structured attributes also ends that COST immediately; this
        is the empty-grid/list-wrap signal for inventories shorter than one
        page. Otherwise a page with no new attribute combination ends the scan.
        """

        normalized_costs = tuple(dict.fromkeys(int(cost) for cost in costs))
        if not normalized_costs or any(cost not in ECHO_INVENTORY_COST_TABS
                                       for cost in normalized_costs):
            raise ValueError('costs must contain one or more of 1, 3, and 4')
        if max_pages_per_cost < 1:
            raise ValueError('max_pages_per_cost must be at least 1')
        if settle_time < 0:
            raise ValueError('settle_time must be non-negative')
        if scroll_count <= 0:
            raise ValueError('scroll_count must be positive to move down the inventory')

        root = Path(output_dir) if output_dir else Path('screenshots') / 'echo_inventory' / datetime.now().strftime(
            '%Y%m%d_%H%M%S'
        )
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        items = []
        issues = []
        attribute_keys: set[tuple[object, ...]] = set()
        duplicate_fingerprints = 0
        pages_scanned = 0
        stop_reasons = []

        for cost in normalized_costs:
            tab_x, tab_y = ECHO_INVENTORY_COST_TABS[cost]
            self.click_relative(tab_x, tab_y, after_sleep=settle_time)
            cost_stopped = False
            seen_detail_keys: set[tuple[object, ...]] = set()

            for page_index in range(max_pages_per_cost):
                pages_scanned += 1
                page_new_items = 0
                self.info_set('遍历COST', cost)
                self.info_set('遍历页', page_index + 1)
                page_dir = root / f'cost_{cost}' / f'page_{page_index + 1:02d}'
                page_dir.mkdir(parents=True, exist_ok=True)
                first_incomplete = None
                repeated_detail = None

                for row_index, click_y in enumerate(ECHO_INVENTORY_GRID_ROWS):
                    for column_index, click_x in enumerate(ECHO_INVENTORY_GRID_COLUMNS):
                        cell_index = row_index * len(ECHO_INVENTORY_GRID_COLUMNS) + column_index
                        card_level = self._read_echo_card_level(column_index, row_index)
                        self.click_relative(click_x, click_y, after_sleep=settle_time)
                        screenshot = str((page_dir / f'cell_{cell_index + 1:02d}.png').resolve())
                        try:
                            item = self._read_selected_echo_inventory_item(
                                cost=cost,
                                page_index=page_index,
                                cell_index=cell_index,
                                screenshot=screenshot,
                            )
                            if completed_only and item.level != 25:
                                first_incomplete = (cell_index, item.level, 'details')
                                break
                            detail_key = (item.level, item.attribute_key)
                            if detail_key in seen_detail_keys:
                                duplicate_fingerprints += 1
                                repeated_detail = (
                                    cell_index,
                                    item.level,
                                    item.identity.name,
                                )
                                break
                            seen_detail_keys.add(detail_key)
                            if item.attribute_key in attribute_keys:
                                duplicate_fingerprints += 1
                                continue
                            attribute_keys.add(item.attribute_key)
                            items.append(item)
                            page_new_items += 1
                            self.info_set('已读取满级声骸', len(items))
                        except (ValueError, RuntimeError) as error:
                            issues.append(EchoInventoryScanIssue(
                                cost,
                                page_index,
                                cell_index,
                                str(error),
                                screenshot,
                            ))
                            self.log_warning(
                                f'COST {cost} 第{page_index + 1}页格{cell_index + 1}读取失败: {error}'
                            )
                            if (completed_only and card_level is not None and
                                    card_level != 25):
                                first_incomplete = (
                                    cell_index,
                                    card_level,
                                    'card level after detail OCR failure',
                                )
                                break
                    if first_incomplete is not None or repeated_detail is not None:
                        break

                if first_incomplete is not None:
                    cell_index, level, source = first_incomplete
                    stop_reasons.append(
                        f'COST {cost}: first non-+25 Echo at page '
                        f'{page_index + 1}, cell {cell_index + 1}, +{level} '
                        f'({source})'
                    )
                    cost_stopped = True
                    break

                if repeated_detail is not None:
                    cell_index, level, name = repeated_detail
                    stop_reasons.append(
                        f'COST {cost}: repeated identical Echo at page '
                        f'{page_index + 1}, cell {cell_index + 1}, '
                        f'{name}, +{level}'
                    )
                    cost_stopped = True
                    break

                if page_index > 0 and page_new_items == 0:
                    stop_reasons.append(
                        f'COST {cost}: no new attribute combinations after scrolling'
                    )
                    cost_stopped = True
                    break
                if page_index + 1 >= max_pages_per_cost:
                    break
                self.scroll_relative(0.25, 0.70, -scroll_count)
                self.sleep(settle_time)

            if not cost_stopped:
                stop_reasons.append(f'COST {cost}: max page limit reached')

        result = EchoInventoryScanResult(
            tuple(items),
            tuple(issues),
            pages_scanned,
            duplicate_fingerprints,
            '; '.join(stop_reasons),
        )
        manifest = root / 'inventory_scan.json'
        manifest.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        self.info_set('遍历报告', str(manifest))
        self.log_info(
            f'声骸遍历完成: 满级{len(result.items)}件，OCR问题{len(result.issues)}项，报告 {manifest}'
        )
        return result

    def analyze_character_echo_inventory(
            self, *, sonata_set: str, build: NamedEchoBuild,
            output_dir: str | os.PathLike | None = None,
            costs: Iterable[int] = (1, 3, 4),
            minimum_gain: float = 0.0,
            required_stats: frozenset[EchoStat] = frozenset(),
            max_pages_per_cost: int = 20,
            settle_time: float = 3.5,
            scroll_count: int = 20) -> EchoCharacterInventoryAnalysis:
        """Select one set, scan its completed Echoes, and rank improvements."""

        root = Path(output_dir) if output_dir else (
            Path('screenshots') / 'echo_character_inventory' /
            datetime.now().strftime('%Y%m%d_%H%M%S')
        )
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        selected_set = self.select_echo_sonata_set(
            sonata_set,
            settle_time=min(settle_time, 1.5),
        )
        self.select_echo_inventory_level_descending(
            settle_time=min(settle_time, 1.5),
        )
        scan = self.scan_filtered_echo_inventory(
            output_dir=root / 'inventory',
            costs=costs,
            completed_only=True,
            max_pages_per_cost=max_pages_per_cost,
            settle_time=settle_time,
            scroll_count=scroll_count,
        )
        replacements = scan.rank_replacements(
            build,
            minimum_gain=minimum_gain,
            required_stats=required_stats,
        )
        report = root / 'character_inventory_analysis.json'
        analysis = EchoCharacterInventoryAnalysis(
            selected_set,
            scan,
            replacements,
            str(report),
        )
        report.write_text(
            json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        self.info_set('可提升声骸', len(replacements))
        if analysis.best_replacement is not None:
            best = analysis.best_replacement
            self.info_set('最大提升位置', best.option.slot)
            self.info_set('最大提升分数', round(best.option.delta, 4))
        else:
            self.info_set('最大提升位置', '无')
        self.info_set('角色声骸分析报告', str(report))
        self.log_info(
            f'{selected_set} 库存评分完成: 满级{len(scan.items)}件，'
            f'可提升{len(replacements)}件，报告 {report}'
        )
        return analysis

    def _stop_for_resources(self, cost) -> None:
        self.log_info(
            f'资源不足，下一孔需要调谐器 {cost.tuners:g}、声骸经验 {cost.echo_exp:g}；'
            '当前声骸不标记弃置，任务结束',
            notify=True,
        )
        self.esc()

    def find_echo_enhance(self):
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        return self.ocr(
            0.82,
            0.86 + y_offset,
            0.97,
            min(1.0, 0.96 + y_offset),
            match='培养',
            frame=frame,
        )

    def is_0_level(self):
        return self._read_selected_echo_level() == 0

    def _select_first_fresh_echo(self, settle_time: float = 0.5) -> int:
        """Select the first visible eligible +0 card.

        The game cannot combine ``level order`` with ``unequipped first``.
        Equipped +25 cards may therefore remain pinned before fresh cards.
        In model mode every +0 card is additionally opened and its exact
        name/COST/main-stat identity must belong to the reviewed candidate
        pool before it can be cultivated.
        """

        self._selected_model_candidate_identity = None
        fresh_seen = 0
        skipped = 0
        all_observed_identities: set[tuple[str, int, str]] = set()
        task_config = getattr(self, 'config', {})
        max_pages = max(1, int(task_config.get('Max Candidate Search Pages') or 10))
        scroll_count = max(1, int(task_config.get('Candidate Search Scroll Count') or 10))
        pages_searched = 0
        candidate_costs: tuple[int | None, ...] = (None,)
        if self._echo_enhancement_session is not None:
            pool_costs = tuple(dict.fromkeys(
                identity.cost
                for identity in self._echo_enhancement_session.candidate_pool
                if identity.cost in ECHO_INVENTORY_COST_TABS
            ))
            configured_costs = getattr(self, '_candidate_cost_order', None)
            candidate_costs = (
                tuple(cost for cost in configured_costs if cost in pool_costs)
                if configured_costs
                else pool_costs
            )
            if not candidate_costs:
                raise RuntimeError('第一步候选池不包含可搜索的COST 1/3/4声骸')

        for candidate_cost in candidate_costs:
            if candidate_cost is not None:
                if getattr(self, '_active_candidate_cost', None) != candidate_cost:
                    tab_x, tab_y = ECHO_INVENTORY_COST_TABS[candidate_cost]
                    self.click_relative(tab_x, tab_y, after_sleep=settle_time)
                    self._active_candidate_cost = candidate_cost
                self.info_set('候选搜索COST', candidate_cost)

            # Upstream 3.7: returning from cultivation leaves the list at the
            # enhanced Echo. Toggle twice to reset the viewport while keeping
            # the user's sort direction, then apply the existing model filters.
            for _ in range(2):
                self.click_relative(0.35, 0.917, after_sleep=settle_time)
            self.info_set('候选搜索起点', '切换两次升降序并回到顶部')

            observed_identities: set[tuple[str, int, str]] = set()
            for page_index in range(max_pages):
                pages_searched += 1
                page_new_identities = 0
                self.info_set('候选搜索页', page_index + 1)
                for row_index, click_y in enumerate(ECHO_INVENTORY_GRID_ROWS):
                    for column_index, click_x in enumerate(ECHO_INVENTORY_GRID_COLUMNS):
                        card_level = self._read_echo_card_level(column_index, row_index)
                        if card_level is not None and card_level != 0:
                            continue
                        self.click_relative(click_x, click_y, after_sleep=settle_time)
                        level = self._read_selected_echo_level()
                        if level != 0:
                            if card_level == 0:
                                skipped += 1
                                self.log_warning(
                                    f'跳过第{page_index + 1}页格子 '
                                    f'{row_index + 1}-{column_index + 1}: '
                                    f'卡片预读为+0，详情等级为{level!r}'
                                )
                            continue
                        fresh_seen += 1

                        if self._echo_enhancement_session is not None:
                            try:
                                identity = self._read_current_echo_candidate_identity()
                            except (RuntimeError, ValueError) as error:
                                skipped += 1
                                self.log_warning(
                                    f'跳过第{page_index + 1}页+0声骸 '
                                    f'{row_index + 1}-{column_index + 1}: {error}'
                                )
                                continue
                            identity_key = (
                                identity.name,
                                identity.cost,
                                identity.compatibility,
                            )
                            all_observed_identities.add(identity_key)
                            if identity_key not in observed_identities:
                                observed_identities.add(identity_key)
                                page_new_identities += 1
                            compatible_slots = (
                                self._echo_enhancement_session.build.compatible_slots(identity)
                            )
                            if (
                                identity not in self._echo_enhancement_session.candidate_pool
                                or not compatible_slots
                            ):
                                skipped += 1
                                self.log_info(
                                    f'跳过不兼容+0声骸: {identity.name}/COST '
                                    f'{identity.cost}/{identity.compatibility}'
                                )
                                continue
                            theoretical = (
                                self._echo_enhancement_session.build.best_possible_replacement(
                                    identity,
                                    minimum_gain=(
                                        self._echo_enhancement_session.config.minimum_gain
                                    ),
                                    required_stats=(
                                        self._echo_enhancement_session.config.required_stats
                                    ),
                                )
                            )
                            if theoretical is None:
                                skipped += 1
                                self.log_info(
                                    f'跳过理论上限也不能提升的+0声骸: '
                                    f'{identity.name}/COST {identity.cost}/'
                                    f'{identity.compatibility}'
                                )
                                continue
                            self._selected_model_candidate_identity = identity
                            self.info_set(
                                '当前模型候选',
                                f'{identity.name}/COST {identity.cost}/{identity.compatibility}',
                            )
                            self.info_set('候选理论目标位置', theoretical.slot)
                            self.info_set(
                                '候选理论最大提升',
                                round(theoretical.delta, 4),
                            )
                            self.info_set(
                                '候选理论最大总分',
                                round(theoretical.final_score, 4),
                            )
                            self.log_info(
                                f'候选名称级目标: {identity.name} -> '
                                f'{theoretical.slot}, 理论最大提升 '
                                f'{theoretical.delta:.4f}, 理论最大总分 '
                                f'{theoretical.final_score:.4f}'
                            )

                        self.info_set('跳过不兼容+0声骸', skipped)
                        self.info_set('当前声骸等级', 0)
                        return 0

                # A level-descending inventory can contain several complete
                # pages of +25 Echoes before the first +0 card.  Only treat a
                # page with no new identity as overlap/end-of-list after at
                # least one +0 identity has actually been observed.
                if (
                    page_index > 0
                    and observed_identities
                    and page_new_identities == 0
                ):
                    break
                if page_index + 1 >= max_pages:
                    break
                self.scroll_relative(0.25, 0.70, -scroll_count)
                self.sleep(settle_time)

        self.info_set('跳过不兼容+0声骸', skipped)
        if fresh_seen:
            observed_text = '；'.join(
                f'{name}/COST {cost}/{compatibility}'
                for name, cost, compatibility in sorted(all_observed_identities)
            ) or '均未能读取结构化身份'
            raise NoEligibleFreshEchoError(
                f'搜索 {pages_searched} 页、发现 {fresh_seen} 个可见+0格子，'
                '但没有一件匹配第一步候选池和主属性；'
                f'已见组合: {observed_text}'
            )
        raise NoEligibleFreshEchoError(
            f'搜索 {pages_searched} 页后没有发现可强化的+0声骸'
        )

    def _candidate_limit_reached(self) -> bool:
        limit = int(self.config.get('Max Candidates Per Run') or 0)
        if limit <= 0:
            return False
        processed = (
            int(self.info_get('成功声骸数量') or 0)
            + int(self.info_get('失败声骸数量') or 0)
        )
        return processed >= limit

    def _publish_enhancement_decision(self, decision) -> None:
        action_labels = {
            EnhancementAction.CONTINUE: '继续强化',
            EnhancementAction.MARK_DISCARD: '标记弃置',
            EnhancementAction.KEEP_AND_LOCK: '保留并上锁',
            EnhancementAction.STOP_RESOURCES: '资源不足停止',
        }
        action = action_labels.get(decision.action, decision.action.value)
        self.info_set('模型决策', action)
        self.info_set('模型决策阶段', f'+{decision.current_slots * 5}')
        self.info_set('模型决策原因', decision.reason)
        metrics_text = ''
        if decision.metrics is not None:
            self.info_set(
                '模型完成成功率',
                round(decision.metrics.success_probability, 4),
            )
            self.info_set(
                '模型条件效率',
                round(decision.metrics.efficiency, 4),
            )
            metrics_text = (
                f', 成功率={decision.metrics.success_probability:.2%}, '
                f'条件效率={decision.metrics.efficiency:.4f}, '
                f'继续阈值={decision.continue_threshold:.4f}'
            )
        self.log_info(
            f'模型决策: 阶段=+{decision.current_slots * 5}, '
            f'动作={action}, 原因={decision.reason}{metrics_text}'
        )

    def select_0_level_echo(self):
        # 3.7 起从培养界面返回后, 背包选中并滚动到刚强化的声骸, 不再停在第一格
        self.click(0.13, 0.21, after_sleep=0.5)  # 第一格
        if self.is_0_level():
            return True
        for _ in range(2):  # 升降序切换两次, 列表回到顶部
            self.click(0.35, 0.917, after_sleep=0.5)
        self.click(0.13, 0.21, after_sleep=0.5)
        return self.is_0_level()

    def run(self):
        self.info_set('成功声骸数量', 0)
        self.info_set('失败声骸数量', 0)
        self._prepare_enhancement_runtime()
        # Preserve earlier evidence. A live test/run may be interrupted after
        # spending resources, so deleting prior screenshots at startup makes
        # diagnosis unnecessarily destructive.
        Path('screenshots').mkdir(parents=True, exist_ok=True)
        while True:
            enhance = self.find_echo_enhance()
            if not enhance:
                raise Exception('必须在背包声骸界面过滤后开始!')
            try:
                current_level = self._select_first_fresh_echo()
            except NoEligibleFreshEchoError as error:
                self.info_set('候选搜索状态', '目标COST没有合格+0声骸')
                self.info_set('候选搜索结果', str(error))
                self.log_info(f'没有合格+0声骸，未消耗材料，任务结束: {error}')
                return
            enhance = self.find_echo_enhance()
            if not enhance:
                raise RuntimeError('选择首件声骸后找不到培养按钮')
            if current_level != 0:
                total = self.info_get('成功声骸数量') + self.info_get('失败声骸数量')
                if self.debug:
                    self.screenshot('无可强化声骸')
                self.log_info(f'无可强化声骸, 任务结束! 强化{total}个, 符合条件{self.info_get("成功声骸数量")}个',
                              notify=True)
                if self.info_get('成功声骸数量') >= 1:
                    try:
                        os.startfile(os.path.abspath("screenshots"))
                    except Exception as e:
                        self.log_error(f"无法打开截图文件夹: {e}")
                return
            current_state = EchoState()
            current_slot_count = 0
            current_identity = None
            if self._echo_enhancement_session is not None:
                if self._echo_candidate_provider is not None:
                    current_identity = self._echo_candidate_provider(self)
                elif self._selected_model_candidate_identity is not None:
                    current_identity = self._selected_model_candidate_identity
                else:
                    current_identity = self._read_current_echo_candidate_identity()
                if current_identity not in self._echo_enhancement_session.candidate_pool:
                    raise RuntimeError(
                        f'当前声骸不在概率模型候选池: '
                        f'{current_identity.name}/COST {current_identity.cost}/'
                        f'{current_identity.compatibility}'
                    )
            start = time.time()
            while time.time() - start < 5:
                if enhance:
                    self._click_capture_box_relative(
                        enhance[0],
                        after_sleep=0.5,
                    )
                enhance = self.find_echo_enhance()
                if not enhance:
                    break

            while True:
                if self._echo_enhancement_session is not None:
                    decision = self._echo_enhancement_session.advise(
                        current_identity, current_state
                    )
                    self._publish_enhancement_decision(decision)
                    if decision.action == EnhancementAction.STOP_RESOURCES:
                        self._stop_for_resources(decision.required_cost)
                        return
                    if decision.action == EnhancementAction.MARK_DISCARD:
                        self.fail_reason = decision.reason
                        self.mark_discarded_and_esc()
                        self._echo_enhancement_session.record_discard(
                            current_identity, current_state, decision.reason
                        )
                        self._publish_resources(self._echo_enhancement_session.ledger)
                        break
                    if decision.action == EnhancementAction.KEEP_AND_LOCK:
                        self.lock_and_esc()
                        self._echo_enhancement_session.record_keep(
                            current_identity, current_state, decision.replacement_slot
                        )
                        break
                elif self._manual_resource_ledger is not None:
                    next_cost = ResourceCostModel().upgrade_cost(
                        current_slot_count, current_slot_count + 1
                    )
                    if not self._manual_resource_ledger.can_afford(next_cost):
                        self._stop_for_resources(next_cost)
                        return

                start_wait = time.time()
                have_add_mat = False
                while time.time() - start_wait < 5:
                    add_mat = self.find_add_mat()
                    if add_mat:
                        have_add_mat = True
                        self._click_capture_box_relative(
                            add_mat[0],
                            after_sleep=0.3,
                        )
                    else:
                        self.next_frame()
                        if have_add_mat:
                            break
                if not have_add_mat:
                    raise Exception('强化设置需要开启阶段放入!')

                self.next_frame()
                y_offset = self._echo_inventory_capture_y_offset(self.frame)
                enhance_and_tune = self.wait_ocr(
                    0.1,
                    0.88 + y_offset,
                    0.29,
                    min(1.0, 0.96 + y_offset),
                    match=['强化并调谐'],
                    time_out=2,
                )
                if enhance_and_tune:
                    self._click_capture_box_relative(
                        enhance_and_tune[0],
                        after_sleep=1.5,
                    )
                else:
                    if self.ocr(
                            0.17,
                            0.88 + y_offset,
                            0.29,
                            min(1.0, 0.96 + y_offset),
                            match=['强化']):
                        raise Exception('强化设置需要开启同步调谐!')
                    else:
                        raise Exception('找不到 强化并调谐!')
                while handle := self.wait_ocr(0.24, 0.18 + y_offset, 0.75, 1.0,
                                              match=[re.compile('不再提示'), '调谐成功', re.compile('点击任')],
                                              time_out=2):
                    if handle[0].name in ['本次登录不再提示', '本次登入不再提示']:
                        self.click_skip_dialog_confirm()
                    elif handle[0].name in ['点击任意位置返回', '调谐成功']:
                        self._click_capture_box_relative(
                            handle[0],
                            after_sleep=1,
                        )
                    else:
                        self.sleep(0.5)
                self.sleep(0.1)
                self.next_frame()
                frame = self.frame
                y_offset = self._echo_inventory_capture_y_offset(frame)
                texts = self.ocr(
                    0.09,
                    0.3 + y_offset,
                    0.40,
                    min(1.0, 0.53 + y_offset),
                    frame=frame,
                )
                self.log_info(f'ocr values: {texts}')
                properties = [p for p in self.find_boxes(texts, match=property_pattern) if '辅音' not in p.name]
                for p in properties:
                    match = property_pattern.search(p.name)
                    if match:
                        p.name = match.group()
                values = self.find_boxes(texts, match=number_pattern)
                self.info_set('属性', properties)
                self.info_set('值', values)

                if self._echo_enhancement_session is not None:
                    pairs = self._pair_ocr_stats(properties, values)
                    try:
                        observed_state = echo_state_from_ocr_pairs(pairs)
                    except ValueError as error:
                        raise RuntimeError(f'无法将OCR副词条转换为强化模型数据: {error}') from error
                    self._echo_enhancement_session.record_reveal(
                        current_state, observed_state
                    )
                    self._publish_resources(self._echo_enhancement_session.ledger)
                    current_state = observed_state
                    current_slot_count = observed_state.slot_count
                elif self._manual_resource_ledger is not None:
                    spent = ResourceCostModel().upgrade_cost(
                        current_slot_count, len(properties)
                    )
                    self._manual_resource_ledger.spend(spent)
                    self._publish_resources(self._manual_resource_ledger)
                    current_slot_count = len(properties)

                if self._echo_enhancement_session is None and not self.check_echo_stats(properties, values):
                    self.mark_discarded_and_esc()
                    break

                if self._echo_enhancement_session is None and len(properties) >= 5:
                    self.lock_and_esc()
                    break

            if self._candidate_limit_reached():
                total = self.info_get('成功声骸数量') + self.info_get('失败声骸数量')
                self.log_info(f'已达到本轮声骸处理上限 {total}，任务结束')
                return

    def find_confirm(self):
        button_box = self.box_of_screen(0.60, 0.65, 0.82, 0.82)
        if confirm := self.find_one('echo_enhance_confirm', box=button_box, threshold=0.7):
            return [confirm]
        return self.ocr(box=button_box, match='确认')

    def check_echo_stats(self, properties, values):
        self.fail_reason = ""
        invalid_count = 0

        paired_stats = self._pair_ocr_stats(properties, values)

        total_count = len(paired_stats)

        crit_rate_val = 0
        crit_dmg_val = 0
        has_crit_rate = False
        has_crit_dmg = False

        checked_first_crit = False
        has_encountered_crit = False

        valid_stats = self.config.get('有效词条') or []

        for p_raw, v_str in paired_stats:
            p = p_raw
            if '暴击伤害' in p:
                p = '暴击伤害'
            elif '暴击' in p:
                p = '暴击'
            elif '攻击' in p:
                p = '攻击' + ('百分比' if '%' in v_str or '％' in v_str else '')
            elif '生命' in p:
                p = '生命' + ('百分比' if '%' in v_str or '％' in v_str else '')
            elif '防御' in p:
                p = '防御' + ('百分比' if '%' in v_str or '％' in v_str else '')
            elif '效率' in p:
                p = '共鸣效率'
            elif '普攻' in p:
                p = '普攻伤害加成'
            elif '重击' in p:
                p = '重击伤害加成'
            elif '解放' in p:
                p = '共鸣解放伤害加成'
            elif '技能' in p:
                p = '共鸣技能伤害加成'

            v = parse_number(v_str)

            is_valid_prop = True
            is_crit_stat = p in ['暴击', '暴击伤害']

            if self.config.get(
                    '双爆出现之前必须全有效词条') and '暴击' in valid_stats and '暴击伤害' in valid_stats and not has_encountered_crit:
                if not is_crit_stat:
                    if p not in valid_stats:
                        self.fail_reason = f'双爆前含无效_{p}'
                        self.log_info(f'双爆出现前存在无效词条 {p}, 丢弃')
                        return False
                else:
                    has_encountered_crit = True

            if is_valid_prop and p not in valid_stats:
                is_valid_prop = False
                self.log_debug(f'非有效词条, {p} 不符合条件')

            if p == '暴击':
                has_crit_rate = True
                crit_rate_val += v
                if '暴击' in valid_stats and not checked_first_crit:
                    checked_first_crit = True
                    if v < self.config.get('首条双爆>='):
                        self.fail_reason = f'首条暴击不足_{v}'
                        self.log_info(f'首条暴击 {v} < {self.config.get("首条双爆>=")}，丢弃')
                        return False

            elif p == '暴击伤害':
                has_crit_dmg = True
                crit_dmg_val += v
                if '暴击伤害' in valid_stats and not checked_first_crit:
                    checked_first_crit = True
                    if v / 2 < self.config.get('首条双爆>='):
                        self.fail_reason = f'首条爆伤不足_{v}'
                        self.log_info(f'首条爆伤 {v} < {self.config.get("首条双爆>=")}，丢弃')
                        return False

            if not is_valid_prop:
                invalid_count += 1

        self.info_set('不符合条件属性', invalid_count)

        if self.config.get('必须有双爆'):
            missing_crit = (0 if has_crit_rate else 1) + (0 if has_crit_dmg else 1)
            remaining_slots = 5 - total_count
            if remaining_slots < missing_crit:
                self.fail_reason = f'无法凑齐双爆_缺{missing_crit}'
                self.log_info(f'无法凑齐双爆 (缺{missing_crit}种, 剩{remaining_slots}孔), 丢弃')
                return False

        if has_crit_rate and has_crit_dmg:
            total_score = crit_rate_val + (crit_dmg_val / 2)
            if total_score < self.config.get('双爆总计>='):
                self.fail_reason = f'双爆总计不足_{total_score:.1f}'
                self.log_info(f'双爆总计 {total_score:.1f} < {self.config.get("双爆总计>=")}，丢弃')
                return False

        if total_count == 1 and self.config.get('第一条必须为有效词条') and invalid_count == 1:
            self.fail_reason = '首条无效'
            self.log_info('第一条必须为有效词条, 丢弃')
            return False

        valid_count = total_count - invalid_count
        remaining_slots = 5 - total_count
        if (valid_count + remaining_slots) < self.config.get('有效词条>='):
            self.fail_reason = f'有效词条不足_上限{valid_count + remaining_slots}'
            self.log_info(f'剩余孔位不足以达到设定的有效词条数量, 丢弃')
            return False

        return True

    def find_add_mat(self):
        self.next_frame()
        frame = self.frame
        y_offset = self._echo_inventory_capture_y_offset(frame)
        return self.ocr(
            0.09,
            0.6 + y_offset,
            0.38,
            min(1.0, 0.86 + y_offset),
            match=['阶段放入'],
            frame=frame,
        )

    def esc(self):
        start = time.time()
        while not self.find_echo_enhance() and time.time() - start < 10:
            self.send_key('esc', interval=4, after_sleep=0.2)
        self.sleep(0.1)

    def _echo_status_feature_box(self, names: Iterable[str]):
        """Return a feature search box shifted into capture coordinates."""

        self.next_frame()
        # Use the whole adjacent discard/lock status strip. Individual old
        # templates have slightly different widths/centres in the current UI;
        # a box built from only one pair can clip an otherwise 0.9+ match.
        status_names = (
            'echo_dropped',
            'echo_not_dropped',
            'echo_locked',
            'echo_not_locked',
        )
        base = get_bounding_box([
            self.get_box_by_name(name) for name in status_names
        ]).scale(1.12)
        y_offset = self._echo_inventory_capture_y_offset(self.frame)
        return base.copy(y_offset=round(y_offset * self.frame.shape[0]))

    def mark_discarded_and_esc(self):
        """Mark the Echo as discarded; never dismantle or recycle it here."""

        start = time.time()
        success = False
        drop_status_box = self._echo_status_feature_box((
            'echo_dropped',
            'echo_not_dropped',
        ))
        while time.time() - start < 5:
            drop_status = self.find_best_match_in_box(drop_status_box,
                                                      ['echo_dropped', 'echo_not_dropped'], threshold=0.7)
            if not drop_status:
                raise Exception('无法找到声骸弃置状态!')
            if drop_status.name == 'echo_not_dropped':
                self.send_key('z', after_sleep=1)
            else:
                self.log_info('成功弃置!')
                success = True
                break
        if not success:
            raise Exception('弃置失败!')
        self.info_incr('失败声骸数量')
        safe_reason = re.sub(r'[<>:"/\\|?*]', '', self.fail_reason)
        self.screenshot_echo(f'failed/{self.info_get("失败声骸数量")}_{safe_reason}')
        self.esc()
        self.log_info('不符合条件 丢弃')
        self.wait_ocr(0.82, 0.86, 0.97, 0.96, match='培养', settle_time=0.1)

    def trash_and_esc(self):
        """Backward-compatible alias for the old, misleading method name."""

        return EnhanceEchoTask.mark_discarded_and_esc(self)

    def screenshot_echo(self, name):
        echo = self.box_of_screen(0.09, 0.09, 0.37, 0.55).crop_frame(self.frame)
        self.screenshot(name=name, frame=echo)

    def lock_and_esc(self):
        start = time.time()
        success = False
        lock_status_box = self._echo_status_feature_box((
            'echo_locked',
            'echo_not_locked',
        ))
        while time.time() - start < 5:
            drop_status = self.find_best_match_in_box(lock_status_box,
                                                      ['echo_locked', 'echo_not_locked'], threshold=0.7)
            if not drop_status:
                raise Exception('无法找到声骸上锁状态!')
            if drop_status.name == 'echo_not_locked':
                self.send_key('c', after_sleep=1)
            else:
                self.log_info('成功上锁!')
                success = True
                break
        if not success:
            raise Exception('上锁失败!')
        self.info_incr('成功声骸数量')
        self.screenshot_echo(f'success/{self.info_get("成功声骸数量")}')
        self.log_info('成功并上锁')
        if self.config.get('Pause after Success'):
            self.log_info('符合条件的声骸，已暂停任务', notify=True)
            self.pause()
        self.esc()
        self.wait_ocr(0.82, 0.86, 0.97, 0.96, match='培养', settle_time=0.1)


def parse_number(text):
    try:
        return float(text.replace('％', '%').split('%')[0])
    except (ValueError, IndexError):
        return 0.0
