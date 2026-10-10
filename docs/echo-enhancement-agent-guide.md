# 声骸强化操作手册：自动执行与异常介入

适用于本仓库的检查、数学模型强化、日志复盘与换装核验。当前策略标识为 `joint-inventory-adaptive-v1`。模型定义与完整配置说明见 [声骸联合优化与动态强化](echo-adaptive-enhancement.md)；旧版界面排查记录见 [库存扫描交接说明](echo-inventory-scanner-handoff.md)。

## 0. 执行原则：常规流程交给程序

默认由 `CheckEchoInventoryTask`、`ModelEnhanceEchoTask` 和 `AdaptiveEnhancementSession` 执行已实现的扫描、候选搜索、逐孔强化、策略判断、材料记账和联合优化。Agent 负责确认任务输入、配置与启动、处理异常、审计结案，不充当逐孔点击和逐件决策的调度器。这里的自动执行指遵循已有代码规则；蒙特卡洛采样仍是概率估计。

正常任务按“预检 → 启动一次自动执行 → 等待完成或异常 → 文本审计”推进。用户已授权且预检通过后，使用已有预算和停手条件持续执行；不要为了让 agent 看结果而反复运行单件探针、逐件重启任务或开启成功暂停。单件探针用于首次验证执行链路、复现异常或用户明确限定一件的场景。

### 0.1 程序与 agent 的职责边界

| 工作 | 默认负责人 | Agent 何时介入 |
|---|---|---|
| 扫描、OCR、身份/词条重复读回 | 现有扫描与界面代码 | 读数冲突、输入缺失、排序/扫描覆盖无法确认 |
| 候选排名、逐孔继续/弃置、+25 联合优化 | 数学策略 | 模型输入错误或用户要求调整目标；不凭截图临时改策略 |
| 常规强化点击、锁定/弃置标记、账本更新 | 强化任务 | 点击结果未核账、操作异常或界面状态不匹配 |
| 切换目标、正常缺货与资源停手 | 强化任务 | 搜索不完整、停手原因与库存证据矛盾 |
| 任务范围、预算、实装基线与最终审计 | Agent | 开始和结束集中核验，优先使用结构化读数 |
| 异常恢复、实际换装与回收 | 按相应章节单独处理 | 当前强化入口未自动覆盖这些操作，不假定已经执行 |

保留/弃置、正常候选切换、一次 +25 联合优化，以及证据完整的策略停手，均不是必须唤起 agent 或查看截图的事件。`stop_uncertain` 优先检查采样区间和收益偏好；图片不能消除统计不确定性。

### 0.2 运行期间只做必要的文本监控

启动后记录执行器/进程标识、本轮日志目录、检查文件和预算。先检查是否已有同一轮执行器运行，避免重复启动；同一时刻只能有一个控制游戏的执行器。默认由 agent 启动本手册的独立入口并保留进程会话，等待进程结束后检查退出码和 `result.json`；异常退出即可进入诊断，无须另加主动唤醒 agent 的服务。长任务仅在必要时按分钟级检查进程状态和 `summary.md` / `latest.json` 的变化，不因每个 `reveal`、`kept` 或 `discarded` 事件发起一次模型判断。

每次只读取本轮的少量字段：最后事件序号/时间、完成件数、活动候选、`stop_reason`、`unreconciled_upgrade`、材料余额和最新调度结论。需要定位问题时再按序号读取相关事件，不反复输出整份 `events.jsonl`、历史图片列表或完整运行目录。

`unreconciled_upgrade=true` 在点击前置意图到 `reveal_recorded` 之间可以是正常的瞬时状态；执行器仍在推进时不抢占操作。若执行器异常退出、明确报错，或持续无进展且最后状态仍未核账，才按第 6 节处理。采样计算和库存搜索可能较慢，单次摘要未更新不能直接判为卡死；结合进程和任务日志确认。

`resource_read_unavailable` 当前代码会保留账本，未实现自动暂停。Agent 发现该事件后应检查现有账本是否仍满足本轮预算与资源核验要求；无法确认时，在可确认的操作边界暂停执行器并核账，不能假定程序已停下，也不能边强化边手工读取或操作游戏。当前没有通用的异常唤醒/自动恢复监督器；以上是操作约定，不代表已实现这些功能。

