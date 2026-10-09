"""Render the observed Xuanling campaign ledger as a human-readable report."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.echo.api import EchoEnhancementSession, EchoResourceSnapshot
from src.echo.optimizer import EchoIdentity
from src.echo.simulation import EchoRoll, EchoState, EchoStat
from src.echo.workflow import EchoInventoryCheckResult, named_echo_build_from_dict, named_echo_build_to_dict


LABELS = {
    'crit_rate': '暴击', 'crit_damage': '暴伤', 'attack_percent': '攻击%',
    'attack_flat': '攻击', 'hp_percent': '生命%', 'hp_flat': '生命',
    'defense_percent': '防御%', 'defense_flat': '防御', 'energy_regen': '共鸣效率',
    'basic_attack_damage': '普攻', 'heavy_attack_damage': '重击',
    'resonance_skill_damage': '共鸣技能', 'resonance_liberation_damage': '共鸣解放',
    'havoc_damage': '湮灭伤害', 'healing_bonus': '治疗效果',
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def stat_text(roll):
    stat = roll['stat']
    value = roll.get('value')
    if value is None:
        value = EchoRoll(EchoStat(stat), roll['tier']).value
    suffix = '' if stat.endswith('_flat') else '%'
    return f"{LABELS.get(stat, stat).rstrip('%')} {value:g}{suffix}"


def rolls_text(rolls):
    return '；'.join(stat_text(roll) for roll in rolls)


def item_rolls(item):
    return item.get('substats', item.get('rolls', item.get('state', {}).get('rolls', [])))


def validate_ledger(root):
    """Reconstruct all costs, completed candidates and upgrades from events."""
    initial = named_echo_build_from_dict(read(root / 'snapshot/current_build.json'))
    pool = tuple(EchoIdentity(**item) for item in read(root / 'fresh_scan/result.json')['candidates'])
    policy = EchoInventoryCheckResult.read(ROOT / 'artifacts/echo_inventory/live_20260912_check/check/analysis_er120/echo_inventory_check.json').policy
    events = [json.loads(line) for line in (root / 'bulk/events.jsonl').read_text(encoding='utf-8').splitlines()]
    resources = next(event['resources'] for event in events if event['kind'] == 'start')
    session = EchoEnhancementSession(initial, pool, resources=EchoResourceSnapshot.manual(
        tuners=resources['tuners'], echo_exp=resources['echo_exp']), config=policy)
    states = {}
    completed = set()
    reveals = 0
    def state(data):
        return EchoState(tuple(EchoRoll(EchoStat(roll['stat']), roll['tier']) for roll in data['rolls']))
    for event in events:
        number, kind = event['candidate_number'], event['kind']
        args = event.get('args', [])
        if kind == 'selected':
            assert number not in states, f'Duplicate candidate {number}'
            assert len(completed) == len(states), 'Previous candidate was not completed'
            states[number] = EchoState()
        elif kind == 'record_reveal':
            previous, observed = state(args[0]), state(args[1])
            assert states[number] == previous and number not in completed
            cost = session.record_reveal(previous, observed)
            assert asdict(cost) == event['result']
            states[number] = observed
            reveals += 1
        elif kind in ('record_keep', 'record_discard'):
            observed = state(args[1])
            assert states[number] == observed and number not in completed
            identity = EchoIdentity(**args[0])
            if kind == 'record_keep':
                assert session.record_keep(identity, observed, args[2]) == event['result']
            else:
                session.record_discard(identity, observed, args[2])
            completed.add(number)
    progress = read(root / 'bulk/progress.json')
    assert asdict(session.snapshot()) == progress['snapshot']
    assert named_echo_build_to_dict(session.build) == progress['build']
    assert len(completed) == len(progress['outcomes'])
    expected = {'融躯战士': 44, '心傀·喜': 23, '霁息兽尊·身': 32,
                '霁息兽尊·首': 20, '不熄猎手': 26, '霁息兽尊': 15, '封庭械囿': 10}
    consumed = Counter(entry['identity']['name'] for entry in progress['outcomes'])
    if len(completed) == sum(expected.values()):
        assert consumed == expected, 'Per-kind consumption differs from initial stack counts'
    result = dict(candidates=len(states), completed=len(completed), reveals=reveals,
                  unresolved=sorted(set(states) - completed),
                  consumed_by_kind=dict(consumed),
                  remaining_by_kind={name: count-consumed[name] for name, count in expected.items()},
                  snapshot=asdict(session.snapshot()), passed=True)
    (root / 'ledger_validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def render(root):
    before = read(root / 'snapshot/plan.json')
    progress_path = root / 'bulk/progress.json'
    progress = read(progress_path) if progress_path.exists() else {}
    result_path = root / 'bulk/result.json'
    result = read(result_path) if result_path.exists() else {}
    equip_path = root / 'equip/result.json'
    equip = read(equip_path) if equip_path.exists() else {}
    final_path = root / 'final_scan/plan.json'
    final = read(final_path) if final_path.exists() else {}
    preview_path = root / 'pre_equip_final_plan.json'
    preview = read(preview_path) if preview_path.exists() else {}
    initial = named_echo_build_from_dict(before['current_build'])
    model = named_echo_build_from_dict(progress.get('build', before['current_build']))
    actual = named_echo_build_from_dict(equip['verified_build']) if 'verified_build' in equip else initial
    outcomes = progress.get('outcomes', [])
    kept = [entry for entry in outcomes if entry['outcome'] == 'record_keep']
    counts = Counter(len(entry['state']['rolls']) * 5 for entry in outcomes)
    complete = 'verified_build' in equip and not result.get('error')
    status = ('已完成强化与装备核验' if complete else
              '170 件胚子强化完成；最终换装与装备核验待 Windows 管理员权限确认' if len(outcomes) == 170 else
              '执行中 / 尚未完成最终装备核验')
    lines = ['# 玄翎声骸强化与提升报告', '', f'状态：{status}。', '',
             '口径：羽落空尘之歌；最低共鸣效率 120%，舒适线 135%，溢出线 145%。沿用原有评分、概率模型、2000 次采样和 0.9 继续强化效率阈值。评分是既有词条模型分，不等同于实战伤害提升。', '',
             '非零词条权重：暴击 1.8、暴伤 0.9、攻击百分比 0.9、固定攻击 0.1、重击 0.675、共鸣效率 0.5；共鸣效率还应用原有分段效用规则。', '',
             '## 强化前记录与最优组合', '',
             f"原装备评分 **{initial.score:.4f}**，共鸣效率 **{initial.panel.value(EchoStat.ENERGY_REGEN):.1f}%**。读取到 {len(before['inventory'])} 件满级声骸；枚举 {before['evaluated_combinations']} 个不重名组合，其中 {before['feasible_combinations']} 个满足共鸣效率约束。库存最优评分 {before['optimal_score']:.4f}，与原装备相同，因此强化前无需更换。", '',
             '| 部位 | 声骸 | 主词条 | 五条副词条（原始顺序） |', '|---|---|---|---|']
    for item in before['equipped']:
        lines.append(f"| {item['slot']} | {item['name']} | {rolls_text(item['main_stats'])} | {rolls_text(item_rolls(item))} |")
    lines += ['', '## 强化前可替换的满级库存', '',
              '以下按主词条匹配部位列出。两个 3C 位、两个 1C 位各自共享对应候选表；实际组合要求五件声骸名称不重复。', '']
    for slot in ('4C', '3C-1', '1C-1'):
        group = {'4C': '4C 位', '3C-1': '两个 3C 位', '1C-1': '两个 1C 位'}[slot]
        lines += [f'### {group}', '', '| 声骸 | 主词条 | 副词条 |', '|---|---|---|']
        for item in before['per_slot_candidates'][slot]:
            lines.append(f"| {item['name']} | {rolls_text(item['main_stats'])} | {rolls_text(item_rolls(item))} |")
        lines.append('')
    eligible = {json.dumps(item, sort_keys=True) for pool in before['per_slot_candidates'].values() for item in pool}
    excluded = [item for item in before['inventory'] if json.dumps(item, sort_keys=True) not in eligible]
    if excluded:
        lines += ['### 已满级但主词条不匹配', '', '| 声骸 | 主词条 | 副词条 |', '|---|---|---|']
        for item in excluded:
            lines.append(f"| {item['name']} | {rolls_text(item['main_stats'])} | {rolls_text(item_rolls(item))} |")
    lines += ['', '## 胚子与材料', '',
              '开始时实读可用胚子 170 件：融躯战士 44、心傀·喜 23、霁息兽尊·身 32、霁息兽尊·首 20、不熄猎手 26（均为 1C 攻击主词条）；霁息兽尊 15、封庭械囿 10（均为 3C 湮灭主词条）。4C 无可用胚子。', '',
              '开始时调谐器 10,748；金色经验管 1,936、紫色经验管 667，合计 11,014,000 经验；贝币 5,808,379。弃置只做标记，未回收，未将潜在返还计入本轮可用资源。', '']
    snapshot = progress.get('snapshot', {})
    if snapshot:
        lines += [f"已处理 **{len(outcomes)} / 170** 件，模型保留 **{len(kept)}** 件。停止等级分布：" + '、'.join(f'+{level}: {count} 件' for level, count in sorted(counts.items())) + '。', '',
                  f"模型账本消耗：调谐器 **{snapshot['gross_tuners']:g}**、经验 **{snapshot['gross_exp']:g}**；剩余调谐器 {snapshot['tuners']:g}、经验 {snapshot['echo_exp']:g}。最终管数、贝币以收尾实读为准。", '',
                  f"已弃置声骸后续可回收理论值：调谐器 {snapshot['recoverable_tuners_later']:g}、经验 {snapshot['recoverable_exp_later']:g}；本轮未回收。", '']
    audit_path = root / 'resource_audit.json'
    if audit_path.exists():
        audit = read(audit_path)
        lines += ['### 最终材料实读', '',
                  f"剩余调谐器 {audit['tuners']:,}、贝币 {audit['shell_credits']:,}。实际消耗调谐器 **{10748-audit['tuners']:,}**、贝币 **{5808379-audit['shell_credits']:,}**。", '']
        if audit.get('gold_tubes') is not None and audit.get('purple_tubes') is not None:
            spent_exp = 11014000 - (audit['gold_tubes'] * 5000 + audit['purple_tubes'] * 2000)
            lines += [f"剩余金色经验管 {audit['gold_tubes']:,}、紫色经验管 {audit['purple_tubes']:,}；消耗金色经验管 **{1936-audit['gold_tubes']:,}**、紫色经验管 **{667-audit['purple_tubes']:,}**，经验管价值合计 **{spent_exp:,}** 经验。", '',
                      '实际经验管消耗与模型最低升级经验的差额，主要来自阶段投料留下的额外经验；未回收弃置声骸。', '']
        else:
            lines += ['调谐器及贝币已从最后一次强化截图核实；最终经验管数量待收尾实读。上面的经验成本为模型最低升级经验，未包含阶段投料留下的额外经验。', '']
    lines += ['## 提升与装备状态', '',
              f'强化前评分 {initial.score:.4f}；逐件强化模型当前组合评分 {model.score:.4f}。', '']
    if 'verified_build' not in equip and preview:
        planned = named_echo_build_from_dict(preview['optimal_build'])
        lines += [f"综合强化前库存与本轮全部满级结果，共 {len(preview['inventory'])} 件满级声骸，最优待换方案评分 **{planned.score:.4f}**，相对开局 **+{planned.score-initial.score:.4f}（+{(planned.score/initial.score-1)*100:.2f}% 模型评分）**，共鸣效率 **{planned.panel.value(EchoStat.ENERGY_REGEN):.1f}%**。", '',
                  '**下表为待换方案，尚未实际换装。** 收尾程序启动时 Windows 管理员授权返回“操作已被用户取消”；已保存全部证据，等待确认后继续最终扫描、换装及核验。', '',
                  '| 部位 | 待换装备 | 副词条 |', '|---|---|---|']
        for echo in planned.echoes:
            lines.append(f"| {echo.slot} | {echo.identity.name} | {rolls_text([dict(stat=roll.stat.value, value=roll.value) for roll in echo.state.rolls])} |")
        lines.append('')
    if 'verified_build' in equip:
        lines += [f'最终提升 **{actual.score-initial.score:.4f} 分（{(actual.score/initial.score-1)*100:.2f}% 模型评分）**；共鸣效率 {actual.panel.value(EchoStat.ENERGY_REGEN):.1f}%。', '',
                  '| 部位 | 最终装备 | 副词条 |', '|---|---|---|']
        for echo in actual.echoes:
            lines.append(f"| {echo.slot} | {echo.identity.name} | {rolls_text([dict(stat=roll.stat.value, value=roll.value) for roll in echo.state.rolls])} |")
        lines += ['', '### 有效属性变化', '',
                  '除共鸣效率含模型中已配置的基础/套装 110% 外，下表为五件声骸副词条合计；不等同于角色完整面板。', '',
                  '| 属性 | 强化前 | 最终 | 变化 | 评分变化 |', '|---|---|---|---|---|']
        for stat in (EchoStat.CRIT_RATE, EchoStat.CRIT_DAMAGE, EchoStat.ATTACK_PERCENT,
                     EchoStat.ATTACK_FLAT, EchoStat.HEAVY_ATTACK_DAMAGE, EchoStat.ENERGY_REGEN):
            old, new = initial.panel.value(stat), actual.panel.value(stat)
            suffix = '' if stat.value.endswith('_flat') else '%'
            weight = actual.profile.weights[stat]
            contribution = ((actual.profile.energy_policy.utility(new, weight) - actual.profile.energy_policy.utility(old, weight))
                            if stat == EchoStat.ENERGY_REGEN and actual.profile.energy_policy else (new-old)*weight)
            lines.append(f'| {LABELS[stat.value]} | {old:g}{suffix} | {new:g}{suffix} | {new-old:+g}{suffix} | {contribution:+.4f} |')
        lines += ['', f"实际更换 {len(equip['changes'])} 个部位，已逐件读取并核对装备后的五条副词条。", '']
        lines += ['### 新保留声骸的最终去向', '', '| 序号 | 声骸 | 最终使用 | 副词条 |', '|---|---|---|---|']
        for entry in kept:
            rolled = tuple(EchoRoll(EchoStat(roll['stat']), roll['tier']) for roll in entry['state']['rolls'])
            used = next((echo.slot for echo in actual.echoes if echo.identity == EchoIdentity(**entry['identity']) and echo.state.rolls == rolled), None)
            lines.append(f"| {entry['number']} | {entry['identity']['name']} | {used or '锁定备用'} | {rolls_text(entry['state']['rolls'])} |")
    if result.get('error'):
        lines += ['', '运行中断原因：' + result['error']['message'], '']
    if result.get('stopped_for_resources'):
        lines += ['', '停止原因：材料不足以继续下一阶段。', '']
    elif result and not result.get('error') and len(outcomes) == 170:
        lines += ['', '停止原因：开局确认的 170 件适用主词条胚子已全部处理，材料仍有剩余。', '']
    validation_path = root / 'ledger_validation.json'
    if validation_path.exists():
        validation = read(validation_path)
        lines += [f"账本校验：重放 {validation['reveals']} 次词条揭晓和全部保留/弃置记录，资源消耗与模型组合完全一致；未闭环候选 {len(validation['unresolved'])} 件。", '']
    interruptions = list((root / 'bulk').glob('interruption_*/original_result.json'))
    if interruptions:
        lines += ['### 运行恢复记录', '',
                  '第 29 件在 +20 时发生 OCR 数值错行，自动停止。截图及两次重读确认实际词条后，补记已发生的调谐与经验消耗，并从该件继续。按行识别和断点恢复已加入回归验证；原评分、概率与强化停止规则未修改。', '']
    lines += ['', '## 逐件强化结果', '', '| 序号 | 胚子 | 停止等级 | 处理结果 | 已揭晓词条 |', '|---|---|---|---|---|']
    for entry in outcomes:
        lines.append(f"| {entry['number']} | {entry['identity']['name']} | +{len(entry['state']['rolls'])*5} | {'保留并锁定' if entry['outcome']=='record_keep' else '标记弃置'} | {rolls_text(entry['state']['rolls'])} |")
    lines += ['', '## 原始证据', '',
              '- `snapshot/plan.json`：原装备、全部满级库存、最优组合与逐部位候选。',
              '- `fresh_scan/`：胚子名称、主词条、各页堆叠截图；识别失败条目保留为 issues，未进入候选池。',
              '- `resources_open/after.webp`、`resources_materials/after.webp`：开局材料和贝币实读。',
              '- `bulk/events.jsonl`：每次选择、模型决策、词条揭晓、材料扣除与保留/弃置事件。',
              '- `bulk/candidate_*.webp`：逐阶段词条截图。',
              '- `final_scan/`、`equip/`：最终全库存优化与实际换装核验（完成后生成）。', '']
    path = root / '玄翎声骸强化报告.md'
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--validate', action='store_true')
    args = parser.parse_args()
    if args.validate:
        print(validate_ledger(args.root.resolve()))
    print(render(args.root.resolve()))
