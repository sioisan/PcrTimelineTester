# AGENTS.md —— 给接手这个项目的人 / 代理

## 这是什么

`PcrTimelineTester`是公主连结公会战调轴工具，独立运行，
只通过 HTTP 调 `PCRCalculator` 的 `POST /simulate` 接口做战斗模拟，不读写游戏内存。

两种入口：图形界面（`gui.py`，Tkinter，冻结版就是它）与命令行（`cli.py`）。
四种优化模式（`modes.py` 里的注册表）：

| 模式 | key | 做什么 | 产物 |
| --- | --- | --- | --- |
| 逐帧搜索 | `frame` | 对每一刀做「峰值 + 最早帧」搜索 | `调帧结果.xlsx` |
| 语义 GA | `ga` | 遗传算法优化五条 UB 语义序列（`ga/`；算法来源 <https://github.com/Lanly109/PCR_CB_GA>） | `优化结果.xlsx` |
| 退火 | `anneal` | 模拟退火在同一套语义基因上一步步试探（`anneal.py`） | `退火结果.xlsx` |
| 阵容搜索 | `roster` | 把候选池里所有 5 人队伍**全部排完**（支持分组配额），每队试 32 种 AUTO/SET 取最高（`roster.py`，不带轴里的帧，`O`=SET / `X`=AUTO） | `阵容结果.xlsx` |

## 目录

```
src/PcrTimelineTester/      # 代码
  cli.py gui.py session.py modes.py job.py search.py frames.py savefile.py
  client.py server.py report.py settings.py anneal.py roster.py datas.py
  ga/                     # 语义 GA：engine/semantics/fingerprint/evaluator/settings/job
tests/                    # pytest 用例（166 个）
tools/                    # 开发工具：run_tests.py（离线跑测器）、build_frozen.py、launch.py
.github/workflows/        # build-frozen：测试 + PyInstaller + 打 tag 自动发布
```

## 硬性规则（改代码前先读）

1. **中文界面 / 中文日志**：所有面向用户的文字、日志、报告、报错都用中文；代码注释中文可以。
2. **不写游戏内存**：本工具只发 HTTP；不允许加任何写进程内存 / hook 的逻辑。
3. **不写 TimingThreshold**：调轴只看模拟器返回的伤害/帧，不读也不写游戏设置里的阈值。
4. **单次写回**：搜索过程只存在内存里，正常结束才写结果表；中断（停止 / Ctrl+C / 关窗口）
   一律不写回，靠 `state.json` 续跑。改动 `job.py` / `session.py` 时别破坏这条。
5. **两道闸不能删**：闸一「本项最优 < 上一项最优 ⇒ 不采纳」，闸二「同伤害且帧没变 ⇒ 跳过」。
   第 2 轮起的额外规矩「本轮没提升就不动帧」也别删（否则平坦地形会把帧一轮轮往前蹭）。
6. **算器（`PCRCalculatorServer`）只能关自己起的那个**：`server.py` 用 pid + Windows Job 对象
   管自己的进程；`stop()` 里绝不能按名字杀进程——用户可能自己开着算器。
   自起进程要**前台窗口**（`CREATE_NEW_CONSOLE`），不能藏后台。
7. **执行顺序按帧**：`build_targets()` 必须按 `(帧号, 角色行, 第几刀)` 排序；界面、命令行、
   实际执行顺序都吃这一条，别改回按角色分组。
8. **伤害口径**：优先 `OnceResult.exceptDamage`（`NeedOnceResult=true`），拿不到才退
   `Stats.TotalDamage`。老算器 `DONT_USE_EX` 编译时两者相同，这是已知的。
9. **期望伤害的自检**：语义 GA 起步前会反推「哪个口径能把轴里自带的语义数据还原成帧轴」，
   对不上就改用帧轴 + 自动语义老师重新推起点；`--samples > 1` 的多种子平均只是近似。