### 0.3 看图必须有具体问题

证据默认按“结构化输出 → 相关文本事件/OCR 结果 → 必要截图”查阅。预检、结束核验和恢复所需的重复读回，优先交给已有读取逻辑；要求两次读数并不等于要求 agent 查看两张截图。读数一致、扫描覆盖已确认且账本闭合时，不额外逐件人工看图。

仅在以下情况查看与疑点直接相关的图片：OCR/身份/词条读数冲突；页面、捕获或排序方向无法由读数确认；搜索不完整或滚动末尾证据不足；强化点击结果与账本不一致；锁定、换装或回收的结构化核验失败。查看前写明要解决的问题，先取 1–2 张相关帧，问题已解决就停止，不遍历整轮截图。若证据仍不足，再有针对性地补充。

程序为审计保存截图，与把截图送入模型上下文是两件事。保留原始本地证据及日志路径；模型确需看图时生成包含必要字段的 WebP 裁剪或适度缩放副本，保证等级、名字和数值清晰，不覆盖原证据。WebP 只能减小单张体积，不能替代减少看图次数；不要通过输出 Base64 把图片混入文本日志。

确需复杂视觉判断时，可将少量相关图片交给空白上下文、短生命周期的视觉 worker，只返回字段、疑点及证据路径；当前工具应明确设置 `fork_turns="none"`，其他 harness 使用对应的独立上下文机制。不要继承完整强化历史、长期复用一个不断积图的 worker，或把其图片结果重新附回主会话。此方法是可选的异常诊断手段；若请求体仍携带子 agent 图片历史，需使用独立的一次性 harness，不能仅凭“用了 subagent”认定上传量已降低。

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

角色模板来自 `echo_character_templates.json`，其当前五件可能是历史快照。第一步扫描不会自动把模板中的当前装备更新为游戏实装；应先逐槽读回实装，校正模型输入，再生成本轮检查文件。读回优先使用已有读取逻辑，不要求 agent 逐槽看图；缺少可用读取入口或输出有疑点时再定点核验。不要把上次运行的逻辑配装当作已换好的实装。

Python 命令使用仓库 `.venv`。需要查看或控制 Windows 界面时，按当前可用的 computer-use 技能执行；这里的历史分辨率和坐标不是跨机器保证。

### 1.1 新环境与独立入口

在 Windows 的 fresh checkout 根目录初始化模板子模块，并按仓库依赖准备 Python 3.12 环境：

```powershell
git submodule update --init --recursive
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/run_echo_workflow.py --help
```

已有 `.venv` 时直接复用，不重复创建。运行代码、角色模板、测试和说明均由 Git 管理；`artifacts/`、`configs/` 和 `.venv/` 是本地数据，不能作为新环境的代码依赖。运行资产依赖初始化后的 `ok_templates` 子模块和项目运行库；OCR 等运行资源按项目正常初始化流程准备。没有游戏的环境可以查看 CLI 帮助并运行离线模型测试，不能据此声称实机捕获已验收。

正式独立入口为 `scripts/run_echo_workflow.py`，公共实现为 `src/echo/live_workflow.py`。入口自动定位当前 checkout 根目录，使用本轮独立配置目录，关闭所有后台触发任务，启用运行库互斥检查，不连接已有 OK-WW 实例。启动前退出其他控制游戏的 OK-WW 进程；目标游戏必须已启动，并使用与游戏相匹配的权限。代码不内置管理员账户或本机绝对路径。

| 命令 | 页面前提与结果 |
|---|---|
| `observe` | 当前游戏页面；保存 WebP 和 OCR 文本，不强化 |
| `equipment --character <角色>` | 目标角色声骸装备页，左侧五个槽位可见；双读实装并按模板评分，不换装 |
| `check --character <角色>` | 同上；先双读实装，再扫描同套装满级库存，输出本轮检查文件 |
| `live --inspection <文件> --tuners <预算> --echo-exp <预算>` | 声骸库存详情页；使用已审核检查文件和明确预算连续强化 |
| `stock --inspection <文件> --cost <1/3/4>` | 声骸库存页；独立双排序、整页属性指纹核验，输出缺货证据和双读条目，不强化 |

