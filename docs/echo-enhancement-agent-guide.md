# 声骸强化 Agent 操作手册

适用于本仓库的检查、数学模型强化、日志复盘与换装核验。当前策略标识为 `joint-inventory-adaptive-v1`。模型定义与完整配置说明见 [声骸联合优化与动态强化](echo-adaptive-enhancement.md)；旧版界面排查记录见 [库存扫描交接说明](echo-inventory-scanner-handoff.md)。

## 1. 确定任务范围与输入

先从当前会话确认角色、合鸣套装、配装结构、主词条、资源预算，以及用户是否要求实际强化、换装或回收。沿用用户已有授权，不要反复确认已明确的操作；只要求文档、模拟或分析时，不进入游戏消耗材料。

开始前准备以下数据，并保存来源：

| 输入 | 必须确认的内容 |
|---|---|
| 角色评分模型 | 基础面板、13 类副属性权重、共鸣效率约束、必需词条与最低提升门槛 |
| 当前五件 | 槽位、完整名字、COST、主词条、五条副词条及数值 |
| 满级库存 | 同套装、主词条兼容的全部 +25，包括当时没有单件提升的版本 |
| 候选胚子 | 按“名字 + COST + 主词条兼容组”区分；已确认数量、零库存与未知分别记录 |
| 可用资源 | 调谐器、经验管数量/可用经验、贝币，以及用户预算与读数时间 |
| 检查文件 | 本轮 `echo_inventory_check.json` 的路径、角色与套装是否一致 |

角色模板来自 `echo_character_templates.json`，其当前五件可能是历史快照。第一步扫描不会自动把模板中的当前装备更新为游戏实装；应先逐槽读回实装，校正模型输入，再生成本轮检查文件。不要把上次运行的逻辑配装当作已换好的实装。

Python 命令使用仓库 `.venv`。需要查看或控制 Windows 界面时，按当前可用的 computer-use 技能执行；这里的历史分辨率和坐标不是跨机器保证。

## 2. 只读检查与模拟预检

1. 在目标角色声骸页确认合鸣套装筛选，读回筛选标签。使用第一步 `CheckEchoInventoryTask`，检查 `Character Template`、三个 COST 主词条、权重和 `Output Directory`。
2. 核实等级确实从高到低排列。只读到“等级顺序”文字不能证明箭头方向正确。若满级扫描异常少，先检查排序，不能据此判定库存耗尽。
3. 检查输出的扫描条目、OCR 问题和停止原因。保留全部合法 +25；不要只把 `inspection.replacements` 里的单件提升送入新策略，生产入口读取的是 `inspection.scan.items`。
4. 核对检查文件里的当前五件、套装、权重、充能约束和候选池，再将路径填入第二步 `Inspection Result File`。第二步不会重新进行第一步满级扫描。
5. 保持 `Simulation Only=True` 先验证数据与日志。离线入口如下，路径替换为本轮实际文件：

```powershell
.\.venv\Scripts\python.exe scripts/simulate_echo_enhancement_step.py `
  'artifacts/echo_inventory/checks/<本轮目录>/echo_inventory_check.json' `
  --output-dir 'artifacts/echo_inventory/simulations/<本轮目录>' `
  --attempts 20 --samples 300 --tuners 1000 --premium-tubes 300
```

`--samples` 控制半成品决策采样，候选排名另外使用默认 20,000/80,000 样本。模拟资源是虚拟预算。未提供 `inspection.fresh_stock` 的候选在模拟中按持续可得处理，因此不能用这种模拟证明实机“还有胚子”。需要检验缺货逻辑时，为所有候选及兼容库存名字提供数量，包含零；格式见策略说明。

## 3. 实机配置与启动

用户已要求实际强化时，预检完成后关闭 `Simulation Only` 并启动第二步；不要把离线模拟结果描述为实机成果。确认同时只有一个任务或探针控制游戏。

| 配置 | 推荐起点 | 执行含义 |
|---|---:|---|
| Candidate Switch Ratio | 1.25 | 其他可用目标效率达到当前目标 1.25 倍时切换 |
| Missing Candidate Stop Ratio | 0.50 | 最佳可用目标低于最佳缺货目标一半时停开新胚子 |
| Fresh Candidate Samples | 20,000 | 候选共同随机样本数 |
| Maximum Decision Samples | 80,000 | 临界判断加密上限 |
| Max Candidates Per Run | 0 | 连续运行，直到预算、库存或策略停手；受控首轮可设 1 |
| Pause after Success | false | 保留后继续重评；需要逐件检视时设 true |
| Max Candidate Search Pages | 30 | 每个排序方向的搜索上限，达到上限不算确认缺货 |
| Decision Log Directory | artifacts/echo_inventory/enhancement_runs | 每轮新建证据目录 |

已有保存配置优先于新增默认值，启动前读取实际配置。1.25 与 0.50 是当前建议起点，不是已证明最优的常数。部分强化件仍沿用检查文件的半成品机会成本策略，当前推荐阈值为 0.9。

资源预算只包含愿意投入且实际可用的材料。`Resource Source=Manual` 可用于独立的模型投入上限，按当前库存填写 `Manual Tuners` 和经验配置；自动 provider 读取的余额不等于用户指定的消费上限。经验管投料可能溢出，模型上限不保证实际经验消耗精确等于预算，严格限制管数时还需核对实际投料。调谐器和经验以外，当前模型不核算贝币，应另外核验其足够支持本轮执行。初始余额与结束余额各留一份证据。

仓库提供受控实机探针；仅在已有实机授权和确认游戏页面的前提下调用。例如最多一件、只支付首次开孔的预算：

```powershell
.\.venv\Scripts\python.exe scripts/live_echo_enhancement_test.py `
  'artifacts/echo_inventory/checks/<本轮目录>/echo_inventory_check.json' `
  --tuners 10 --echo-exp 4400 --max-candidates 1 `
  --output 'artifacts/echo_inventory/live_enhancement_tests/<本轮目录>'
```

