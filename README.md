# PcrTimelineTester v1.0.1

按**期望伤害**择优，把 PCRCalculator 公会战轴里每条 UB 的**帧号**调到最优点。独立命令行程序，
全程只通过 PCRCalculator 的 HTTP `/simulate` 接口打战斗，不需要游戏客户端、
不读写游戏内存、不碰任何游戏内设置。

- **算法**：
  八阶段"先粗探、再外探、后逐帧补齐"的搜索，参数语义（步长 / 偏移 / 外探次数 / 允许掉幅 /
  峰值容差）逐项对应；场次上限默认关闭（0 = 不限制）。
- **程序形态**：`savePage` 轴格式 + 数组式 `/simulate` 请求，中文交互、每次运行新建一个目录；
  轴格式的读写代码是自带的，不依赖任何外部工具目录。
- **两个入口**：图形界面（Tkinter，冻结版就是它，见「图形界面」）与命令行（见「用法」）。
- **语义 GA 的算法来自** <https://github.com/Lanly109/PCR_CB_GA>。

下文把 `PCRCalculator` 的模拟器（`PCRCalculatorServer`）统一简称**摸轴器**。

## 环境与安装

```powershell
# Python 3.10+（带 tkinter 的官方安装包即可）
pip install -e .            # 装成命令 PcrTimelineTester
# 或者不安装，直接带源码跑：
$env:PYTHONPATH = "src"; python -m PcrTimelineTester inspect 轴.xlsx
```

依赖只有 `httpx`（HTTP）与 `openpyxl`（读写 .xlsx）。

## 目录结构

```
src/PcrTimelineTester/      程序本体
  cli.py                 命令行（inspect / probe / tune / resume / modes / gui）
  gui.py                 图形界面（Tkinter，无第三方依赖）
  modes.py               优化模式接口：ModeSpec / Param / register
  job.py                 逐帧搜索的调度、两道闸、单次写回、循环轮次
  search.py              八阶段峰值 + 最早帧搜索
  anneal.py              模拟退火：温度/接受准则/四类邻域/重启，复用 GA 的语义基因
roster.py              阵容搜索：把候选池里所有 5 人队伍排完（支持分组配额），每队试 32 种 AUTO/SET
datas.py               角色资料：读 Datas 里的角色名，按需下载并缓存角色图标
  frames.py savefile.py  帧号换算与 savePage 轴读写
  client.py server.py    接口客户端与摸轴器自启（前台窗口 + Job 对象）
  session.py report.py   一次运行的目录、报告、结果表
  settings.py            本地设置（%LOCALAPPDATA%\PcrTimelineTester\settings.json）
  ga/                    语义 GA：engine / semantics / fingerprint / evaluator / settings / job
tests/                   124 个 pytest 用例
tools/                   run_tests.py（离线跑测器）、build_frozen.py（冻结版）、launch.py
.github/workflows/       build-frozen：测试 → PyInstaller → 打 tag 自动发布
AGENTS.md                接手须知与硬性规则
```

## 启动摸轴器

```powershell
PCRCalculatorServer.exe --server --host 127.0.0.1 --port 8080
```

默认地址即 `http://127.0.0.1:8080/simulate`。图形界面不用手动启动：填好 PCRCalculator 目录
与端口，勾上「开始调帧时自动启动摸轴器」，点开始就会拉起 `server\PCRCalculatorServer.exe`。

- **前台窗口**：摸轴器在自己的控制台窗口里跑（不是隐藏进程），你能看见它，也能直接关掉它。
- **不会残留**：本程序退出（含被强关）时用 Windows Job 对象把摸轴器一起带走；
  你在运行中点「停止」只停任务、摸轴器留着方便再跑；点窗口的关闭按钮则会连摸轴器一起停。
- **不抢别人的**：端口上已经有摸轴器在跑就直接复用，绝不关掉不是自己启动的进程。
- 摸轴器自己的输出写进 `%LOCALAPPDATA%\PcrTimelineTester\server.log`（窗口里只有它自己的标题）。

接口必须是**同步返回数组**的那一版（`PCRCalculatorServer`）。

## 用法

```powershell
PcrTimelineTester                    # 交互向导：选轴 → 填参数 → 确认开跑
PcrTimelineTester inspect 轴.xlsx    # 只看：角色、Boss、战斗秒数、种子、每条 UB 的帧、可调项
PcrTimelineTester probe   轴.xlsx    # 用当前轴打一场，看看伤害和结果（确认接口通了）
PcrTimelineTester tune    轴.xlsx    # 调帧，默认跑全部可调项
PcrTimelineTester resume runs/20261115-155300_轴   # 接着上次中断的跑
```