`equipment` / `check` 直接读取仓库 `echo_character_templates.json`，可通过 `--templates` 指定自己的模板；不会加载旧 `configs/CheckEchoInventoryTask.json` 的权重覆盖。角色和页面必须由本轮确认，代码不会自动切换角色。模板里五件的词条仅为历史基线，`check` 用本次实装读数构造模型，并拒绝与模板 COST/主词条不兼容的实装。

例如为“心”生成新检查文件：

```powershell
.\.venv\Scripts\python.exe scripts/run_echo_workflow.py check `
  --character '心' --output 'artifacts/echo_inventory/current_check'
```

成功后读取 `artifacts/echo_inventory/current_check/result.json`，其 `inspection` 指向 `check/echo_inventory_check.json`。每次 `--output` 必须是新目录，避免覆盖旧证据；不传时自动生成带时间戳的目录。检查或实机失败返回退出码 1；成功返回 0。输入校验、执行和清理错误写入 `result.json` 的 `status`、`error`、`cleanup_errors`，清理前先保存执行结果。启动参数错误使用 argparse 的退出码 2。

观察与检查不消费强化材料，但会读取/导航游戏页面。不要将“只读”理解为完全不点击界面。若通过 PowerShell `Start-Process` 提升权限，使用隐藏窗口并记录实际子进程，等待其结束、检查 `ExitCode` 和结果文件；仅取得启动 PID 或启动命令返回 0，不能证明强化成功。

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

用户已要求实际强化时，预检完成后关闭 `Simulation Only` 并启动第二步；不要把离线模拟结果描述为实机成果。确认同时只有一个任务或探针控制游戏。正常批量任务使用第二步的连续执行循环；受控探针验证通过后，不继续以“一件探针 + agent 看图”的方式串起整轮任务。

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

Agent 自行启动时使用正式独立入口。下例预算仅为命令格式示例，必须替换为本轮已核验且愿意投入的数值：

```powershell
.\.venv\Scripts\python.exe scripts/run_echo_workflow.py live `
  --inspection 'artifacts/echo_inventory/current_check/check/echo_inventory_check.json' `
  --tuners 100 --echo-exp 100000 `
  --output 'artifacts/echo_inventory/current_live'
```

默认 `--max-candidates 0`、不在成功后暂停，使用 1.25 / 0.50 和 20,000 / 80,000 样本；可用对应 CLI 参数明确调整。`--settle` 默认 1.5 秒、`--scroll-count` 默认 5，替代历史 helper 的搜索等待时间 monkey patch。入口只使用本轮配置和检查文件，不读取旧轮次的 `current_check.txt` 或历史配装结果。点击/OCR 失败通过异常结束并写报告；它不新增卡死监督器，也不实现自动恢复、换装或回收。

仓库提供受控实机探针；仅在已有实机授权和确认游戏页面的前提下调用。例如最多一件、只支付首次开孔的预算：

```powershell
.\.venv\Scripts\python.exe scripts/live_echo_enhancement_test.py `
  'artifacts/echo_inventory/checks/<本轮目录>/echo_inventory_check.json' `
  --tuners 10 --echo-exp 4400 --max-candidates 1 `
  --output 'artifacts/echo_inventory/live_enhancement_tests/<本轮目录>'
