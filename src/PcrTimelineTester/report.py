"""Run folder bookkeeping: tee logging, JSON state and the markdown report."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time

from .ga.semantics import SemanSequenceType, SemanSkillType, SemanUBType
from .roster import lineup_names, lineup_text

CONFIG = "config.json"
STATE = "state.json"
REPORT_JSON = "report.json"
REPORT_MD = "报告.md"
LOG = "log.txt"
RESULT_WORKBOOK = "调帧结果.xlsx"


def timestamp_dir(root: Path, stem: str) -> Path:
    """Create ``<root>/<yyyymmdd-HHMMSS>_<stem>`` and return it."""
    root = Path(root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = root / f"{stamp}_{stem}"
    suffix = 2
    while folder.exists():
        folder = root / f"{stamp}_{stem}_{suffix}"
        suffix += 1
    folder.mkdir(parents=True)
    return folder


def write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {Path(path).name}：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{Path(path).name} 必须是对象")
    return data


@dataclass
class RunLog:
    """Print to the console and keep the same text in ``log.txt``."""

    path: Path
    echo: bool = True
    _stream: object | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        text = str(message)
        if self.echo:
            print(text, flush=True)
        stream = self._stream
        if stream is not None:
            stream.write(text + "\n")
            stream.flush()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|")


def _enum_name(enum: type, value: object) -> str:
    """把口径里的整数（或名字）写成枚举名，None 显示成「默认」。"""
    if value is None:
        return "默认"
    try:
        return enum(int(value)).name
    except (TypeError, ValueError):
        return str(value)


def profile_text(profile: dict) -> str:
    """`UB类型 SERIES_1 / 技能类型 SERIES_20 / 序列类型 LANES`。"""
    return (
        f"UB类型 {_enum_name(SemanUBType, profile.get('ub_type'))}"
        f" / 技能类型 {_enum_name(SemanSkillType, profile.get('skill_type'))}"
        f" / 序列类型 {_enum_name(SemanSequenceType, profile.get('sequence_type'))}"
    )


def write_report(path: Path, summary: dict, items: list[dict]) -> None:
    """Human readable Chinese report next to the machine readable JSON one."""
    lines: list[str] = []
    baseline = summary.get("baseline") or {}
    final = summary.get("final") or {}
    lines.append("# PCR 调帧结果")
    lines.append("")
    lines.append(f"- 轴文件：`{summary.get('excel')}`")
    lines.append(f"- 运行目录：`{summary.get('run_dir')}`")
    lines.append(f"- 接口：`{summary.get('endpoint')}`（战斗 {summary.get('battle_time')} 秒，种子 {summary.get('seed')}"
                 f"{'，每个候选取 ' + str(summary.get('samples')) + ' 个种子平均' if (summary.get('samples') or 1) > 1 else ''}）")
    lines.append(f"- 搜索参数：{summary.get('search_text')}")
    lines.append(f"- 待测项：{summary.get('targets')} 项 × {summary.get('passes') or 1} 轮，共 {summary.get('battles')} 场，用时 {summary.get('elapsed_s')} 秒")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if summary.get("cancelled"):
        lines.append("> 本次运行被中断，**没有写回任何帧**（存档保持原样）。")
        lines.append("")
    lines.append(f"- 基线期望伤害：{_int_text(baseline.get('metric'))}（实战伤害 {_int_text(baseline.get('damage'))}，"
                 f"战斗帧 {baseline.get('battle_frames')}，结果 {baseline.get('result_name')}）")
    if final:
        lines.append(f"- 调帧后期望伤害：{_int_text(final.get('metric'))}（实战伤害 {_int_text(final.get('damage'))}，"
                     f"战斗帧 {final.get('battle_frames')}，结果 {final.get('result_name')}）")
        gained = (final.get('metric') or 0) - (baseline.get('metric') or 0)
        lines.append(f"- 变化：{_signed_text(gained)}")
    lines.append(f"- 写回 {summary.get('written')} 项 / 跳过 {summary.get('skipped')} 项 / 失败 {summary.get('failed')} 项")
    if summary.get("result_workbook"):
        lines.append(f"- 结果轴：`{summary.get('result_workbook')}`（导入 PCRCalculator 即可）")
    lines.append("")
    lines.append("## 逐项结果")
    lines.append("")
    lines.append("| 项 | 轮 | 原帧 | 新帧 | 决策 | 基线期望伤害 | 本项最优 | 场次 | 用时 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for item in items:
        lines.append(
            "| {label} | {pass_no} | {old} | {new} | {decision} | {base} | {peak} | {battles} | {elapsed} |".format(
                label=_cell(item.get("label")),
                pass_no=_cell(item.get("pass") or 1),
                old=_cell(item.get("old_frame")),
                new=_cell(item.get("new_frame") or "—"),
                decision=_cell(item.get("decision_text")),
                base=_cell(_int_text(item.get("baseline_metric"))),
                peak=_cell(_int_text(item.get("peak_metric"))),
                battles=_cell(item.get("battles")),
                elapsed=_cell(f"{(item.get('elapsed_ms') or 0) / 1000:.1f}s"),
            )
        )
    problems = [item for item in items if item.get("decision") in ("fail", "cancel")]
    if problems:
        lines.append("")
        lines.append("## 失败与中断")
        lines.append("")
        for item in problems:
            lines.append(f"- {item.get('label')}：{item.get('message') or item.get('decision_text')}")
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 阈值只决定「还要不要继续往外探」，不决定最终选帧；最终帧永远取同伤害里最早的帧。")
    lines.append("- 任一采样失败即中止该行搜索并按失败计数，绝不把失败当 0 伤害静默跳过。")
    lines.append("- 搜索期间只在内存里累计改动，全部结束后才写一次回文件；中途中断不会留下半调好的轴。")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ga_report(path: Path, summary: dict) -> None:
    """语义 GA 模式的报告：最优伤害、代数、模拟次数、代际表。"""
    profile = summary.get("semantic") or {}
    lines: list[str] = []
    lines.append("# PCR 语义 UB 优化结果")
    lines.append("")
    lines.append(f"- 轴文件：`{summary.get('excel')}`")
    lines.append(f"- 运行目录：`{summary.get('run_dir')}`")
    lines.append(f"- 优化模式：语义 GA")
    lines.append(f"- 接口：`{summary.get('endpoint')}`（战斗 {summary.get('battle_time')} 秒，种子 {summary.get('seed')}"
                 f"{'，每个候选取 ' + str(summary.get('samples')) + ' 个种子平均' if (summary.get('samples') or 1) > 1 else ''}）")
    lines.append(f"- 转换口径：{profile_text(profile)}")
    if summary.get("profile_detected"):
        lines.append("- 口径自检：原来选的口径还原不回这条轴，已按轴里的语义基因自动对上（见上面这行）")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if summary.get("cancelled"):
        lines.append("> 本次运行被中断，**没有导出结果轴**（原存档保持原样；可以续跑）。")
        lines.append("")
    baseline = summary.get("baseline") or 0
    final = summary.get("final") or 0
    reference = summary.get("frame_damage")
    if reference:
        lines.append(f"- 帧轴参考伤害：{_int_text(reference)}（同一条轴的帧模式打出来的伤害）")
        delta = float(baseline) - float(reference)
        if abs(delta) > max(1.0, abs(float(reference)) * 0.001):
            lines.append(
                f"- :warning: 语义起点和帧轴伤害差了 {_signed_text(delta)}，"
                "说明语义口径或 UB 条件和这条轴对不上，下面的数字不可信"
                "（把「序列类型」改回只调五条 UB、或换「技能类型」再跑）"
            )
    lines.append(f"- 基线期望伤害：{_int_text(baseline)}")
    lines.append(f"- 最优期望伤害：{_int_text(final)}（{_signed_text(float(final) - float(baseline))}）")
    lines.append(f"- 代数：跑到第 {summary.get('generations')} 代，最优来自第 {summary.get('best_generation')} 代")
    lines.append(f"- 模拟场次：{summary.get('simulations')} 场（精确重复 {summary.get('exact_duplicates')} / 行为重复 {summary.get('behavior_duplicates')}）")
    ga = summary.get("ga") or {}
    elite = ga.get("elite_count")
    decay = ga.get("mutation_decay")
    floor = ga.get("mutation_floor")
    if elite is not None or decay is not None:
        lines.append(
            f"- 策略：精英保留 {elite} / 突变衰减 {decay}（下限 {floor}）"
            f" / 停滞停止 {ga.get('stagnation_limit') or '关'}"
            f" / 重启 {summary.get('restarts') or 1} 轮"
        )
    if summary.get("result_workbook"):
        lines.append(f"- 结果轴：`{summary.get('result_workbook')}`（导入 PCRCalculator 即可，已开启语义 UB）")
    lines.append("")
    export = summary.get("export") or {}
    lanes = export.get("lanes") or []
    if lanes:
        lines.append("## 最优 UB 序列")
        lines.append("")
        lines.append("| 角色 | UB 序列 |")
        lines.append("| --- | --- |")
        for index, lane in enumerate(lanes):
            lines.append(f"| 角色{index + 1} | {'、'.join(str(gene) for gene in lane)} |")
        if export.get("sequence"):
            lines.append("")
            lines.append(f"- 角色序列：{'、'.join(str(role) for role in export['sequence'])}")
        lines.append("")
    rounds = summary.get("rounds") or []
    if len(rounds) > 1:
        lines.append("## 重启轮次")
        lines.append("")
        lines.append("| 轮 | 随机种子 | 跑了几代 | 最优期望伤害 | 累计模拟 | 提前停止 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in rounds:
            lines.append(
                f"| {item.get('round')} | {item.get('seed')} | {item.get('generations')}"
                f" | {_int_text(item.get('damage'))} | {item.get('simulations')}"
                f" | {'是' if item.get('stopped_early') else '否'} |"
            )
        lines.append("")
    history = summary.get("history") or []
    if history:
        lines.append("## 代际")
        lines.append("")
        lines.append("| 代 | 最优期望伤害 | 最优来自 | 累计模拟 |")
        lines.append("| --- | --- | --- | --- |")
        for entry in history:
            lines.append(
                f"| {_cell(entry.get('generation'))} | {_cell(_int_text(entry.get('damage')))} "
                f"| {_cell(entry.get('best_generation'))} | {_cell(entry.get('simulations'))} |"
            )
        lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 每一代只对没评估过的基因提交战斗，重复基因直接复用结果。")
    lines.append("- 行为重复（同样出手顺序）按指纹距离剔除，不再浪费场次。")
    lines.append("- 中断不会导出任何轴；`state.json` 里存着完整检查点，可以续跑。")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _int_text(value: object) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.1f}"


def _signed_text(value: float) -> str:
    return f"+{_int_text(value)}" if value > 0 else _int_text(value)


def write_anneal_report(path: Path, summary: dict) -> None:
    """退火模式的报告：最优伤害、迭代/温度、接受率、迭代轨迹。"""
    profile = summary.get("profile") or summary.get("semantic") or {}
    anneal = summary.get("anneal") or {}
    lines: list[str] = []
    lines.append("# PCR 语义 UB 退火结果")
    lines.append("")
    lines.append(f"- 轴文件：`{summary.get('excel')}`")
    lines.append(f"- 运行目录：`{summary.get('run_dir')}`")
    lines.append("- 优化模式：退火（模拟退火）")
    lines.append(
        f"- 接口：`{summary.get('endpoint')}`（战斗 {summary.get('battle_time')} 秒，种子 {summary.get('seed')}"
        f"{'，每个候选取 ' + str(summary.get('samples')) + ' 个种子平均' if (summary.get('samples') or 1) > 1 else ''}）"
    )
    lines.append(f"- 转换口径：{profile_text(profile)}")
    if summary.get("profile_detected"):
        lines.append("- 口径自检：原来选的口径还原不回这条轴，已按轴里的语义基因自动对上（见上面这行）")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if summary.get("cancelled"):
        lines.append("> 本次运行被中断，**没有导出结果轴**（原存档保持原样；可以续跑）。")
        lines.append("")
    baseline = summary.get("baseline") or 0
    final = summary.get("final") or 0
    reference = summary.get("frame_damage")
    if reference:
        lines.append(f"- 帧轴参考伤害：{_int_text(reference)}（同一条轴的帧模式打出来的伤害）")
        delta = float(baseline) - float(reference)
        if abs(delta) > max(1.0, abs(float(reference)) * 0.001):
            lines.append(
                f"- :warning: 语义起点和帧轴伤害差了 {_signed_text(delta)}，"
                "说明语义口径或 UB 条件和这条轴对不上，下面的数字不可信"
            )
    lines.append(f"- 基线期望伤害：{_int_text(baseline)}")
    lines.append(f"- 最优期望伤害：{_int_text(final)}（{_signed_text(float(final) - float(baseline))}）")
    lines.append(
        f"- 迭代：跑到第 {summary.get('iterations')} 次（共 {summary.get('total_iterations')} 次），"
        f"最优来自第 {summary.get('best_iteration')} 次"
    )
    lines.append(f"- 扫描：接受 {summary.get('accepted'):,} / 拒绝 {summary.get('rejected'):,}")
    lines.append(f"- 模拟场次：{summary.get('simulations'):,} 场")
    lines.append(
        f"- 温度：{anneal.get('initial_temperature')} → {anneal.get('final_temperature')}（几何降温）"
        f" / 链长上限 {anneal.get('chain_range')}"
    )
    lines.append(
        f"- 邻域概率：单点 {anneal.get('swap_chance')} / 双点 {anneal.get('double_swap_chance')}"
        f" / 链式 {anneal.get('chain_chance')} / 手动 {anneal.get('manual_chance')}"
        f" / 重启 {summary.get('restarts') or 1} 轮"
    )
    if summary.get("result_workbook"):
        lines.append(f"- 结果轴：`{summary.get('result_workbook')}`（导入 PCRCalculator 即可，已开启语义 UB）")
    lines.append("")
    export = summary.get("export") or {}
    lanes = export.get("lanes") or []
    if lanes:
        lines.append("## 最优 UB 序列")
        lines.append("")
        lines.append("| 角色 | UB 序列 |")
        lines.append("| --- | --- |")
        for index, lane in enumerate(lanes):
            lines.append(f"| 角色{index + 1} | {'、'.join(str(gene) for gene in lane)} |")
        if export.get("sequence"):
            lines.append("")
            lines.append(f"- 角色序列：{'、'.join(str(role) for role in export['sequence'])}")
        lines.append("")
    rounds = summary.get("rounds") or []
    if len(rounds) > 1:
        lines.append("## 重启轮次")
        lines.append("")
        lines.append("| 轮 | 随机种子 | 迭代次数 | 最优期望伤害 | 累计模拟 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in rounds:
            lines.append(
                f"| {item.get('round')} | {item.get('seed')} | {item.get('iterations')} "
                f"| {_int_text(item.get('damage'))} | {item.get('simulations')} |"
            )
        lines.append("")
    history = summary.get("history") or []
    if history:
        lines.append("## 迭代轨迹")
        lines.append("")
        lines.append("| 迭代 | 温度 | 当前期望伤害 | 最优期望伤害 | 累计模拟 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in history[-40:]:
            lines.append(
                f"| {item.get('iteration')} | {_int_text(item.get('temperature'))} "
                f"| {_int_text(item.get('current'))} | {_int_text(item.get('best'))} "
                f"| {item.get('simulations')} |"
            )
        lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 评分用的是「期望伤害 + 通关剩余时间奖励」，与逐帧搜索同一套口径。")
    lines.append("- 邻域算子按概率挑：单点替换 / 双点互换 / 链式替换 / 手动指定（链长上限受配置限制）。")
    lines.append("- 允许重复状态关闭时，算过的基因直接复用分数，不重复请求摸轴器。")
    lines.append("- 中断不会导出结果轴，`state.json` 里留了迭代与最优，可以直接续跑。")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def write_roster_report(path: Path, summary: dict, *, names: dict[int, str] | None = None) -> None:
    """阵容搜索的报告：把候选池里所有队伍组合排完，给出最优阵容与每人的操作。"""
    names = names or {}
    export = summary.get("export") or {}
    reference_score = summary.get("reference") or {}
    reference = int(reference_score.get("damage") or 0) if reference_score else None
    final = summary.get("final") or 0
    combos = int(summary.get("combos") or 1)
    groups = summary.get("groups") or []
    lines: list[str] = []
    lines.append("# PCR 阵容搜索结果")
    lines.append("")
    lines.append(f"- 轴文件：`{summary.get('excel')}`")
    lines.append(f"- 运行目录：`{summary.get('run_dir')}`")
    lines.append("- 优化模式：阵容搜索（把所有队伍组合排完）")
    lines.append(
        f"- 接口：`{summary.get('endpoint')}`（战斗 {summary.get('battle_time')} 秒，种子 {summary.get('seed')}）"
    )
    lines.append(f"- 候选：{len(summary.get('pool') or [])} 个角色，队伍固定 5 人")
    if groups:
        for group in groups:
            members = "、".join(names.get(int(unit), str(unit)) for unit in (group.get("members") or []))
            lines.append(
                f"- 分组 {group.get('name')}：最少 {group.get('minimum')} 人"
                + (f"（{members}）" if members else "")
            )
    else:
        lines.append("- 分组：没有（全部从候选池里抽）")
    lines.append(f"- 组合：{_int_text(summary.get('teams_total'))} 支队伍 × {combos} 种 AUTO/SET")
    lines.append(f"- 属性：{'满级满 Rank' if summary.get('to_max_attr') else '沿用轴里的练度'}")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 参考伤害（导入轴原来那队）：{_int_text(reference) if reference is not None else '没有参考'}")
    lines.append(f"- 最优期望伤害：{_int_text(final)}")
    if reference:
        delta = int(final) - int(reference)
        lines.append(f"- 相对参考：{'+' if delta >= 0 else ''}{_int_text(delta)}")
    lines.append(f"- 已排队伍：{_int_text(summary.get('teams_done'))} / {_int_text(summary.get('teams_total'))}")
    lines.append(f"- 模拟场次：{_int_text(summary.get('battles'))} 场")
    if summary.get("result_workbook"):
        lines.append(f"- 结果轴：`{summary.get('result_workbook')}`（角色已换成最优阵容，导入 PCRCalculator 即可）")
    lines.append("")
    lines.append("## 最优阵容")
    lines.append("")
    lines.append("| 队伍 | 角色 | 每位角色的操作 |")
    lines.append("| --- | --- | --- |")
    team = export.get("team") or []
    toggles = export.get("toggles") or []
    if team:
        text = lineup_names(team, names)
        operation = lineup_text(team, toggles, names)
        lines.append(f"| {_cell('最优阵容（按站位）')} | {_cell(text)} | {_cell(operation)} |")
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    teams = summary.get("teams") or []
    if teams:
        lines.append(f"## 前 {len(teams)} 支队伍")
        lines.append("")
        lines.append("| # | 队伍 | 每位角色的操作 | 伤害 |")
        lines.append("| --- | --- | --- | --- |")
        for rank, item in enumerate(teams, start=1):
            members = item.get("team") or []
            flags = item.get("toggles") or []
            text = lineup_names(members, names)
            operation = lineup_text(members, flags, names)
            lines.append(
                f"| {rank} | {_cell(text)} | {_cell(operation)} | {_int_text(item.get('damage'))} |"
            )
        lines.append("")
    lines.append("- 每支候选队伍都把「每个人 AUTO / SET」的组合各打一遍（5 人 = 32 种），"
                 "取伤害最高的那种作为这支队的分；上表记下最终采用的操作。")
    lines.append("- 请求里不带导入轴的帧：`O` = SET（一有机会就按），`X` = AUTO（角色自己找时机，靠 `IsAutoMode`）。")
    lines.append("- 角色按游戏里的站位（攻击范围从小到大）排列并标了号码，队伍里的先后只是枚举顺序。")
    lines.append("- 分组里设了最少人数的，每支队伍先抽够这个人数，剩下的名额从未分组的角色里补；"
                 "没设最少人数的分组等于没分组，成员回落到未分组一起抽。")
    lines.append("- 所有组合都会排到（`队伍数 = 各组 C(成员, 最少) 连乘 × C(未分组, 剩余名额)`），不做随机采样。")
    lines.append("- 结果表与报告只列伤害最高的 100 支队伍（跑完会更多，内存里只留前 100）。")
    lines.append("- 中断不会导出结果轴，`state.json` 记着排到第几支队伍，`resume` 接着跑。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_zero_report(path: Path, summary: dict, *, names: dict[int, str] | None = None) -> None:
    """0 押轴的报告：退火找到的逐刀 O/X 设定与刷新过程。"""
    names = names or {}
    best = summary.get("best") or {}
    units = [int(unit) for unit in (summary.get("units") or ())]
    plan = best.get("plan") or []
    lines: list[str] = []
    lines.append("# PCR 0押轴结果")
    lines.append("")
    lines.append(f"- 轴文件：`{summary.get('excel')}`")
    lines.append(f"- 运行目录：`{summary.get('run_dir')}`")
    lines.append("- 优化模式：0押轴（整场不打手动 UB，退火逐刀决定 O=SET / X=AUTO）")
    lines.append(
        f"- 接口：`{summary.get('endpoint')}`（战斗 {summary.get('battle_time')} 秒，种子 {summary.get('seed')}）"
    )
    if units:
        lines.append(f"- 站位：{lineup_names(units, names)}")
    lines.append(
        f"- 退火规模：{_int_text(summary.get('iterations'))} 轮 × 每轮 {_int_text(summary.get('batch'))} 个候选 × "
        f"{_int_text(summary.get('samples'))} 个种子；实际模拟 {_int_text(summary.get('battles'))} 场"
    )
    lines.append(
        f"- 最多几刀：每个角色最多排 {_int_text(summary.get('ub_limit'))} 刀"
        "（实际能排几刀跟着回包里的 TP 满帧条数走——刀数本来就会随打法变化）"
    )
    baseline = summary.get("baseline") or {}
    lines.append(
        f"- 基线：纯 SET {_int_text(baseline.get('set_damage'))} ／ 纯 AUTO {_int_text(baseline.get('auto_damage'))}"
    )
    lines.append(f"- 属性：{'满级满 Rank' if summary.get('to_max_attr') else '沿用轴里的练度'}")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 最优期望伤害：{_int_text(best.get('damage'))}")
    lines.append(f"- 战斗帧 / 结果：{_int_text(best.get('frames'))} 帧 / {_result_label(best.get('result'))}")
    if summary.get("result_workbook"):
        lines.append(f"- 结果表：`{summary.get('result_workbook')}`（轴保持原样，另加一页「0押结果」）")
    lines.append("")
    lines.append("## 最优方案的逐刀设定")
    lines.append("")
    lines.append("| 站位 | 角色 | 逐刀设定（O=SET、X=AUTO） |")
    lines.append("| --- | --- | --- |")
    for index, unit in enumerate(units):
        flags = plan[index] if index < len(plan) else []
        body = "".join("O" if flag else "X" for flag in flags) or "O"
        tail = "O" if (flags[-1] if flags else True) else "X"
        lines.append(f"| {index + 1} | {_cell(names.get(unit, str(unit)))} | `{body}`（之后一律 {tail}） |")
    lines.append("")
    history = list(summary.get("history") or ())
    if history:
        lines.append("## 刷新记录")
        lines.append("")
        lines.append("| 轮次 | 逐刀设定 | 期望伤害 |")
        lines.append("| --- | --- | --- |")
        for item in history:
            lines.append(
                f"| {_int_text(item.get('round'))} | `{_cell(item.get('text') or '')}` | {_int_text(item.get('damage'))} |"
            )
        lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 0 押 = 不打手动 UB，只决定「TP 满的那一刻按不按」：`O` = 到点就按（SET），`X` = 不按，交给自动（会等角色空下来）。")
    lines.append("- 判据取自接口：`Stats.TpChangeEvents` 里 `CrossedReady` 的那一帧就是 TP 刚满的帧（`CalcStats` 带 256 才有）。某个角色第 k 刀选 `O`，就把第 k 个 TP 满帧写进 `UBExecTimeList`。")
    lines.append("- 同一角色逐刀可以不一样：`O X O O …` 表示第一次 TP 满时按、第二次不按、第三次起都按；表里省略的刀一律沿用最后一刀。")
    lines.append("- 刀数不写死：跳过一刀会让后面 TP 满的时机整体变化，所以每轮都用最近一次回包重新算 TP 满帧，能按出 14 刀时也会排到 14 刀（上限「最多几刀」）。")
    lines.append("- 请求里不带导入轴原本的帧，`SetToggles` 全关、`IsAutoMode` 开（没写进帧表的刀由自动兜底）。")
    lines.append("- 中断不会导出结果表，`state.json` 记着当前最优方案，`resume` 接着跑。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _result_label(value: object) -> str:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return {0: "胜利", 1: "失败", 2: "超时", 3: "撤退"}.get(result, str(result))