`tune` 常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--endpoint` | `http://127.0.0.1:8080/simulate` | 接口地址（图形界面里是「目录 + 端口」，不需要填 URL） |
| `--battle-time` | 轴名里的 `尾刀 Ns`，否则 90 | 战斗秒数 |
| `--seed` | 轴里的 `RandomSeed`，否则 666 | 固定战斗种子 |
| `--samples` | 1 | 每个候选帧打几个种子取平均 |
| `--items 1:2,3:1` | 全部 | 只调指定 `(第几个角色:第几条UB)`；`--max-items N` 限条数 |
| `--include-boss` | 关 | 是否把第 6 行（Boss 自动 UB）也当作可调项 |
| `--step` | 5 | 搜索步长（帧） |
| `--max-offset` | 100 | 以当前帧为中心的搜索半径 |
| `--outer-probes` | 3 | 越过阈值后的外探次数（可调，不写死） |
| `--threshold` / `--threshold-mode` | `500000` / `absolute` | 允许掉幅：`absolute`（绝对伤害）/ `percent`（百分比）/ `strict`（不允许掉伤害），也可直接写中文 |
| `--peak-tolerance` | 0.5 | 判"同样好"的容差，同值时取最早帧 |
| `--passes` | 1 | 循环轮数：整套流程重复跑几轮，每轮都用上一轮的最优帧当起点（见「循环模式」） |
| `--max-battles` | 0 | 单条 UB 的场次上限，0 = 不限制 |
| `--chunk` | 8 | 一次 `/simulate` 数组里塞几个候选帧（只影响速度，不影响结果） |
| `--batch-size` | 16 | 单次 HTTP 请求最多几场战斗 |
| `--no-verify` | — | 跑完不再复核最后一战 |
| `--config` | — | 读 JSON 配置（`{"settings": {...}, "search": {...}}`） |

**退火的两个开关**：`--set restarts=0` 表示重启不限（连续 3 轮都没刷新最优才停）；
`--set moves=2` 表示一步里连着改 2 处基因（默认 1，只改一处时容易卡在局部最优）。
温度按「观察到的伤害跳变幅度」缩放，所以换轴不用重新调温度。

## 图形界面

```powershell
run.bat gui                       # 或：PcrTimelineTester gui
PcrTimelineTester gui 轴.xlsx       # 直接打开这条轴
PcrTimelineTester-gui               # 装过 `pip install -e .` 之后的独立入口
```

界面自上而下：

1. **轴文件** —— 打开 PCRCalculator 导出的 `.xlsx`，下面一行显示轴名、角色、Boss、从轴名推测的战斗秒数。
2. **摸轴器** —— PCRCalculator 目录 + 端口（不用填 URL）、优化模式、开始调帧时自动启动摸轴器（摸轴器在自己的窗口里跑）。
3. **参数标签页** —— 三种模式的参数分开放在四个标签上（通用 / 逐帧搜索 / 语义 GA / 退火），切「优化模式」会自动翻到对应页：
   - **通用**：战斗秒数 / 种子、采样种子数 / 单次场次、超时 / 跑完复核（所有模式共用）。
   - **逐帧搜索**：步长 / 偏移 / 外探 / 峰值容差、阈值 + 阈值口径 / 每批帧数、含 Boss 行、循环轮数、`CalcStats`。
   - **语义 GA**：种群 / 代数、岛屿 / 交叉率、精英保留 / 重启轮数、突变率 / 迁移间隔、突变衰减 / 突变下限、
     停滞停止 / 灾变间隔、单行上限 / 指纹容差、UB 类型 / 技能类型 / 序列类型、
     三种候选开关（旧版 UB / 本角色技能 / 其他角色技能）。
4. **可调项** —— 一行一项（只显示角色名 + 第几刀，不再带槽位号），点一下切换勾选；支持全选 / 全不选 / 反选；
   勾上「只调选中的项」后就只跑勾选的。语义 GA / 退火模式下这一栏只作参考（整条轴一起优化）。
5. **操作条** —— 开始调帧 / 开始优化 / 开始退火、停止 / 续跑… / 打开运行目录 / 打开报告，右侧是进度条与计数
   （逐帧：写回 / 跳过 / 失败 / 场次；语义 GA：第几代 / 模拟场次 / 当前最优；退火：第几次迭代 / 模拟场次 / 当前最优）。
6. **日志 / 结果** —— 日志页实时跟进（同时也写进 `runs/…/log.txt`），结果页逐帧模式按项列出轮次、原帧 → 新帧、决策、伤害变化、场次，
   语义 GA 模式按代列出最优伤害与「本代刷新 / 最优仍来自第 N 代」，退火模式按迭代列出温度与最优伤害。

每个可填/可改的选项都有悬浮说明（鼠标停 0.4 秒弹出）。界面里的设置（摸轴器目录、端口、
自启开关、上次打开的轴、所有参数、勾选项、窗口大小位置）都保存在
`%LOCALAPPDATA%\PcrTimelineTester\settings.json`，下次打开自动恢复。

规矩和命令行完全一致：**搜索期间只改内存，最后一次性写回**；点「停止」= 收尾且不写回任何帧
（已完成的部分留在 `state.json`，回到窗口点「续跑…」选那个运行目录即可接着跑）。
窗口和命令行共用同一层 `session`，所以产物、两道闸、退出码都相同。