```

此命令会实际消耗材料，也可能锁定或标记弃置。预算不足时可以留下 +5 半成品，不能把它记成完整失败或自动回收。探针的 `--max-candidates` 必须为正数；生产任务配置中的 0 才代表不限件数。

历史实机环境是 1366×768，BitBlt 捕获包含约 30px 标题栏，曾需要管理员权限。先确认当前捕获与 OCR 正常，再运行。复用项目坐标补偿方法，不要对全部坐标再次统一加偏移。Windows 后台辅助进程用隐藏窗口启动。

2026-10-10 实测补充：普通权限捕获可能读到遮挡游戏的前台窗口；这不能视为有效游戏截图。紧凑布局的等级裁剪必须包含完整的 `+25` 字形。候选搜索已排除卡片间隙的动画，但千傀重楼等大型模型仍可能透过卡片内部干扰滚动比较；不要因此降低缺货核验标准。若本轮已两次确认堆叠数量，且对应数量全部逐件完成、材料与点击已核账，可按“已核验数量用尽”记录零库存，并保存数量与事件序列证据；否则保留未知。第一步仍须以实际卡片等级确认排序方向，不能只依赖 `等级顺序` 标签。游戏可能始终将已装备声骸置顶，因此顶部实装件的 `+25` 也不能证明未装备库存从高到低；应核对未装备卡片的等级顺序，并确认本轮已知新满级件全部进入扫描结果。独立核验滚动末尾时，可对照连续整页的等级与完整属性指纹；原始 OCR 标签杂字符不应参与属性指纹比较。

## 4. 执行器自动完成的逐件循环

以下是现有代码必须执行和记录的规则，不是 agent 每件都要重复操作或人工审核的清单。程序正常推进时不插入截图审阅或手工排名；只有第 0 节所列异常才转入 agent 诊断。

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
| select | 运行中的执行器自动搜索并继续；Agent 不重复发起点击，已停止时先查停止原因 |
| stop_missing_stock | 给出最佳合法缺货目标与最佳现有目标的效率比例，建议获取高收益胚子 |
| stop_uncertain | 说明 80,000 样本后仍跨停手线；查阅区间与用户收益偏好，不悄悄降低阈值重启 |
| stop_no_sampled_gain | 表述为本次抽样未见提升；检查模型与目标，不能断言数学上绝不提升 |
| stop_resources | 保留未完成候选，核对余额、预算和下一阶段成本 |
| search_incomplete | 检查翻页上限、OCR 和滚动证据；保持未知，不能当零库存 |
| resource_read_unavailable | 代码保留已扣费账本但不会因此自动暂停；按第 0.2 节核验，不能恢复成最初手填余额 |
| unreconciled_upgrade=true | 推进中的瞬时状态不抢占；异常退出或持续未核账时按下一节处理，不重试强化点击 |
| 达到件数上限或成功后暂停 | 这是运行设置的停止，不能报告为库存耗尽或无收益 |

完整搜索两个排序方向且无 OCR 问题后仍未找到，才能确认缺货；已核验数量用尽也可以记零。若缺货 3C 的替代 4C 仍在线上，可以继续 4C。提示用户获取胚子时给出完整名字、COST、主词条、相对效率及库存证据。

搜索像素比较受动画影响时，在原执行器停止后使用独立的整页属性指纹核验，不必逐页让 agent 看图：

```powershell
.\.venv\Scripts\python.exe scripts/run_echo_workflow.py stock `
  --inspection 'artifacts/echo_inventory/current_check/check/echo_inventory_check.json' `
  --cost 3 --output 'artifacts/echo_inventory/current_stock'
