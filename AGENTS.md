# PcrTimelineTester 维护指南

用户入口、模式参数和结果文件见 [README.md](README.md)。源码位于 `src/PcrTimelineTester/`。GUI（`gui.py`）与 CLI（`cli.py`）共用 `modes.py` 注册的六种模式。

## 模拟与存档

- 战斗模拟只通过 PCRCalculator 的 HTTP `POST /simulate`。禁止读写游戏内存、添加 hook 或读写 `TimingThreshold`。
- 请求开启 `NeedOnceResult`。优先取 `OnceResult.exceptDamage`，缺失时取 `Stats.TotalDamage`。多种子平均是近似期望。
- 搜索期间保留内存中的候选和可续跑的 `state.json`。正常结束才导出结果表，停止、Ctrl+C 或关窗须允许续跑并避免写回半成品。GUI 完成后自动载入结果表。
- `server.py` 复用已监听的算器，自己启动时使用 `CREATE_NEW_CONSOLE` 显示前台窗口。仅以 PID 和 Windows Job 对象管理自己的进程，禁止按名称结束算器。

## 搜索约束

| 模式与代码 | 维护时保留的行为 |
| --- | --- |
| 逐帧 · `job.py`、`search.py`、`effects.py` | `build_targets()` 按（帧号、角色行、刀序）排序，帧范围 90～5399。本项最优低于上一项最优时不采纳，同伤同帧跳过，第 2 轮起无提升就保留原帧。向下遇到实放帧与请求不符时最多再探 `min(UB_MISS_LIMIT, outer_probes)` 次，此限制仅用于向下搜索。效果帧联调仅采纳整场伤害提升的组合。 |
| 循环 · `cycle.py` | 以实际事件生成增删刀、充能联动和换段候选，按层保留分支与续跑状态。主结果按整场期望伤害选，多发分支另记。发布工作簿前，读回导出的帧轴复核伤害及首种子五人的实放 UB 次数。 |
| 语义 GA / 退火 · `ga/`、`anneal.py` | 语义车道固定五行，多余行截断，少于五行时忽略语义轴及消费顺序，两种情况均写中文日志。GA 先核对自带语义能否还原帧轴，对不上就从帧轴重新推起点。变异和交叉须保持消费顺序与各车道刀数相容。 |
| 阵容 · `roster.py` | 枚举全部合法五人队并去重，分组只设下限且可重叠。请求用空 `UBExecTimeList`，AUTO 开启 `IsAutoMode`，SET 使用 `SetToggles`。请求中的队伍、开关及显示都按 `lineup_order` 排序（站位宽度、unitId 升序）。保留 4 路并发，按完成队数显示进度，保留前 100 队。全 SET 筛选只测一种，详情页的「试这队 32 种组合」始终测满。 |
| 0押 · `zero.py` | 用 `CalcStats=1\|2\|256` 读取 `CrossedReady` 和 `UBTime`。第 k 刀选 O 就写第 k 个 TP 满帧，选 X 就交给 AUTO。开启 `IsAutoMode`，所有 `SetToggles` 为假。刀数随回包变化，复核原样重发 `ZeroScore.lanes`，结果表只给原轴添加摘要。 |

## 界面与角色资料

- 界面、日志、报告和错误使用中文。Tk 控件及变量只在主线程访问。通过 `_report_error()` / `_notice()` 显示提示，退出运行中的任务才弹确认框。
- 昼夜配色集中在 `Palette`。勾选框以对勾表示选中、空框表示未选，选项卡用下划线标识当前页，保留键盘切换、空格勾选和滚动条拖动。
- 设置、预设、图标、角色资料缓存及服务和崩溃日志放在 `%LOCALAPPDATA%\PcrTimelineTester\`。冻结版以 `--windowed` 运行，保留 `gui.main()` 初始化标准输出、错误流和崩溃日志的逻辑。
- 名称及简称取自摸轴器 Datas。职能、属性和站位宽度经 `datas.load_unit_info()` 从 `unit_info.php` 获取并缓存，`unit_width()` 只读缓存。发现 Datas 新角色时每次会话最多主动刷新一次。选人窗口按站位排列，只展示职能和属性齐全的角色。图标仅处理 `unitId < 190000`，后缀优先级为 61、31、11。

## 验证与发布

```powershell
python -m compileall -q src tests
python tools/run_tests.py
python tools/build_frozen.py
```

- 本地可用离线跑测器，CI 用 `python -m pytest -q`。真机验证用独立端口，保留用户的 8080 算器。用一条实际轴跑 `inspect` 和小规模搜索，核对报告、结果表及自动载入。
- 构建机使用 PyInstaller 和 Pillow。诊断冻结版时可加 `--console`。提交使用中文，排除 `build/`、`dist/`、`runs/` 和临时脚本。
- 发布版本号只需修改 `pyproject.toml` 的 `[project].version`。`vX.Y.Z` 标签触发测试、打包和发布。保留 README 的许可说明，发布图标包时注明「游戏素材版权归 Cygames，仅供个人研究使用」。