### 阵容搜索（`--mode roster`）

阵容搜索**不用迭代、也不用温度**：它把候选池里所有可能的 5 人队伍**全部排一遍**，每支队伍再试
`2^5 = 32` 种「每个人 AUTO / SET」的组合（`SetToggles`，见 API 文档 2.2；`IsSetMode=false`），
取伤害最高的那种作为这支队伍的分，并记下每个人到底是 AUTO 还是 SET。

速度：每支队伍 32 场按 `单次场次`（默认 32）拆请求，**同时发 4 个请求**（`roster.POST_WORKERS`）。
本机 24 核实测 2002 支队伍 = 64,064 场，用时 47 秒（≈1,400 场/秒）；1 万支队伍≈4 分钟。
界面进度条按「排完几支队伍」走，计数器显示「队伍 x/y / 场次 n / 最优 d」，最优阵容一变就往结果页补一行。

```bat
:: 候选池 8 个人，分两组（A 组 3 人里必须上 2 个、B 组 1 个人必须上 1 个），剩下 2 个名额从别人里抽
run.bat tune 轴.xlsx --mode roster ^
  --set pool=134901,136201,128701,129701,136001,100101,100201,100301 ^
  --set "groups=A:2=134901,136201,128701;B:1=129701" ^
  --set every_combo=true
```

- **分组和候选池是两回事**：候选池是「能上场的所有人」，分组只是给这些人加一层「最少上几个」的约束。
  只设了最少人数的组才算数（`minimum=0` 的组等于没设）；组里挑人时只看「这个组里也在候选池里的人」。
- **允许组间重叠**：同一个角色可以同时出现在两个分组里，搜索会照常枚举——只要一支队伍里没有重复角色。
  没进任何分组（或所在分组没设最少人数）的人合到「未分组」，用来补足剩下的人头。
- 队伍固定 5 人，不设迭代次数、不设温度：`队伍数 = 枚举出来的合法队伍总数`，一支不落地都会打到。
- 轴里原来那 5 个人都在池子里时，会先打一场当**参考伤害**，报告里跟最优阵容对比。
- 日志里会写 `共 N 支队伍 × 32 种 AUTO/SET × S 个种子 = B 场`，并且**每支队伍都打一行**
  `[i/N] 角色名 → 伤害（OXXOX）`——`O` = SET、`X` = AUTO；跑完写「最优阵容」和「每位角色的操作」。
- **前几批慢是正常的**：摸轴器冷启动要 3～4 秒才开端口，第一条 `/simulate` 还要把战斗数据读进来并首次
  初始化（≈100 ms/场），之后 .NET 把热点方法编译成机器码（Tier0→Tier1），单场会掉到 ≈3 ms——实测
  同一个请求从 3.2 秒一路掉到 0.09 秒（快 36 倍）。所以日志里每批都带耗时：
  `第 1 批 8 支队伍 / 8 场用了 2.16 秒（累计 4.2 秒）`、`第 2 批 … 0.67 秒`，几批之后就只剩几十毫秒。
  另外单场成本跟战斗长度成正比（90 秒满场 5400 帧比 60 秒就全灭贵得多），枚举开头往往是打满的队伍。
- `every_combo=false` 时只打「全 SET（`OOOOO`）」一种；SET 与帧轴无关（见下一节），没有帧数据的轴照样能试 32 种。
- **不吃轴里的帧**：轴里的 `UBExecTimeList` 是「帧模式」的手动刀表，不代表 AUTO / SET，阵容搜索一律不带它
  （请求里 `UBExecTimeList=[]`）。谁自己按 UB 完全由 `SetToggles` + `IsAutoMode` 决定。
- **O 和 X 的意思**：`O`（SET）= `SetToggles[i]=true` → `pressing`，一有机会就按；
  `X`（AUTO）= 靠 `IsAutoMode=true` 让角色自己找时机。所以只要有一个 X 就会打开 `IsAutoMode`。
- **结果只留最优的 100 支队伍**：`阵容结果.xlsx`、`报告.md`、界面「查看结果…」都只列这 100 条；
  想细看某一队，在窗口里选一行点「试这队 32 种组合」，会把这队 32 种组合的伤害/战斗帧/胜负列出来
  （这个按钮固定跑满 32 种，不看「每队试 32 种」那个勾选；32 条请求一次性并发发出去，几秒就回来）。
- 分组的成员用「选择成员…」挑，和「选择角色…」是**同一个窗口**（按职能 / 属性 / 名字过滤、带图标、
  可以从全部角色里选，不必先加进候选池；选了池外的角色会自动补进候选池）。