10. **本地文件都放在 `%LOCALAPPDATA%\PcrTimelineTester\`**（`settings.json`、`server.log`、`crash.log`、
    `icons\`、`unit_info.json`、`roster_presets.json`），不往别处写。
    GUI 跑完会把结果表自动载入，别把这个关了。
11. **冻结版是 `--windowed`，`sys.stdout`/`sys.stderr` 都是 `None`**：任何 `print` 都会抛
    `AttributeError: 'NoneType' object has no attribute 'write'`，而窗口模式下连报错都看不见。
    `gui.main()` 开头会补上这两个流并把未捕获异常写进 `crash.log`，改动别把这段拆了；
    排查启动崩溃用 `python tools\build_frozen.py --console`。
12. **阵容搜索的规则**：候选池与分组独立（分组只约束「最少上几人」，允许组间重叠），队伍固定 5 人，
    枚举必须覆盖所有合法排列（`iter_teams` 去重、跳过队内重复）；紧凑写法 `O` = SET、`X` = AUTO；`every_combo=false` 只跑全 `O`。**请求里永远不带轴里的帧**（`UBExecTimeList=[]`）——轴里的帧是帧模式的手动刀表，不代表 AUTO / SET；AUTO 必须靠 `IsAutoMode=true`（只给 `SetToggles=false` 角色根本不放 UB，实测 40,505,442 vs 149,119,769），SET 靠 `SetToggles[i]=true`；结果、报告、界面只保留伤害最高的 100 支队伍（`roster.RESULT_LIMIT`）。**显示一律按游戏真实站位**（`roster.lineup_order/lineup_names/lineup_combo/lineup_text`；站位 = `searchAreaWidth` 升序、宽度相同则 unitId 小的在前，宽度也在 `unit_info.php` 里，缓存在 `%LOCALAPPDATA%\PcrTimelineTester\unit_info.json`），`SetToggles[i]` 在服务器里是按**站位槽位**生效的（内部按 searchAreaWidth 排序后 `playerUnitCtrl[i].pressing = allSet || setToggles[i]`），所以 `request_for()` 必须把 `PlayerList` 和 `SetToggles` 一起按站位重排（`lineup_order` + `_flag_of`），否则界面上的 OX 会和真实操作错位（曾出现「显示 2.3 亿的组合是 XOXOX」这种事故）。
    发请求是并发的（`roster.POST_WORKERS = 4`，`_chunk_size()` 要凑够这么多批），
    改回串行会让 1 万支队伍从 4 分钟变成 40 分钟。界面进度按 `job.teams_done/teams_total` 走，
    别改回按「代/迭代」算（`RosterJob` 没有 `history`）。
    「试这队 32 种组合」走 `TeamEvaluator.evaluate_all`（2^5 条请求一次性并发，固定跑满 32 种，
    不看勾选）；**读界面变量必须留在主线程**——worker 里碰 Tk `StringVar` 会抛
    `RuntimeError: main thread is not in main loop`，现象是点完只有一行日志、什么都不发生。
13. **角色数据**：角色名/简称来自摸轴器 `Datas`；职能（role）、属性（talent）、站位宽度
    （`search_area_width`）统一来自 `http://192.144.214.135/pcr/unit_info.php`，键是 6 位 unitId，
    第一次抓一次并缓存到 `%LOCALAPPDATA%\PcrTimelineTester\unit_info.json`（`datas.load_unit_info()`
    负责抓，`datas.unit_width()` 只读缓存、绝不联网——界面线程里排序不能卡在网络上）；
    只展示既有职能又有属性的角色；图标只认 `unitId < 190000`，后缀优先 `61 → 31 → 11`。
    打开角色表时若发现 `Datas` 里已有角色在缓存里查不到（`DataBook.units_without_info()`），
    会自动 `load_unit_info(refresh=True)` 重抓一次，靠 `App._info_refreshed` 保证一次会话只抓一次；
    选人窗口（`_character_picker`）也按站位排（`lineup_order`），别改回按职能 / 属性排。
14. **许可**：代码 MIT（`LICENSE.txt`；`pyproject.toml` 里 `license = "MIT"`、`license-files` 指它）。
    游戏名、角色名、角色立绘、从 `unit_info.php` / `Datas` 整理出来的角色资料（职能、属性、宽度）
    版权归 Cygames，**不在 MIT 范围内**；README 的「## 许可」一节是给用户看的归属声明，别删。
    发布冻结版时可以带上图标包，但要在说明里写清「仅供个人研究使用」。

## 开发环境事实

- Python：3.10+（需要 tkinter，官方安装包自带；开发机上另外装过 openpyxl、httpx、PyInstaller）。
- **pip 装不了东西**（本机 TLS 坏，连 `api.github.com` 都握不了手），所以本地没有 pytest：
  跑测试用自带的离线跑测器：

  ```bat
  python tools\run_tests.py            :: 全跑
  python tools\run_tests.py test_ga    :: 只跑文件名含 test_ga 的
  ```

  CI（GitHub Actions）里装真 pytest，用 `python -m pytest -q`。
- 冻结版：

  ```bat
  python tools\build_frozen.py           :: dist\PcrTimelineTester.exe + dist\PcrTimelineTester-icons.zip
  :: 图标取项目根的 icon.ico（构建时扩成 16~256 七种尺寸写进 build/icon.ico 再嵌入 exe）
  :: 构建机要装 Pillow（pip install pillow）；CI 的 workflow 里已经装了
  ```

- 摸轴器（PCRCalculatorServer.exe）就在 PCRCalculator 安装目录的 `server\` 子目录里，
  启动命令 `PCRCalculatorServer.exe --server --host 127.0.0.1 --port 8080`。
  **用户自己的 8080 算器不要动**；本地联调自己起一个别的端口（8091/8092…），用完只杀自己 pid。

## 改完要做的

1. `python -m compileall -q src tests`
2. `python tools/run_tests.py`（本地）/ `python -m pytest -q`（CI）
3. 真机跑一轮（起自己的算器 → `run.bat inspect 轴.xlsx` → 小规模 `tune`/`--mode ga` → 看报告）
4. 顺手清理：`build/`、`dist/`、`runs/`、临时脚本不进仓库（`.gitignore` 已覆盖）
5. 提交信息用中文一句话说清做了什么（例如 `界面：跑完自动载入结果表`）

## 版本

`pyproject.toml` 与 `src/PcrTimelineTester/__init__.py` 的版本号要一起改；tag 用 `vX.Y.Z`，
推 tag 会触发 `build-frozen` 自动构建并发布冻结版。