```

仅两种排序都到达连续相同整页指纹、且所有卡片两次读取一致时，输出 `verified_missing_stock.json`；达到页数上限或读数冲突则失败，不能据此记零。需要沿用这份证据时，为 `live` 显式传入 `--verified-stock <文件>`。文件绑定检查文件的 SHA-256，检查文件变化后不能直接复用；还必须确认仍是同一库存状态，没有新增胚子、换账号等变化。数量守恒已放入 `AdaptiveEnhancementSession.observe_stock`：同一 session 后续数量读不清时保留已经逐件扣减的正数，新读到的数值仍优先；新 session 不继承旧数量。

## 6. 中断与恢复

日志每次追加后关闭文件，`latest.json` 和摘要通过临时文件替换。保留整个运行目录，不覆盖历史日志。先确认原执行器已经停止或暂停且不会继续点击，再进行界面核验或恢复；重启 Codex 或模型请求中断，不等于独立运行的游戏脚本已经停止。点击发出与结果记账之间可能中断，应按以下顺序核验：

1. 找到最后的 `upgrade_click_intent`、`upgrade_clicked`、`reveal`、`reveal_recorded`，比较对应截图与当前培养页。
2. 两次读回当前声骸身份、实际等级和完整已开词条，确认是不是正在处理的那一件；不要退回背包后误选另一件同名堆叠。
3. 重读实际资源，与事件账本对账。若已花材料却没记账，应只补一次；无法确认时保留半成品和异常证据，停止继续投入。
4. 当前没有从 JSONL 一键自动恢复生产任务的入口。底层 `resume_candidate` 需要调用方恢复会话与账本并核实培养页，不能仅凭旧日志构造状态后直接调用。
5. 再次运行前刷新检查文件和当前实装；有未完成候选时先处理其归属。新会话不应伪装成原日志的连续执行。

`scripts/xuanling_campaign.py` 是 2026-10-09 实测专用脚本，包含历史单件策略、特定角色/槽位和阶段目录假设。其 `bulk`、`resume`、`equip`、`finalize` 不是新策略的通用启动/恢复接口。新任务使用 `scripts/run_echo_workflow.py` / `ModelEnhanceEchoTask`；复用专用脚本前必须逐项检查基线文件、角色、页面和输出目录。

## 7. 换装、回收与结算

用户要求换装时，以本轮全部已知满级库存和当前模型求出最终联合配装。实际操作前重新扫描需要的条目，按完整副词条指纹匹配，不能只按名字选同名版本。按变化槽位操作，每次读回；结束后再次读取五个槽位，验证名字、COST、主词条、五条副词条、总分和充能约束全部一致。读回优先使用结构化输出，发现不一致再定点看图；减少看图不能省略实装核验。成功发出“替换/装配”点击不是完成证据。

最终报告分开写逻辑最优与已核验实装。未换装就明确说明仅生成方案；已换装应给出变化槽位与读回证据。

回收只在用户任务包含它时执行。保留当前组合和有价值的充能/评分备选，不能把所有“不在最优五件里”的版本统一处理。实际回收后重读资源，返还按实际读数入账。

材料结算至少分三类：

- 实际库存变化：调谐器、各等级经验管、贝币；标明读数是否完整。
- 模型毛投入：按升级阶段最低经验成本计算，可能低于实际投料。
- 预计待回收与经济净耗：调谐器 30%、经验 66% 是当前模型假设；未回收时不称为实际净消耗。

锁定备选的概率可能高于直接提升概率，预计净成本应使用保留概率；否则会过度扣减假想返还。

## 8. 日志交付与离线验证

默认运行目录为 `artifacts/echo_inventory/enhancement_runs/<时间戳>/`。向用户提供可点击的 `summary.md`、`latest.json`，需要复盘时再附 `events.jsonl` 和关键截图。日志时间为 UTC，报告中说明展示时区。

结案先汇总结构化事件并检查预算、库存与点击账本是否闭合；没有异常时不为了写报告遍历截图。报告引用本地证据路径即可，只有解释具体异常时才附必要图片。本手册的职责分工不会自动修改截图保存格式、增加异常监督器或补齐恢复/换装/回收入口；这些代码能力需单独实现和验证。

离线审计使用通用入口，显式列出同一模型各轮 `events.jsonl`，按实际执行顺序传入；以下路径替换为本轮实际目录：

```powershell
.\.venv\Scripts\python.exe scripts/audit_echo_workflow.py `
  --inspection '<本轮检查文件>' --events '<第一轮/events.jsonl>' '<第二轮/events.jsonl>' `
  --output 'artifacts/echo_inventory/current_audit'
```

输出 `summary.md`、`audit.json` 和 `verified_missing_stock.json`，校验点击/揭晓/结算数量、事件序号、模型毛投入与账本。只接受 `live` 日志，拒绝模拟日志、重复日志和不同评分模型；未核账操作返回 1 并保留审计结果，不生成缺货结论。统计只证明模型账与逻辑配装，不把它当成实际资源变化或已换装；实际初末资源仍单独读回。历史 `audit_final.py` / `build_report.py` 中写死的角色、件数、余额、日期与停止结论不迁入通用代码。

若当前满级扫描遗漏了已独立双读的条目，可先运行本轮 `stock` 核验，再显式补全检查文件：

```powershell
.\.venv\Scripts\python.exe scripts/reconcile_echo_inventory.py `
  --inspection '<本轮检查文件>' --stock-audits '<本轮stock/result.json>' `
  --output 'artifacts/echo_inventory/current_reconciled'