- 分组可以存成预设（界面「保存预设 / 载入预设」，写在 `%LOCALAPPDATA%\PcrTimelineTester\roster_presets.json`）。
- 分组跟着设置一起存（`settings.json`），关掉再打开会直接显示出来，不用先新建一次才看得见。
- 子窗口（分组、选人、结果）都摆到主窗口正中间；选人窗口里鼠标停在任意位置都能滚轮翻页，不要求停在滚动条上。
- 角色名、简称来自摸轴器安装目录的 `PCRGuildCalculator_Data\StreamingAssets\Datas`；
  **只显示既有职能又有属性的角色**。职能 / 属性 / 站位宽度统一读
  `http://192.144.214.135/pcr/unit_info.php`（`role` = 职能、`talent` = 属性、`search_area_width` = 攻击范围），
  第一次打开角色表时抓一次，缓存在 `%LOCALAPPDATA%\PcrTimelineTester\unit_info.json`（之后不再联网；
  万一 `Datas` 里已经有角色在这份缓存里查不到，打开角色表时会自动重抓一次，一次会话只抓一次）：

  | role | 职能 | talent | 属性 |
  | --- | --- | --- | --- |
  | 1 | 攻击型 | 1 | 火 |
  | 2 | 破防型 | 2 | 水 |
  | 3 | 增益型 | 3 | 风 |
  | 4 | 减益型 | 4 | 光 |
  | 5 | 强化型 | 5 | 暗 |
  | 6 | 治疗型 | | |
  | 7 | 坦克型 | | |
  | 8 | 干扰型 | | |

- 「选择角色 / 选择成员」窗口也按站位排（攻击范围小的在前），找前排角色不用翻列表。
- **界面上按游戏真实站位写**：每个角色在队伍里有固定站位（1 号位在最前），
  站位顺序 = 攻击范围 `searchAreaWidth` 从小到大（相同再按角色 ID），这也正是游戏回放战斗日志时的排法
  （`ExcelHelper.cs`：`OrderBy(searchAreaWidth).ThenBy(unitId)`）。宽度就在上面那张 `unit_info.php` 里
  （缓存的 `unit_info.json`，354 条），所以状态栏、结果窗口、`报告.md`、
  `阵容结果.xlsx` 都写成「1 花音 AUTO（X）、2 纳娜 SET（O）……」这种带站位号的形式，
  紧凑的 `OX` 串也按站位排（不再按我们枚举队伍时的先后）。
  **请求也按站位发**：`SetToggles[i]` 在服务器里是按「站位槽位」生效的（内部按 `searchAreaWidth`
  排序后 `playerUnitCtrl[i].pressing = allSet || setToggles[i]`），跟 `PlayerList` 的先后无关。
  实测同一组 toggles 只换队伍顺序，伤害只跟「第几个位置是 true」有关（谁排第 1 位谁 SET 结果一样），
  所以 `request_for()` 会把 `PlayerList` 与 `SetToggles` 一起按站位重排——界面上的「1 花音 SET」
  才真的是花音的刀。队伍枚举顺序依旧不影响结果。
- 中断不导出（`state.json` 记了已经排到第几支队伍，`resume` 会从那里继续）。

### 每支队伍都试 32 种 AUTO / SET

`SetToggles` 里每个人一个开关：`O`（SET）= 一有机会就按（服务器把它变成 `pressing=true`，与 `UBExecTimeList` 无关）；
`X`（AUTO）= 打开 `IsAutoMode`，由摸轴器自己找时机。**轴里的帧不参与**——它是帧模式的手动刀表。

同一支队、同一个种子、都按满属性算（8099 实测）：

| 打法 | 请求 | 伤害 |
| --- | --- | --- |
| 全 `O`（SET） | `SetToggles=[T]*5`，不带帧 | 100,978,474（3802 帧，LOSE） |
| 全 `X`（AUTO） | `IsAutoMode=true`，`SetToggles=[F]*5`，不带帧 | 149,119,769 |
| 既不开 AUTO 也不 SET | 两个都关，不带帧 | 40,505,442（根本不放 UB，所以 AUTO 必须开 `IsAutoMode`） |
| 轴里的帧 | `UBExecTimeList` = 轴里的刀表 | 363,472,384（这是帧模式的结果，阵容搜索不用） |
5 个人就是 `2^5 = 32` 种组合，伤害差得很远（实测同一支队：`AAAAA` 3.62 亿、
`OOOOO` 1.01 亿、`XXXXO` 2.21 亿）。一次 32 连打只要 3 秒左右。

### 图标

图标从 `https://wthee.xyz/redive/jp/resource/icon/unit/` 下载一次后缓存在
`%LOCALAPPDATA%\PcrTimelineTester\icons`。

下载规则：只下 `unitId < 190000` 的角色，文件名用摸轴器 Datas 里的编码
（`unitId // 100` + `11 / 31 / 61`，例如 `115001 → 115011.webp`）。
**默认优先用最大那套立绘**：有 `61` 就显示 `61`，没有就退 `31`，再没有才 `11`。
已经在缓存里的不再下载；界面上的「下载选中角色的图标」在后台用 8 个线程跑，
进度条实时更新，窗口不会卡；「导入图标包…」可以直接把发布包里的
`PcrTimelineTester-icons.zip` 解压进缓存目录。

