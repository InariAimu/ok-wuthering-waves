# 声骸筛选结果遍历：交接说明

## 当前状态

2026-10-09 已合并上游 `9ccccde`（3.7 背包适配）。每次强化候选搜索前按上游方式切换两次升降序，恢复列表顶部并保留排序方向；不再依赖返回库存后的自动滚动位置。名称/COST/主属性筛选、强化决策、评分与优化算法保持原样。下方 8 月实机记录仅描述当时版本；本次适配需在新版本游戏中另行实测。

本次离线验证：声骸模型、库存扫描、强化、合成与配置相关 119 项测试全部通过；全仓逐模块执行共 407 项，402 项通过。剩余 4 项错误来自上游 `test_hsin.py` 的 `retarget` 测试替身未接收 `time_out/check_health` 参数，1 项失败来自 `TestChar.py::test_main_dps_prefers_lowest_buff_remaining_eligible_buffer` 的目标角色断言；相关角色、战斗源码和测试均与上游保持一致。`src/echo`、两步任务与角色模板共 16 个文件与合并前备份逐项一致，原有强化任务除候选搜索导航外的方法也保持不变。当前虚拟环境已更新至上游要求的 `ok-script==2.0.8`。

筛选结果遍历功能已经完成首版实现，并于 2026-08-26 完成纯模型、任务模块、离线遍历器测试和 COST 1/3/4 游戏实时遍历。三种 COST 的名称、主词条、五条副词条、重叠去重和结构化到底检测均已通过当前 1366×768 环境验收。面向用户的流程现已拆为两个独立任务：第一步“选择对应合鸣套装 → 扫描 → 去重 → 评分 → 输出检查文件”，用户检视后再手动启动第二步并加载该文件进行数学模型强化。

第一步检查任务只读取并导航声骸库存，不会执行培养、替换、卸下、锁定、弃置、分解或确认操作。第二步默认进入“模拟（不会操作游戏）”：先显示当前五个位置、模型总属性和第一步发现的最大提升位置，再离线重放数学模型决策并写出逐事件日志。只有用户明确关闭 `Simulation Only` 后，第二步才允许消耗材料和改变锁定/弃置状态；两种模式都不会重新执行第一步扫描。

## 已实现范围

1. 依次切换指定的 COST 1/3/4 页签。
2. 点击当前 COST 页签，利用游戏自身的页签切换行为自动回到列表顶部，然后按网格逐页遍历到底；不额外使用滚轮回顶。
3. 默认只保留 +25 声骸；卡片等级 OCR 不确定时仍会点击并通过右侧详情二次验证，避免静默漏项。
4. 每次点击后等待声骸模型和右侧 UI 加载，默认间隔 3.5 秒。
5. 从右侧详情中结构化读取：
   - 声骸名称；
   - 等级；
   - COST；
   - 主词条；
   - 五条副词条及对应公开档位；
   - COST 与主词条组成的兼容组。
