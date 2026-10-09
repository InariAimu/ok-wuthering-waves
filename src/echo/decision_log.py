"""Durable structured decisions with a small human-readable current view."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
from typing import Mapping


def json_value(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class DecisionJournal:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.sequence = 0
        self.latest_ranking = None

    def _atomic_json(self, name, value):
        target = self.directory / name
        temporary = target.with_suffix(target.suffix + '.tmp')
        temporary.write_text(json.dumps(json_value(value), ensure_ascii=False,
                                        indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(target)

    def append(self, kind: str, payload: dict, snapshot: dict):
        self.sequence += 1
        event = dict(schema_version=1, sequence=self.sequence,
                     at=datetime.now(timezone.utc).isoformat(), kind=kind,
                     payload=payload, snapshot=snapshot)
        # Close after every event so a game/OCR failure preserves prior costs.
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(json_value(event), ensure_ascii=False, allow_nan=False)+'\n')
        if kind == 'candidate_ranking':
            self.latest_ranking = json_value(payload)
        self._atomic_json('latest.json', dict(event=event, latest_ranking=self.latest_ranking))
        resources = snapshot['resources']
        lines = ['# 声骸强化决策日志', '',
                 f"模式：{snapshot['mode']}；最后事件：{kind}；序号：{self.sequence}。", '',
                 f"已完成 {snapshot['completed_candidates']} 件；逻辑配装评分 {snapshot['build_score']:.4f}。逻辑配装不代表已在游戏中换装。", '',
                 f"停止原因：{snapshot.get('stop_reason') or '尚未停止'}", '',
                 f"可花余额：调谐器 {resources['tuners']:g}；经验 {resources['echo_exp']:g}。", '',
                 f"毛投入：调谐器 {resources['gross_tuners']:g}；经验 {resources['gross_exp']:g}。", '',
                 f"预计待回收：调谐器 {resources['recoverable_tuners_later']:g}；经验 {resources['recoverable_exp_later']:g}。未回收返还不计入可花余额。", '',
                 f"预计经济净耗：调谐器 {resources['economic_net_tuners']:g}；经验 {resources['economic_net_exp']:g}。", '',
                 '经验投入默认来自最低升级成本模型；实际投料溢出需用资源重读校准。返还率是模型参数，不是已执行回收。', '']
        if snapshot.get('unreconciled_upgrade'):
            lines += ['**存在尚未核对的强化操作：可能已经消耗材料，尚未成功读回并记账。请先核对截图、等级和资源，勿按旧余额盲目重试。**', '']
        if self.latest_ranking:
            selection = self.latest_ranking
            lines += [f"调度结论：{selection['action']}；{selection['reason']}。", '',
                      '| 胚子 | COST / 主词条 | 库存状态 | 提升概率 | 平均正提升 | 净材料效率 | 相对最佳效率 | 样本数 |',
                      '|---|---|---|---:|---:|---:|---:|---:|']
            for row in selection['rankings']:
                identity, metrics = row['identity'], row['metrics']
                remaining = row['remaining']
                stock = ('已见可用，数量未知' if row['availability'] == 'present' else '尚未确认') if remaining is None else str(remaining)
                lines.append(f"| {identity['name']} | {identity['cost']} / {identity['compatibility']} | {stock} | "
                             f"{metrics['success_probability']:.3%} | {metrics['expected_gain']:.6f} | "
                             f"{metrics['efficiency']:.4f} | {row['relative_efficiency']:.3f} | {metrics['samples']} |")
            lines += ['', '缺货目标仍参与比较；搜索不完整不会被记为缺货。概率为补全到 +25 的抽样估计，不是逐级策略的成功保证。', '']
        lines += ['完整事件见 `events.jsonl`；当前配装、词条、库存证据、政策阈值、置信区间及资源口径见 `latest.json`。', '']
        temporary = self.directory / 'summary.md.tmp'
        temporary.write_text('\n'.join(lines), encoding='utf-8')
        temporary.replace(self.directory / 'summary.md')