## 输入与输出

**输入**：PCRCalculator 导出的轴 `.xlsx`——`savePage!A1` 是轴名、`A2` 是
`gzip+base64` 的存档 JSON；同一本工作簿里的 `基础数据` 页提供**中文角色名**（A 列角色ID、
B 列名字，`BOSS详情` 行是 Boss），程序从那里查名字，查不到才退回 `角色1…角色5`。帧轴在 `playerGroupData.UBExecTimeData`（5 条角色行，可多 1 条 Boss 行），
同帧多 UB 用 `480.001` 这种小数尾巴表示顺序。必须是**帧轴**；只有语义 UB 数据的轴会被拒绝，
请先在摸轴器里导出帧轴。

**输出**：每次跑新建一个目录 `runs/<yyyymmdd-HHMMSS>_<轴名>/`

| 文件 | 内容 |
| --- | --- |
| `调帧结果.xlsx` | 可直接导回 PCRCalculator 的轴（隐藏 `savePage` + 可见「调帧结果」清单 + 原样带过去的 `基础数据`） |
| `report.json` / `报告.md` | 结论、逐项原帧/新帧、伤害变化、失败与中断原因 |
| `log.txt` | 完整日志（同时打到控制台） |
| `config.json` / `state.json` | 本次参数 / 断点状态（`resume` 用） |

源轴文件不会被修改。

## 搜索算法

1. **① 基线**：当前轴打一场，其余各项都以它为参照。
2. **②③ 向上**：从峰值按步长往上扫；一旦某帧伤害掉出阈值，就**外探** `--outer-probes` 次，
   若外探发现更高值，换峰值回到 ②。
3. **④⑤ 向下**：镜像处理（下探的窗口受 `baseline - max-offset` 限制）。
4. **⑥⑦ 补齐**：以峰值为中心逐帧（步长 1）向左、向右补齐，最多
   `max-offset + (outer-probes+2) × 步长` 帧，遇到明显更低的帧就收手。
5. **⑧ 择优**：全场最高伤害；伤害相同（容差内）取**最早**的帧。

阈值区间（`threshold_start ~ threshold_end`）是"掉幅仍在允许范围内"的帧范围，报告里会写出来。

## 循环模式（`--passes` / 界面「循环轮数」）

整套流程可以重复跑多轮：**每一轮都拿上一轮写回后的最优帧当起点再搜一遍**，
所以第二轮起是在已经调好的轴上继续找提升，而不是从头再来。

- 第 1 轮与单次运行完全一致（含闸二：相同且帧未变即跳过）。
- 第 2 轮起多一条"本轮无提升就不动帧"的规矩：伤害没涨就保持当前帧，
  否则平坦地形会一轮轮把帧往更早的位置蹭（伤害一样但轴被改得面目全非）。
- 中断（Ctrl+C / 停止）仍然一条都不写回；轮次进度记在 `state.json`，`resume` 会从断的那一轮接着跑。
- 报告里的逐项表带「轮」列，结论里写明「N 项 × M 轮」。

## 优化模式（`--mode` / 界面「优化模式」）

同一份轴可以用三种模式优化，默认是逐帧搜索：

| 模式 | key | 做什么 | 结果表名 |
| --- | --- | --- | --- |
| 逐帧搜索 | `frame` | 一次挑一个 UB，在窗口内搜峰值帧 | `调帧结果.xlsx` |
| 语义 GA | `ga` | 把五条语义基因整条轴一起做遗传算法 | `优化结果.xlsx` |
| 退火 | `anneal` | 同样的语义基因，用模拟退火一步步试探 | `退火结果.xlsx` |
| 阵容搜索 | `roster` | 排完候选池里所有 5 人队伍（分组配额），每队试 32 种 AUTO/SET 取最高 | `阵容结果.xlsx` |

```powershell
run.bat tune 轴.xlsx                      # 逐帧搜索
run.bat tune 轴.xlsx --mode ga            # 语义 GA（默认 48×30，岛屿 3）
run.bat tune 轴.xlsx --mode anneal        # 模拟退火（默认 400 次迭代）
run.bat modes                             # 列出所有模式的参数（--set 能用的名字）
run.bat inspect 轴.xlsx --mode ga         # 看这条轴在 GA 模式下会从哪儿起步
```

- **算法来源**：语义 GA 的思路来自 <https://github.com/Lanly109/PCR_CB_GA>，本工具是独立实现（同步 HTTP、不依赖它的运行时）。
- **语义 GA 的起点**：轴里已有语义数据就用它；只有帧轴时先把帧轴转换成语义轴。
  新摸轴器会给 `Stats.UbConversion`；老摸轴器（例如 2.11.0 那份）没有这个字段，
  程序会退回「自动语义老师」——用 `CalcStats=128` 让摸轴器自己排出五条基因
  （`Stats.AutoSemanUBExecTimeList`）当起点，并在日志里提示。