6. 对滚动产生的重叠项按“名称、COST、主词条、五条副词条类型与数值”的规范化结构指纹去重；副词条显示顺序、等级、固定副主词条、截图与网格位置不参与指纹。
7. 每个访问格保存原始截图；解析失败不会进入评分模型，而是写入带截图路径的问题记录。
8. 扫描前严格读回目标套装标签，并把排序切换、读回为“等级顺序”（高等级优先）；无法确认时不开始扫描。
9. 每个 COST 按顺序确认右侧详情，遇到第一个非 `+25` 立即停止该 COST，不再访问后续格子或翻页。若当前 COST 全部可见条目均为 `+25`，再使用“滚动后一整页没有新结构组合”停止，并保留最大页数保护。
10. 输出可供评分与优化器使用的 Python 对象，以及 JSON 交接文件。
11. `EchoInventoryScanResult.rank_replacements()` 将唯一条目接入现有 `NamedEchoBuild` 评分，按提升量返回候选和最大提升位置；适配层会再次按结构键去重，避免重叠条目重复提交。
12. `select_echo_sonata_set()` 通过合鸣筛选面板精确选择套装，并以应用后的标签读回作为唯一成功条件；不得以“点击过目标”推断成功。
13. `analyze_character_echo_inventory()` 封装单角色套装选择、等级顺序设置、库存扫描、替换评分和 JSON 报告。
14. 逐件强化每轮显式点击首格并读取真实等级；只有 `+0` 才进入培养，模型候选身份可直接从右侧详情读取，不再强制要求外部 `candidate_provider`。
15. `CheckEchoInventoryTask` 与 `ModelEnhanceEchoTask` 已分别注册为“① 检查”和“② 强化”两个一次性任务，不会自动串行。
16. 第一步输出带格式类型和版本号的 `echo_inventory_check.json`，其中冻结角色评分权重、基础面板、当前五件、候选池、模型策略和完整扫描/排名结果。
17. 第二步要求用户显式选择第一步 JSON；加载时校验格式、版本及候选兼容性，并在消费材料前重新确认同一合鸣套装筛选；它只构造强化模型上下文，不扫描满级库存。相同文件可以直接用于离线模拟和独立回归测试。
18. 第二步预检通过任务信息区显示 `4C / 3C-1 / 3C-2 / 1C-1 / 1C-2` 的名称、COST、主属性、五条副词条，以及当前模型总属性、模型总分和第一步最大提升项。
19. 第二步默认 `Simulation Only=True`；模拟分支不创建游戏运行时、不选择套装、不点击培养/锁定/弃置，并将策略、资源账本、完整事件和最终套装写入版本化 JSON。关闭模拟后才沿用原实机强化入口。
20. `scripts/simulate_echo_enhancement_step.py` 可在不启动游戏的前提下记录任务信息区的每次更新及全部日志，用于独立验证 UI 行为和日志完整性。
21. 实机强化不再假设首格为 `+0`。当前游戏基础排序不支持“未装备优先 + 等级顺序”组合，已装备的 `+25` 和未装备高等级项都可能排在前面；任务会跳过明确读到的非零等级，卡片角标不可读时仍打开右侧详情复核。每个确认的 `+0` 再校验名称、COST 和主属性兼容组，只有完整身份属于第一步冻结候选池且当前存在兼容槽位时才进入培养。
22. 第二步默认 `Max Candidates Per Run=1`；一次实机验证最多处理一件。模型的每次继续、弃置、保留或资源停止决策都会同步写入任务信息区和日志，包括阶段、原因、成功率及条件效率。
23. 1366×768 BitBlt 捕获包含约 30px 标题栏；培养按钮、阶段放入、强化并调谐、弹窗及状态图标的 OCR/模板区域和点击均已统一补偿捕获偏移。
24. 第二步从第一步排名结果读取最大提升位置，并先映射到目标 COST 1/3/4 页签；点击该页签同时让游戏把对应列表恢复到顶部。当前秧秧·玄翎结果为 `1C-1`，因此实机候选搜索只进入 COST 1，不固定按 1→3→4 盲扫。
25. 目标 COST 内允许连续多屏高等级项；在实际看到第一个 `+0` 组合之前，不会以“本页没有新 +0 身份”提前停止。若最终没有合格主属性，会以“无合格胚子、零消耗完成”结束并在任务信息区记录已见身份组合。
26. 按上游 3.7 背包行为，弃置或上锁返回库存后会停在刚强化的声骸位置。每次候选搜索（包括同一 COST 连续强化）均切换两次升降序回顶，再逐格执行原有候选筛选；换 COST 时先切换页签。
27. 第一阶段“库存最佳 +25 的名称/位置”只用于决定优先 COST，不会绑定后续所有胚子的替换槽位。每个实际 `+0` 会按其自身名称、同名唯一性、COST、主属性、当前五件和五条公开副词条的理论最高档重新计算可达最大总分；理论上也不能严格提升的名称不会消耗第一次 +5。任务信息区记录该名称的理论目标位置、最大提升和最大总分。
28. 简体中文声骸名称受官方库街区 Wiki 的 199 项本地快照约束。OCR 先做宽度、空白、中点和括号规范化，再依次尝试精确匹配、唯一前缀补全和带歧义间隔的高置信度模糊匹配；不在列表、低相似度或多候选歧义时更换裁剪/阈值重新 OCR，三次失败则写入问题记录并拒绝评分/强化。`霁息兽尊·` 因同时可能为“首/身”不会自动猜测。
29. 第一步已支持直接从 UI 启动，不再要求预先调用 `configure_echo_check(...)`。UI 可选择角色模板、4C/3C/1C 主属性并调整全部 13 种副属性权重；角色套装、当前五件、候选名称、基础面板、共鸣效率区间和模型策略来自项目根目录的 `echo_character_templates.json`。启动 UI 时会读取该文件，把第一个角色的主属性和权重作为界面默认值。
30. 单个 COST 库存不足一页时，点击空格位可能仍显示上一件详情。扫描器现在把“等级 + 名称/COST/主属性/五条副属性”完全相同的第二次读数视为列表回绕信号，立即停止该 COST，不再继续点击剩余空格或滚动；第一次读数仍作为唯一存在项提交。