```

只合并绑定同一检查文件、完整核验且双读成功的 +25 条目，按属性指纹去重并重新计算替换建议；保留当前实装模型，不直接复制旧库存或改写游戏装备。原扫描的 OCR 问题不能通过补充条目自动清空，翻页上限等原停止证据仍保留，必须解决后才能作为新的实机输入。

历史 helper 的替代路径：

| 本地历史脚本/行为 | 受版本控制的替代入口 |
|---|---|
| `xuanling_adaptive_*/run_current.py`、`hsin_adaptive_*/run_current.py` | `scripts/run_echo_workflow.py` |
| `hsin_equipment_*/read_current.py` 的装备读取 | `run_echo_workflow.py equipment` |
| `stock3`、临时缺货/数量 monkey patch | `run_echo_workflow.py stock`、`configure_live_observations`、核心 `observe_stock` |
| `audit_final.py`、`build_report.py` 的通用账本核验 | `scripts/audit_echo_workflow.py` |
| `reconcile_final_check.py` 的已核验条目补全 | `scripts/reconcile_echo_inventory.py` |

按固定 PID/旧目录结束进程的 `stop_*.py` 和生成另一份脚本的 `prepare_helper.py` 不作为通用入口；由 agent 管理自己启动的进程，直接调用上述正式代码。旧 helper 保留为历史证据，新任务不执行或复制它们。

最小结案记录包括：角色与检查文件、实际使用的策略参数、执行件数/开孔数、最终停止原因、当前及推荐目标、收益概率与效率口径、实际/逻辑五件变化、材料三类账、未核对操作与待处理半成品。

修改代码后使用本地虚拟环境验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_echo_*.py'
.\.venv\Scripts\python.exe -m unittest tests.TestEnhanceEchoStatusBox tests.TestEnchaneEcho
git diff --check
```

核心模块为 `src/echo/joint.py`、`src/echo/adaptive.py`、`src/echo/decision_log.py`；实机入口为 `src/task/ModelEnhanceEchoTask.py`。纯模型和界面替身测试通过，并不等于新策略整条实机路径已验收。2026-10-09 完成离线验证与历史检查点判断；2026-10-10 实际处理玄翎 29 件胚子，验证了逐孔决策、封庭缺货后切换千傀及 +25 联合优化。最终停手使用已核验数量耗尽的证据，由独立 `post-run-audit` 计算；大型模型干扰下的自动搜索仍需 agent 核验，不应称为全自动端到端验收。该轮保留 0 件，无换装和回收；详细证据保留在本机 `artifacts/echo_inventory/xuanling_adaptive_20261010/`。

如本机保留原始 campaign，可做单个历史状态的无未来数据判断：

```powershell
.\.venv\Scripts\python.exe scripts/replay_echo_adaptive_checkpoint.py `
  'artifacts/echo_inventory/xuanling_campaign_20261009' `
  --check '<当时使用的检查文件>' `
  --stock 'artifacts/echo_inventory/xuanling_campaign_20261009/initial_verified_stock.json' `
  --through 90
```

此工具只读取指定件数及之前的观测和投入。第 90 件检查点会因低收益剩余库存而停手；第 54 件在停手边界附近返回不确定。不能据此声称新策略必然获得原流程第 90 件的结果，未开出的词条不能补造为反事实轨迹。

运行产物保留在本机 `artifacts/`，该目录由 Git 忽略；克隆仓库不会自带实测原始数据。新任务按第 1.1 节从模板和本轮读数生成输入即可，不需要这些旧数据。需要共享证据时提取最小、必要的样本到明确的文档或测试目录，不提交整套游戏截图。`report_xuanling_campaign.py`、`evaluate_xuanling_campaign.py` 仅用于历史复盘，仍依赖原始 campaign 数据，其中报告校验还引用历史检查文件路径；新轮次使用 `audit_echo_workflow.py`。