- GA 的每条基因 = 一段「语义 UB 条件 + 间隔帧」的序列，由五种突变、分岛迁移、
  灾变按代推进；同一代里重复的基因只跑一场，行为重复用 `ActionProgress` 指纹剔除。
- **GA 的几个算子**（都能用 `--set 名字=值` 改，界面上在「语义 GA」页）：

  | `--set` 名字 | 界面 | 默认 | 作用 |
  | --- | --- | --- | --- |
  | `elite_count` | 精英保留 | 2 | 每代先把最好的 N 个原样带进下一代（0 = 关掉） |
  | `mutation_decay` | 突变衰减 | 1.0 | 第 g 代突变率 = `mutation × decay^g`，越往后越稳 |
  | `mutation_floor` | 突变下限 | 0.0 | 衰减后的突变率不再低于这个值 |
  | `stagnation_limit` | 停滞停止 | 0 | 连续 N 代没刷新最优就收工（0 = 不早停） |
  | `restarts` | 重启轮数 | 1 | 整条流程重跑几轮，第 2 轮起换随机种子，取各轮最优 |

  ```powershell
  run.bat tune 轴.xlsx --mode ga --population 64 --generations 60 `
      --set elite_count=4 --set mutation_decay=0.9 --set stagnation_limit=20 --set restarts=3
  ```
- 只有帧轴/只有语义轴的轴都能读：逐帧搜索需要帧轴，语义 GA 两者皆可。
- **序列类型要跟轴一致**：轴里没有角色序列（`SemanUBSequenceData` 为空）时，
  「同时调角色序列」会让摸轴器按角色序列解释基因、整条 UB 排程都变，伤害会莫名其妙掉下去；
  本程序读到这种轴会自动退回「只调五条 UB 序列」，并在日志里说明。
- **口径自检**：开工前用轴里自带的语义基因反推「哪个 UB 类型 / 技能类型 / 序列类型
  能把这条轴原样还原成帧轴」，对不上就自动改过来并写进日志与报告；
  还原不出来时还会拿帧轴本身打一场当参考，两者伤害不一致就 `[warn]` 提醒（数字不可信）。
- 中断（Ctrl+C / 停止）不导出任何结果轴，`state.json` 记着跑到第几代，`resume` 接着跑。
- 报告里有「重启轮次」表（多轮时）、逐代最优表、帧轴参考伤害；
  语义起点和帧轴伤害对不上时报告里会单独标出来，`exceptDamage` 口径见「接口细节」。

### 退火（`--mode anneal`）

模拟退火：每一步随机改动一两个 UB 的位置，
伤害变好就接受、变差则按 `exp(Δ/温度)` 的概率接受，温度按几何级数降下来。
本工具把同一套数学做在语义基因上：

- 温度：`T = 初始温度 × |当前最优伤害|`，按 `(最低温度/初始温度)^进度` 几何降温；
  日志里每 `报告间隔` 步打一行 `[i/total] T=… 当前 … 最优 … 未接受 n`。
- 四类邻域（概率可在界面「退火」页或 `--set` 里调）：
  `swap_chance` 单点替换、`double_swap_chance` 双点互换、
  `chain_chance` 链式替换一段、`manual_chance` 先随机挑角色再挑一刀。
- 与 GA 共用语义口径、基因池、口径自检与帧轴交叉核对；起点默认用轴里的语义数据，
  对不上时同样退回「自动语义老师」。
- 默认 400 次迭代、单轮；`--set restarts=3` 可多轮重启取最优，
  `--set allow_duplicates=true` 关掉缓存（每一步都真打一场）。

```powershell
run.bat tune 轴.xlsx --mode anneal --set iterations=800 --set restarts=3
run.bat tune 轴.xlsx --mode anneal --set initial_temperature=0.1 --set chain_range=8
```

结果写 `退火结果.xlsx`（同时含帧轴与语义轴），报告里多一张「迭代轨迹」表。

### 跑完接着跑

界面里一次优化正常结束（没中断）后，会自动把刚生成的 `调帧结果.xlsx` / `优化结果.xlsx` / `退火结果.xlsx`
载进「轴文件」，可调项、建议秒数、种子都换成这次的结果，直接再点一次「开始」就是
在上一轮的结果上继续调。运行目录仍然开在**最初那条轴**旁边的 `runs\` 里，
不会出现 runs 套 runs。

### 接入别的优化模式（`modes.py`）

模式是一层接口，不是写死的 if：

```python
from PcrTimelineTester import modes