## 代码位置

- `src/echo/inventory.py`
  - OCR 行分组与属性归一化；
  - `EchoInventoryItem`；
  - `EchoInventoryScanIssue`；
  - `EchoInventoryScanResult`；
  - `parse_echo_inventory_item()`。
  - `rank_echo_inventory_replacements()` 与 `EchoInventoryScanResult.rank_replacements()`。
- `src/task/EnhanceEchoTask.py`
  - `select_echo_sonata_set()` 合鸣套装精确选择；
  - `analyze_character_echo_inventory()` 单角色扫描评分编排；
  - `scan_filtered_echo_inventory()` 游戏页面遍历入口；
  - COST 切换、卡片点击、等级预读、滚动、截图与报告落盘；
  - 逐件强化首格选择、真实等级校验与候选身份读取。
- `src/task/CheckEchoInventoryTask.py`
  - 第一步只读检查任务；
  - 从 UI 角色模板、主属性和副属性权重构造检查上下文；
  - 仍兼容上游直接注入角色当前五件套和模型策略；候选池可由上游提供，或从实际满级扫描中提取兼容类型；
  - 生成用户可检视的版本化交接文件。
- `echo_character_templates.json`
  - 供人工复制、修改的角色默认模板；
  - 保存套装、当前五件精确副词条、各 COST 候选名称、模型权重、基础面板和强化策略。
- `src/echo/character_templates.py`
  - 校验并加载角色模板；
  - 将 UI 主属性与权重覆盖转换为 `NamedEchoBuild`、候选池和 `PolicyConfig`。
- `src/task/ModelEnhanceEchoTask.py`
  - 第二步强化任务；
  - 只接受用户选中的第一步交接文件或显式注入上下文；
  - 加载后发布五个当前位置、模型总属性及最大提升位置；
  - 默认运行不接触游戏的数学模拟，逐事件刷新状态并写结构化日志；
  - 实机模式不调用库存扫描/评分流程。
- `src/echo/workflow.py`
  - `NamedEchoBuild`、`PolicyConfig` 与候选池的 JSON 往返；
  - `EchoInventoryCheckResult` 格式校验、读写和摘要。
- `src/echo/__init__.py`
  - 导出遍历数据接口。
- `scripts/echo_page_feasibility_probe.py`
  - `--sequence inventory` 管理员只读扫描入口；
  - `--sequence select_sonata` 套装选择诊断；
  - `--sequence inspect_candidate` 首格等级/候选身份只读诊断；
  - 失败时写出 `<label>_error.json`，避免提升权限的隐藏进程丢失异常原因。
- `scripts/simulate_echo_enhancement_step.py`
  - 第二步离线模拟入口；
  - 输出模拟报告及 UI 状态/日志轨迹，不连接或操作游戏。
- `scripts/live_echo_enhancement_test.py`
  - 一次最多处理一件的受控实机强化入口；
  - 可设置手工资源硬预算，输出结构化任务信息、决策日志和压缩 WebP 终态；
  - 提供只读状态模板诊断及中断后的弃置恢复模式。
- `tests/test_echo_inventory.py`
  - OCR 分组、元素/治疗主词条、完整性和档位映射测试。
- `tests/test_echo_inventory_scanner.py`
  - 不连接游戏的遍历流程、预筛回退、去重、到底判断和 JSON 落盘测试。

## 已完成验证（2026-08-26）

1. `tests/test_echo_inventory.py` 与 `tests/test_echo_inventory_scanner.py`：7 项通过。
2. Echo 模型及现有强化任务回归：55 项通过，只有 Qt 上游的 2 条弃用警告。
3. `EnhanceEchoTask` 与 `src.echo` 导入通过。
4. 重放 `artifacts/echo_page_feasibility/cost_tabs/cost_scan.json` 的真实 OCR 框：
   - COST 1：`cost_1:attack_percent`，两行主属性、五条副词条；
   - COST 3：`cost_3:havoc_damage`，两行主属性、五条副词条；
   - COST 4：`cost_4:crit_damage`，两行主属性、五条副词条。
