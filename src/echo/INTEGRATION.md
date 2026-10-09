# 声骸强化模型接入 ok-ww

稳定入口位于 `src.echo`：

- `resolve_echo_resources(...)`：优先调用 ok-ww 注入的资源读取器，失败时回退到手动数量。
- `EchoEnhancementSession.advise(...)`：根据当前套装、候选名称/COST 与已开词条返回下一步动作。
- `record_reveal(...)`：在游戏实际开孔后扣除毛消耗。
- `record_discard(...)`：只登记待处理弃置声骸，不向本轮账本返还资源。
- `record_keep(...)`：接受并写回模型中的最佳同 COST 替换位置。

简体中文库存与候选名称会先通过 `src.echo.name_catalog` 中的官方 Wiki 快照约束。
精确、唯一前缀及高置信度唯一模糊匹配可得到规范名；低置信度和歧义结果必须重新 OCR，
不能提交给评分或强化模型。

当前仓库没有经过验证的调谐器/密音筒 OCR 页面，因此没有硬编码资源坐标。ok-ww 后续可实现
`get_echo_resources()` 并通过任务的 `set_echo_resource_provider(...)` 注入；用户也可在任务配置中输入
调谐器、特级密音筒与额外经验。

推荐流程已经拆成两个必须由用户分别启动的一次性任务：

1. `CheckEchoInventoryTask`：选择角色对应套装、扫描、去重、评分，生成可检视的
   `echo_inventory_check.json`；绝不进入培养。
2. `ModelEnhanceEchoTask`：用户检视第一步结果后，手动选择该 JSON 再运行；只加载其中冻结的
   五件套、评分权重、候选池和策略，不重新扫描满级库存。消费材料前会按文件内容重新确认同一合鸣套装筛选。

第一步可以直接从 GUI 启动。`Character Template`、`4C Main Stat`、`3C Main Stat`、
`1C Main Stat` 和 13 项 `Weight ...` 配置会从项目根目录的
`echo_character_templates.json` 取得默认值，并构造 `NamedEchoBuild`、候选池和策略。
JSON 可人工复制角色节点后修改；重启 UI 会刷新角色下拉列表和第一个角色的默认值。

上游角色数据适配层仍可直接注入当前五件套与候选池：

```python
from src.echo import PolicyConfig

check_task.configure_echo_check(
    sonata_set="羽落空尘之歌",
    build=current_named_build,
    candidate_pool=available_echo_identities,
    policy=PolicyConfig(required_stats=required_substats),
    output_dir="artifacts/echo_inventory/character",
)
check_result = check_task.run()
print(check_result.source_path)
```

`candidate_pool` 可省略；第一步会从本次指定套装的实际满级扫描中提取与当前五件兼容的
“名称/COST/主词条”类型。若上游已经掌握全部可用胚子类型，则应显式传入，以覆盖库存中暂时没有满级样本的类型。

用户检查 JSON 后，再明确把同一文件交给第二步：

```python
enhance_task.load_inspection_result(check_result.source_path)
enhance_task.run()
```

GUI 中对应配置为第二步的 `Inspection Result File`。选择结果文件是显式确认动作；两项任务不会自动串行。
第二步加载后会先在任务信息区列出五个当前位置的名称、COST、主属性、五条副词条，随后显示
当前模型总属性/总分和第一步排名中的最大提升位置。`Simulation Only` 默认开启：此时只运行
`StagedUpgradeCampaign`、逐事件刷新任务信息并写 JSON 决策日志，不选择套装、不点击培养，也不读取或
消耗游戏库存。验证模拟结果后，用户明确关闭该选项才进入下面的实机强化流程。

离线复现任务信息和日志更新：

```powershell
.\.venv\Scripts\python.exe scripts\simulate_echo_enhancement_step.py `
  artifacts\echo_inventory\character\check\echo_inventory_check.json