modes.register(modes.ModeSpec(
    key="my-mode",
    label="我的模式",
    description="一句话说明",
    needs_frames=False,                      # 读轴时是否强制要有帧数据
    workbook_name="我的结果.xlsx",
    params=(modes.Param("rounds", "轮数", "悬浮说明", "int", 3, 1, 99),),
    build=lambda values, save: MySettings(rounds=values["rounds"]),
    session=lambda save, settings, run_dir, **kw: MySession(...),
    plan=lambda save, settings: [f"轮数 {settings.rounds}"],
))
```

注册后 `--mode my-mode`、界面下拉、`--set rounds=5`、`modes` 子命令和报告都会自动带上它；
`ModeSpec.from_values()` 会把命令行/输入框的字符串按 `Param` 的类型和范围校验
（`int` / `float` / `bool` / `choice`），未知参数名与非法值都直接报中文错误。

## 两条硬规矩

- **闸一 · 退步保护**：本项搜出的最高伤害 < 上一项写回后的最优，就不采纳，记失败、保持原帧。
- **闸二 · 无需改动**：本项最优 == 当前值且选中的帧就是原帧，记跳过。
- **只在最后写一次**：搜索期间只在内存里累积，跑完一次性写盘。中途 Ctrl+C（或搜索失败）
  不会留下半调好的轴——再次按 Ctrl+C 才强制退出。

任何一次采样失败都会让**整个任务报错中止**，绝不把失败当成 0 伤害继续跑。

## 接口细节

- 请求体是**数组**，一次最多 `batch_size` 场；回包按请求顺序一一对应。
- `UBExecTimeList` 送出 5（或 6）行帧；`SemanUBExecTimeList` 送空数组、
  `IsSemanMode=false`——纯帧轴模式。
- `Settings.UseFixedRandomSeed=true` + `RandomSeed=<种子>`，并校验回包 `CurrentSeed`
  与请求一致，不一致直接报错。
- 伤害取**期望伤害**：请求带 `NeedOnceResult=true`，读回包 `OnceResult.exceptDamage`；
  拿不到时退回 `Stats.TotalDamage`（实战伤害），走哪条路都会写进日志。
  当前 .NET 摸轴器用 `DONT_USE_EX` 编译，`exceptDamage` 与 `currentDamage` 相同，
  所以期望伤害一般等于实战伤害；等摸轴器恢复期望伤害统计，这里自然就是真期望值。
- 若回包没有 `Stats`（`CalcStats` 太小时会这样），自动改用 `CalcStats=511` 重试一次。

## 退出码

`0` 成功 / `1` 出错（参数、Excel、接口、战斗失败）/ `130` 被中断（已收尾，未写回任何帧）。

## 已知限制

- 逐帧搜索只处理帧轴；语义 UB 的基因优化走「语义 GA」模式（同一个程序，`--mode ga`）。
- 语义 GA 在只有帧轴时需要摸轴器支持转换：新摸轴器给 `Stats.UbConversion`，
  老摸轴器退回自动语义老师；两者都没有的摸轴器只能先用摸轴器把轴切成语义模式再导出。
- 一次只读一条轴；轴里必须正好 5 个角色、非空 Boss。
- **执行顺序按帧**：可调项、界面列表、命令行清单、实际调帧顺序一律按「这一刀自己的帧号」
  从小到大排（同帧再按角色顺序），不再按角色分组；所以越早的 UB 越先被调，
  它写回后对后面那些刀的伤害影响是累积的。
- **语义起点自检**：语义 GA 先拿轴里自带的 `SemanUBExecTimeData` 反推帧轴，对得上才用它当起点；
  对不上（这条轴的语义数据和帧轴不是同一次状态）就改成用帧轴 + 自动语义老师重新推起点，
  日志里会写「起点改成用帧轴重新推出」，这样基线伤害和逐帧搜索的数字一致。
- 语义 GA 的「同时调角色序列」只在轴自带 `SemanUBSequenceData` 时才有意义（见上节）。
- 轴里的 `SemanUBExecTimeData` **超过 5 行时只保留前 5 行**，多出来的行直接丢掉，并在日志里
  打一行提示——摸轴器只按位号 0~4 取车道（`SemanUbManager.IsUbExec(pos)` 里
  `pos > 4 || pos >= queues.Length` 直接返回 false），第 6 行以后永远不会被执行。
  **少于 5 行则整条语义轴当成没有（空）**，同样在日志里说一句，此时 `SemanUBSequenceData`
  也一起忽略——凑不满五条车道的语义数据本来就用不了。
- 队伍的**行顺序**由摸轴器按 `searchAreaWidth` 重排，所以本程序不做重排、也不改顺序；
  若你的轴行顺序与摸轴器内部顺序不同，请先在摸轴器里确认。
- `--samples > 1` 会按 `abs(seed)+i` 派生种子取平均伤害。

## 冻结版（单个 exe，不需要装 Python）

```powershell
python -m pip install pyinstaller          # 只有构建机需要
python tools\build_frozen.py               # → dist\PcrTimelineTester.exe（单文件、无控制台窗口）
```

- 冻结版只打包图形界面；命令行入口仍然用 `pip install -e .` 装。
- 产物名可用环境变量 `PFT_FROZEN_NAME` 覆盖。
- **图标**：项目根的 `icon.ico` 会被嵌进 exe（任务栏、资源管理器、窗口标题栏都用它）；
  构建时用 Pillow 扩成 16/24/32/48/64/128/256 共 7 种尺寸，小图标不会糊。
  所以**构建机要装 Pillow**（`python -m pip install pillow`，CI 里已经带上）；没有它也能构建，
  只是 exe 里只有原始尺寸的图标、界面里的角色缩略图不显示。
  源码运行时（`python tools\launch.py`）窗口图标也取自同一个文件；没有这个文件就用 PyInstaller 默认图标。
- 只出单文件版：`dist\PcrTimelineTester.exe`。它启动时会把内容解压到系统临时目录，
  所以 `%TEMP%` 必须可写（受限环境下会报 `Could not create temporary directory!`）。
- 发布包是两个文件：`PcrTimelineTester.exe` 和 `PcrTimelineTester-icons.zip`（图标包，
  在界面「阵容搜索」页点「导入图标包…」导入）。
- 冻结版出错时不会再"闪退没反应"：异常会写进 `%LOCALAPPDATA%\PcrTimelineTester\crash.log`
  并弹窗告诉你路径。要自己看 traceback，可构建带控制台的诊断版：

  ```powershell
  python tools\build_frozen.py --console --name PcrTimelineTester-console
  ```

### 发布到 Release

推一个 `vX.Y.Z` 标签，`build-frozen` 就会自动跑测试、构建冻结版，并把 `dist/` 里的两个文件挂到
Release 上（`generate_release_notes` 会自动写变更列表）：

```powershell
git tag -a v1.0.1 -m "PcrTimelineTester v1.0.1"
git push origin v1.0.1
```

- **只想手动发一次**（不打标签）：Actions → `build-frozen` → `Run workflow`，这只产出 artifact、
  不会建 Release；下载 `PcrTimelineTester-windows-x64` 后，在 GitHub「Releases → Draft a new release」
  里手动挂上 `PcrTimelineTester.exe` 与 `PcrTimelineTester-icons.zip`。
- Release 说明里请写明：**图标包含游戏素材，版权归 Cygames，仅供个人研究使用**（见「许可」一节）。
- 版本号要三处一起改：`pyproject.toml`、`src/PcrTimelineTester/__init__.py`、README 标题。

- 推 `v*` 标签会触发 `.github/workflows/build-frozen.yml`：先在 `windows-latest` 上
  `pytest -q`，再构建冻结版，上传 artifact 并附到 Release。

## 测试

```powershell
pip install pytest
python -m pytest -q    # tests/ 下 147 个用例，接口层用 httpx.MockTransport，不需要真服务器