5. 完整仓库测试：62 项测试与 5 个子测试通过。
6. COST 4 单页实时 OCR：正式 `completed_only=True` 路径得到 5 个唯一 `+25` 条目，每件均含名称、COST、两行主属性和五条副词条，0 个 OCR 问题。
7. COST 4 实时到底检测：第一页得到 5 个唯一组合，向下滚动后的第二页只出现这 5 个重叠组合；`duplicate_fingerprints=5`，扫描以 `no new attribute combinations after scrolling` 停止，未触发 3 页保护上限。
8. COST 3/1 实时到底检测：第一页分别得到 9/15 个唯一满级组合，第二页分别只重复这 9/15 个组合；合计 `duplicate_fingerprints=24`、0 个 OCR 问题，两种 COST 都按无新结构组合停止。
9. 本轮声骸模型、库存、强化任务关键回归：60 项通过，只有 Qt 上游的 2 条弃用警告。
10. 单角色套装选择实机通过：独立任务进程从当前库存状态精确选择 `羽落空尘之歌`，兼容顶部“全部”筛选壳层、二级合鸣下拉框和应用后隐藏标题的当前 UI。
11. 逐件强化只读实机探针通过：任务显式点击首格并读到 `+25`，正确判定不可强化，未进入培养。当前页面仅设置“未装备优先”，因此正式运行前仍必须设置等级从 0 排序。
12. 最新完整回归：96 项与 5 个子测试通过，Python 编译与 `git diff --check` 通过；仅有既有换行符提示。
13. 两步工作流新增离线回归：交接文件完整往返、错误格式拒绝、第一步仅调用检查编排、第二步加载检查结果、重选同一套装且不调用扫描。
14. 第二步默认安全模拟实跑（秧秧·玄翎 / 羽落空尘之歌）：20 次尝试、67 个模型事件、493 次任务信息更新、70 条日志；报告明确记录 `game_ui_touched=false`、`game_resources_consumed=false`。当前模型总分 172.9822，第一步最大提升位置为 `1C-1`、提升 2.9750；本次固定随机种子模拟得到 0 次替换、20 次早停弃置，未改变当前套装。
15. 实机候选预检证明排序不能提供所需组合：已装备 `+25` 会置顶，且连续三个 `+0 不熄猎手` 的主属性分别为防御%、生命%、生命%。新逐格选择器全部跳过后，正确选中 `不熄猎手 / COST 1 / 攻击% / +0`。
16. 首件受控实机强化从 `+0` 到 `+20`，依次读到暴击 7.5%、固定生命 470、暴击伤害 12.6%、固定防御 50；模型在第四条后判定完成效率低于新胚子机会成本。账本从 50/142600 变为 10/63500，准确记录净消耗 40 调谐器和 79100 经验。状态框修复后成功标记弃置并返回库存，没有分解。
17. 第二件最小预算端到端实测只允许 10 调谐器和 4400 经验：正确候选开出首条暴击伤害 19.8%，模型先记录“继续强化”，随后在 `+5` 记录“资源不足停止”，指出下一孔需 10 调谐器/12100 经验；任务保留该 `+5`、不标记弃置并正常结束。结构化报告 `completed=true`，后续只读清理诊断退出码为 0 且 `cleanup_errors=[]`。
18. 已实机验证：候选逐格过滤、主属性兼容校验、进入培养、阶段放入、强化并调谐、逐阶段 OCR、模型决策、资源扣账、提前停止、弃置标记及返回库存。成功候选的“上锁”分支仍只有离线/模板测试，尚未为了制造成功样本继续消耗材料。
19. 2026-08-27 在同一套装和原五件 `+25` 未变化的状态下，先复现“目标 COST 1 多屏搜索后无合格候选”。正式入口按第一步最大提升 `1C-1` 锁定 COST 1，确认仅有防御%候选时以 `completed=true`、0 成功、0 失败结束，调谐器 50 与经验 142600 均未扣减。
20. 同一库存继续验证卡片角标 OCR 不完整：修正为不可读格也打开详情后，只读预检从大量 `+25/+0` 混合项中成功找到 `不熄猎手 / COST 1 / 攻击% / +0`。随后第三件受控实机强化在 `+5` 开出固定生命 390，模型以成功率 2.10%、条件效率 0.0751 判弃；红色弃置状态确认成功，净消耗 10 调谐器和 4400 经验，原五件 `+25` 未改变。
21. 两件连续实机回归使用 20 调谐器/8800 经验：第一件名称级理论目标为 `1C-1`，+5 开出防御% 11.8% 后判弃；返回库存后未重复点击 COST 1，沿游戏自动滚动位置找到第二件同名攻击%胚子，+5 开出共鸣效率 8.4%。预算归零后第二件以资源保护停止、不标弃，结构化报告 `completed=true`、`cleanup_errors=[]`，原五件 `+25` 未改变。
22. 官方名称白名单接入后的只读实机预检通过：当前候选原始 OCR `不熄猎手` 规范化后仍为 `不熄猎手`，匹配方式 `exact`、相似度 1.0、重试 0 次；随后正确得到 COST 1 / 攻击% / +0，未触碰培养和资源。