```

输出目录同时包含 `echo_enhancement_simulation.json` 和
`echo_enhancement_ui_trace.json`；后者保存信息区更新历史和全部任务日志。

该 JSON 使用带版本号的 `ok-ww.echo-inventory-check` 格式，完整保存 `NamedEchoBuild`、
`PolicyConfig`、候选池和扫描分析，所以离线测试/模拟也可以直接调用
`EchoInventoryCheckResult.read(path)`，无需启动游戏或重放 OCR。

```python
reviewed = EchoInventoryCheckResult.read("echo_inventory_check.json")
session = EchoEnhancementSession(
    reviewed.build,
    reviewed.candidate_pool,
    resources=offline_resources,
    config=reviewed.policy,
)
```

底层 `analyze_character_echo_inventory(...)` 仍可供第一步任务和测试调用。它按“选择套装 → 扫描 →
评分”封装，并只提交含五条副词条的 `+25` 条目；结构化属性键会再次去重，确保滚动重叠项不会重复提交给评分模型。

第一步开始扫描前有两个硬校验：套装标签必须精确读回目标名称，排序标签必须读回“等级顺序”
（当前游戏 UI 中为高等级优先）。任何一项无法读回都会停止，不生成可供强化使用的结果。每个 COST
按行读取右侧详情，确认首个非 `+25` 后立即结束该 COST；只有全部可见项均为 `+25` 时，才继续使用
“滚动后一整页无新结构组合”的到底条件。

也可以在已有扫描结果上单独评分；返回结果按分数提升量从高到低排列，首项即当前最大提升位置：

```python
replacements = scan_result.rank_replacements(
    current_named_build,
    minimum_gain=0.0,
    required_stats=required_substats,
)
best = replacements[0] if replacements else None
if best:
    print(best.item.identity.name, best.option.slot, best.option.delta)
```

```python
from src.echo import EchoResourceSnapshot, PolicyConfig


class InventoryReader:
    def get_echo_resources(self):
        # 返回 OCR、共享状态或其他 ok-ww 已验证来源的结果。
        return {"tuners": 6621, "premium_tubes": 1275}


enhance_task.set_echo_resource_provider(InventoryReader())
enhance_task.configure_echo_enhancement_model(
    build=current_named_build,
    candidate_pool=available_echo_identities,
    config=PolicyConfig(required_stats=required_substats),
)
```

直接注入模型上下文仍保留给集成测试和上游代码；面向用户的正式两步流程应优先使用检查结果文件。

`candidate_provider` 现在是可选项。未注入时，任务会从当前首格右侧详情读取名称、COST 和主词条兼容组；部分界面的 COST 数字是图标，任务会在 `+0` 条件下用固定副主词条（生命 456 / 攻击 20 / 攻击 30）区分 COST 1/3/4。

任务执行约定：

1. 游戏排序不能组合“未装备优先”和“等级顺序”。任务不会再假设首格为 `+0`，而是逐格跳过装备/已强化项，并对每件 `+0` 读取名称、COST 与主属性；身份不在第一步候选池或无兼容槽位时继续找下一格。首屏没有合格项时继续向下搜索，直到找到候选、没有新结构身份或达到页数上限。
2. `CONTINUE` 才进行下一孔强化；每次模型决策都会发布阶段、动作、原因、成功率和条件效率。
3. `KEEP_AND_LOCK` 在游戏内确认上锁成功后，才更新会话套装。
4. `MARK_DISCARD` 只在游戏内确认弃置状态后，才登记待处理弃置项；任务不会分解或将预计回收量计入剩余资源。
5. `STOP_RESOURCES` 保留当前声骸原状态、不标记弃置，并结束任务。
6. `Max Candidates Per Run` 控制单次最多处理件数；第二步默认 1，确认基础设施后再由用户扩大。
7. 所有强化完成后，由用户统一处理已标记的声骸；若要继续新会话，应重新读取实际资源。

每轮开始会扫描当前可见格子的卡片等级，并只点击候选 `+0`；右侧详情仍会二次确认等级及完整身份。置顶的装备项和错误主属性不会进入培养。为了让 `+0` 尽量出现在首屏，仍建议选择目标套装后使用低等级方向或“未装备优先”，但正确性不再依赖首格排序。