# 装不上 pytest 时（比如没有外网）用自带的离线跑测器，跑的是同一批用例：
python tools\run_tests.py
python tools\run_tests.py test_ga    # 只跑文件名含 test_ga 的
```

用例覆盖：帧换算与可调项枚举、搜索八个阶段的落点与场次、帧轴的读写往返、
`savePage` 编码、请求体契约（字段不多不少）、种子校验、`CalcStats` 511 回退、
两道闸、中断不写回、循环多轮、摸轴器自启/复用、设置持久化、`session` 产出的整套运行目录、续跑，
以及语义 GA 的基因/指纹/引擎不变量、GA 请求契约与多种子、`GaJob` 的起点与导出、
模式接口（注册/查表/参数强转/未知模式报错/`needs_frames` 分流）、
阵容搜索（换人/跨队互换、每队 32 种 AUTO/SET 取最高、结果与报告带操作列）、
图标缓存与图标包（编码规则、跳过已缓存、解压只收 .webp）。
图形界面不在用例里（Tk 需要窗口）；它的逻辑都在 `session` 层，界面只负责搬运参数与显示。

## 许可

- **代码**：MIT，见 [`LICENSE.txt`](LICENSE.txt)。`pyproject.toml` 里的 `license = "MIT"` 与
  `license-files` 指向同一个文件。
- **依赖**：`httpx`（BSD-3-Clause）、`openpyxl`（MIT）等；冻结版会把它们一起打包，
  各自的许可声明随本文件一并生效。打包工具 PyInstaller 是 GPL-2.0 **带打包例外**，
  允许把本项目打进单个 exe 再按 MIT 分发。
- **游戏素材不属于本项目**：游戏名称、角色名、角色立绘（图标包里的 `.webp`），以及从
  `unit_info.php` / 摸轴器 `StreamingAssets/Datas` 整理出来的角色资料（职能、属性、站位宽度），
  版权归 Cygames
  （《プリンセスコネクト！Re:Dive》）。MIT 只覆盖本项目的代码，**不包含**对上述素材的任何授权；
  图标包与 `Datas` 只供个人本机研究使用，请勿另行分发或商用。
- 本项目与 Cygames、与摸轴器（PCRCalculator）的作者都没有关联。