验证中修复了两个会影响实扫的缺陷：元素伤害主词条曾被丢弃并误用固定攻击行作为兼容组；JSON 序列化曾访问不存在的 `EchoRoll.tier_index` 而在报告落盘时崩溃。

后续最小实扫发现旧实现的额外回顶滚轮会把列表位置扰乱。游戏在点击 1/3/4 COST 页签时会自行回到该列表顶部，因此扫描器已删除滚轮回顶，只在翻页时使用负数滚轮向下滚动。修复后 COST 4 的单页读取和重叠到底检测均已在游戏中通过。

## Python 调用接口

调用方必须已经处于声骸库存页面。正式用户流程可以直接在 UI 中打开
“① 检查套装与声骸（不强化）”，选择：

- `Character Template`：角色模板，例如“秧秧·玄翎”；
- `4C Main Stat / 3C Main Stat / 1C Main Stat`：本次接受的主属性；
- `Weight ...`：13 种副属性的模型权重。

默认模板为项目根目录的 `echo_character_templates.json`。手工新增或修改角色后重启 UI，角色下拉框及第一个角色的默认主属性/权重会重新读取。程序化集成仍可使用：

```python
check_task.configure_echo_check(
    sonata_set="羽落空尘之歌",
    build=current_named_build,
    candidate_pool=available_echo_identities,
    policy=PolicyConfig(required_stats=required_substats),
    output_dir="artifacts/echo_inventory/character",
)
check_result = check_task.run()
```

用户检视 `check_result.source_path` 后，再手动运行第二步：

```python
model_enhance_task.load_inspection_result(check_result.source_path)
model_enhance_task.run()
```

GUI 中第二步通过 `Inspection Result File` 指定同一文件。第一步不会自动触发第二步。`Simulation Only` 默认开启；可通过 `Simulation Attempts`、`Simulation Rollout Samples`、`Simulation Tuners` 和 `Simulation Premium Echo EXP Tubes` 调整纯模拟参数。模拟通过后，用户明确关闭 `Simulation Only` 才会进入实机套装确认和强化。底层扫描接口仍可用于诊断：

如果上游已经完成套装筛选，也可以只调用底层扫描：

```python
result = task.scan_filtered_echo_inventory(
    output_dir="artifacts/echo_inventory/live",
    costs=(1, 3, 4),
    completed_only=True,
    max_pages_per_cost=20,
    settle_time=3.5,
    scroll_count=20,
)
```

返回的 `EchoInventoryScanResult` 包含：

- `items`：成功解析且去重后的声骸；
- `issues`：解析失败位置、原因及截图；
- `pages_scanned`：扫描页数；
- `duplicate_fingerprints`：滚动重叠导致的重复指纹数；
- `stop_reason`：各 COST 是到达底部还是触发页数上限。

每个 `EchoInventoryItem` 可直接取得：

- `identity.name`；
- `identity.cost`；
- `identity.compatibility`；
- `level`；
- `main_stats`；
- `state`，即现有评分/优化模型使用的 `EchoState`；
- `fingerprint`；
- `screenshot`。

## 管理员探针调用