此命令会实际消耗材料，也可能锁定或标记弃置。预算不足时可以留下 +5 半成品，不能把它记成完整失败或自动回收。探针的 `--max-candidates` 必须为正数；生产任务配置中的 0 才代表不限件数。

历史实机环境是 1366×768，BitBlt 捕获包含约 30px 标题栏，曾需要管理员权限。先确认当前捕获与 OCR 正常，再运行。复用项目坐标补偿方法，不要对全部坐标再次统一加偏移。Windows 后台辅助进程用隐藏窗口启动。

## 4. 每件声骸的决策循环

1. **选择目标。** 按全套联合正提升 / 预期净材料成本排名，同时比较有货和缺货目标。不能只看成功概率、只追最弱槽位，或固定把所有某 COST 胚子烧完。
2. **确认胚子。** 当前页必须是 +0；两次读回名字、COST 和主词条，堆叠数量也需一致。卡片裸数字是数量，不能当等级。数量读不清仍可记未知，不得编造数量。
3. **逐孔强化。** 每次点击前检查可花余额、保存意图和截图；读回全部已揭晓词条，确认原词条不变且恰好多一条，再扣账。OCR 行与数值按行位置配对，不能把下一行数值借给漏读行。
4. **重新评估半成品。** 根据已揭晓词条、剩余投入、当前回收价值和新的机会成本，决定继续、保留或弃置。概率为条件采样估计，不能承诺后续会开出什么。
5. **每到 +25 联合优化。** 无论旧单件规则是否认为它有用，都检查完整库存的合法五件组合。保留同名唯一、COST/主词条约束与充能可行性，允许一次更换多个槽位。
6. **保留与记账。** 当前最优配装或尚未被同名版本同时在非充能评分与充能值压过的备选可以锁定；备选不等同于立即替换。弃置只做标记，待回收返还不能增加可花余额。
7. **重排下一目标。** 每件结束后重评；有资源 provider 时重读余额。新极品自然降低同名后续收益，无须额外人为惩罚。候选获取难度只记录 `3C > 1C > 4C` 的定性顺序，不凭空转成强化收益倍率。

联合优化更新的是逻辑配装。强化入口不会自动实际换装，也不会自动回收；这两项需要根据用户任务范围另行执行。

## 5. 停手结论与下一步

先读取 `summary.md`，再看 `latest.json` 的 `latest_ranking.action`、排名区间和 `event.snapshot`，必要时按 `events.jsonl` 的序号回溯。

| 结论或证据 | Agent 应采取的行动 |
|---|---|
| select | 继续所选目标；若先要核实库存，完成搜索再投入材料 |
| stop_missing_stock | 给出最佳合法缺货目标与最佳现有目标的效率比例，建议获取高收益胚子 |
| stop_uncertain | 说明 80,000 样本后仍跨停手线；查阅区间与用户收益偏好，不悄悄降低阈值重启 |
| stop_no_sampled_gain | 表述为本次抽样未见提升；检查模型与目标，不能断言数学上绝不提升 |
| stop_resources | 保留未完成候选，核对余额、预算和下一阶段成本 |
| search_incomplete | 检查翻页上限、OCR 和滚动证据；保持未知，不能当零库存 |
| resource_read_unavailable | 保留已扣费账本，重新读取实际资源，不能恢复成最初手填余额 |
| unreconciled_upgrade=true | 按下一节核账，先不要重试强化点击 |
| 达到件数上限或成功后暂停 | 这是运行设置的停止，不能报告为库存耗尽或无收益 |

完整搜索两个排序方向且无 OCR 问题后仍未找到，才能确认缺货；已核验数量用尽也可以记零。若缺货 3C 的替代 4C 仍在线上，可以继续 4C。提示用户获取胚子时给出完整名字、COST、主词条、相对效率及库存证据。