游戏以管理员权限运行时，PostMessage 探针也必须提升权限。UAC 需要用户手动确认。

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$Script = (Resolve-Path .\scripts\echo_page_feasibility_probe.py).Path
$Output = [IO.Path]::GetFullPath("artifacts\echo_inventory\live")
$Arguments = @(
    $Script,
    "--sequence", "inventory",
    "--costs", "1,3,4",
    "--max-pages", "20",
    "--settle", "3.5",
    "--output", $Output
)
Start-Process -FilePath $Python -Verb RunAs -ArgumentList $Arguments -Wait -WindowStyle Hidden
```

主报告位置为：

```text
<output>/inventory/inventory_scan.json
```

每个访问格的截图位于：

```text
<output>/inventory/cost_<COST>/page_<NN>/cell_<NN>.png
```

## JSON 核心结构

```json
{
  "items": [
    {
      "name": "千傀重楼",
      "cost": 4,
      "compatibility": "cost_4:crit_damage",
      "level": 25,
      "main_stats": [],
      "substats": [],
      "page_index": 0,
      "cell_index": 0,
      "screenshot": "...",
      "fingerprint": "..."
    }
  ],
  "issues": [],
  "pages_scanned": 0,
  "duplicate_fingerprints": 0,
  "stop_reason": "..."
}
```

## 安全边界

遍历器仅使用以下交互：

- 点击 COST 1/3/4；
- 点击左侧库存网格；
- 在库存网格内滚动。

当前实现没有调用强化按钮、快捷键 Z/C、替换、卸下或任何确认按钮。探针结束时调用 `runtime.quit()`、等待任务线程退出并关闭设备管理器，避免再次遗留 OK-WW 托盘进程。

## 已知限制与待验证点

1. 坐标已在当前 1366×768、当前 UI 缩放环境完成 COST 1/3/4 实扫；其他分辨率、宽屏模式和 UI 缩放仍待验证。
2. 卡片等级预读只是性能优化。未读到等级时会回退到点击详情；该回退已在 COST 4 混合等级页面实测，低等级条目会在详情二次校验后排除。
3. +25 条目要求成功读取五条副词条，否则进入 `issues`，不会向评分模型提交残缺数据。
4. 完全相同名称、主词条和五条副词条的两个物理声骸会拥有相同指纹并被折叠。对“寻找最佳属性”没有影响，但库存件数可能少计；如后续需要精确库存计数，应实现基于滚动重叠位置的实例级 ID。
5. 套装名称由所选角色模板或调用方提供；任务会自动选择，但尚未自动识别游戏内当前角色。
6. 扫描与评分已由 `analyze_character_echo_inventory()` 串联；角色当前五件套仍需由调用方构造成 `NamedEchoBuild`，尚未从游戏 UI 自动读取。
7. 扫描完成后停留在最后一个 COST 的列表底部，没有恢复用户原来的页签与滚动位置。
8. 大库存扫描耗时主要来自 3D 模型/UI 加载等待；应根据实测结果决定是否改成状态稳定检测，而不是降低固定间隔。
9. 逐件强化不会依赖组合排序或首格状态；它根据第一步最大提升选择目标 COST，并逐格复核到合格 `+0`。超大单 COST 库存仍受 `Max Candidate Search Pages` 保护上限约束。
10. 已完成三次单候选受控实机强化与两次弃置状态写入；成功候选的上锁分支尚未自然触发。进程在游戏已扣资源但账本尚未登记时退出的崩溃恢复仍未实现。
11. 第一步可由 GUI 角色模板直接构造数据模型，也保留 `configure_echo_check(...)` 注入接口。当前五件、副词条和候选名称尚未从角色装备页自动读取；用户在实际换装后需要同步维护模板，否则评分基线会过期。
12. 第一阶段会用 COST 与五条精确公开词条，将旧报告中的装备名称校正为本次库存 OCR 身份；若重叠页把同一完整属性项分别读成截断名和唯一较长名，则采用较长名并合并存在项。模糊匹配不会自动修改。
13. 当前手动资源账本只约束调谐器与声骸经验，强化所需贝币尚未纳入预算和扣账。
14. 名称词典当前是 2026-08-27 的简体中文官方页面快照；游戏新增声骸后需要更新 `src/echo/name_catalog.py`。繁体中文名称尚未建立独立官方词典，因此本轮白名单实机验收范围仍是简体中文界面。

## 建议的后续验证顺序

离线验证已经完成，后续可按顺序继续：

1. 对三种 COST 的 JSON 条目继续做人工抽查，重点核对五条副词条公开档位。
2. 视实际使用需要，将角色当前五件套从游戏 UI 自动读成 `NamedEchoBuild`，减少手工维护模板的成本。
3. 统计更大库存的 OCR 问题率、平均每件耗时和重复率，再决定动态等待与实例级去重方案。
4. 验证其他分辨率、宽屏模式和 UI 缩放。