## 6. 中断与恢复

日志每次追加后关闭文件，`latest.json` 和摘要通过临时文件替换。保留整个运行目录，不覆盖历史日志。点击发出与结果记账之间可能中断，应按以下顺序核验：

1. 找到最后的 `upgrade_click_intent`、`upgrade_clicked`、`reveal`、`reveal_recorded`，比较对应截图与当前培养页。
2. 两次读回当前声骸身份、实际等级和完整已开词条，确认是不是正在处理的那一件；不要退回背包后误选另一件同名堆叠。
3. 重读实际资源，与事件账本对账。若已花材料却没记账，应只补一次；无法确认时保留半成品和异常证据，停止继续投入。
4. 当前没有从 JSONL 一键自动恢复生产任务的入口。底层 `resume_candidate` 需要调用方恢复会话与账本并核实培养页，不能仅凭旧日志构造状态后直接调用。
5. 再次运行前刷新检查文件和当前实装；有未完成候选时先处理其归属。新会话不应伪装成原日志的连续执行。

`scripts/xuanling_campaign.py` 是 2026-10-09 实测专用脚本，包含历史单件策略、特定角色/槽位和阶段目录假设。其 `bulk`、`resume`、`equip`、`finalize` 不是新策略的通用启动/恢复接口。新任务优先使用 `ModelEnhanceEchoTask`；复用专用脚本前必须逐项检查基线文件、角色、页面和输出目录。

## 7. 换装、回收与结算

用户要求换装时，以本轮全部已知满级库存和当前模型求出最终联合配装。实际操作前重新扫描需要的条目，按完整副词条指纹匹配，不能只按名字选同名版本。按变化槽位操作，每次读回；结束后再次读取五个槽位，验证名字、COST、主词条、五条副词条、总分和充能约束全部一致。成功发出“替换/装配”点击不是完成证据。

最终报告分开写逻辑最优与已核验实装。未换装就明确说明仅生成方案；已换装应给出变化槽位与读回证据。

回收只在用户任务包含它时执行。保留当前组合和有价值的充能/评分备选，不能把所有“不在最优五件里”的版本统一处理。实际回收后重读资源，返还按实际读数入账。

材料结算至少分三类：

- 实际库存变化：调谐器、各等级经验管、贝币；标明读数是否完整。
- 模型毛投入：按升级阶段最低经验成本计算，可能低于实际投料。
- 预计待回收与经济净耗：调谐器 30%、经验 66% 是当前模型假设；未回收时不称为实际净消耗。

锁定备选的概率可能高于直接提升概率，预计净成本应使用保留概率；否则会过度扣减假想返还。

## 8. 日志交付与离线验证

默认运行目录为 `artifacts/echo_inventory/enhancement_runs/<时间戳>/`。向用户提供可点击的 `summary.md`、`latest.json`，需要复盘时再附 `events.jsonl` 和关键截图。日志时间为 UTC，报告中说明展示时区。

最小结案记录包括：角色与检查文件、实际使用的策略参数、执行件数/开孔数、最终停止原因、当前及推荐目标、收益概率与效率口径、实际/逻辑五件变化、材料三类账、未核对操作与待处理半成品。

修改代码后使用本地虚拟环境验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_echo_*.py'
.\.venv\Scripts\python.exe -m unittest tests.TestEnhanceEchoStatusBox tests.TestEnchaneEcho
git diff --check
```

核心模块为 `src/echo/joint.py`、`src/echo/adaptive.py`、`src/echo/decision_log.py`；实机入口为 `src/task/ModelEnhanceEchoTask.py`。纯模型和界面替身测试通过，并不等于新策略整条实机路径已验收。2026-10-09 新策略完成了离线验证与历史检查点判断，整条新自适应实机流程尚未端到端验收。

如本机保留原始 campaign，可做单个历史状态的无未来数据判断：

```powershell
.\.venv\Scripts\python.exe scripts/replay_echo_adaptive_checkpoint.py `
  'artifacts/echo_inventory/xuanling_campaign_20261009' `
  --check '<当时使用的检查文件>' `
  --stock 'artifacts/echo_inventory/xuanling_campaign_20261009/initial_verified_stock.json' `
  --through 90
```

此工具只读取指定件数及之前的观测和投入。第 90 件检查点会因低收益剩余库存而停手；第 54 件在停手边界附近返回不确定。不能据此声称新策略必然获得原流程第 90 件的结果，未开出的词条不能补造为反事实轨迹。

运行产物保留在本机 `artifacts/`，该目录由 Git 忽略；克隆仓库不会自带实测原始数据。需要共享证据时提取最小、必要的样本到明确的文档或测试目录，不提交整套游戏截图。`report_xuanling_campaign.py`、`evaluate_xuanling_campaign.py` 同样依赖原始 campaign 数据，其中报告校验还引用历史检查文件路径，使用前核对其来源。
