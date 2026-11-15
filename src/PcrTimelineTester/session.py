"""One tuning session: the job plus the files it leaves behind.

Both front ends (the console and the Tk GUI) drive exactly this class, so a run
started from a window behaves like a run started from a terminal: same
``config.json`` / ``state.json`` / ``report.json`` / ``报告.md`` / ``调帧结果.xlsx``,
same "write the axis once at the end" rule, same exit codes.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from .frames import text
from .job import JobSettings, TuneJob
from .roster import lineup_names, lineup_text
from .report import (
    CONFIG, LOG, REPORT_JSON, REPORT_MD, RESULT_WORKBOOK, STATE, write_json, write_report,
)
from .savefile import SaveFile, write_tuned_save
from .search import SearchSettings

OK = 0
CANCELLED = 130


def _number_text(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def workbook_summary(summary: dict, job: TuneJob) -> list[list[Any]]:
    """Human readable sheet that ships next to the tuned axis."""
    baseline = summary.get("baseline") or {}
    final = summary.get("final") or {}
    rows: list[list[Any]] = [
        ["PCR 调帧结果", summary.get("excel")],
        ["接口", f"{summary.get('endpoint')}（{summary.get('battle_time')} 秒 / 种子 {summary.get('seed')}）"],
        ["搜索参数", summary.get("search_text")],
        ["循环轮数", summary.get("passes") or 1],
        ["基线期望伤害", _number_text(baseline.get("metric"))],
        ["调帧后期望伤害", _number_text(final.get("metric"))],
        ["实战伤害（基线 → 最终）", f"{_number_text(baseline.get('damage'))} → {_number_text(final.get('damage'))}"],
        ["写回 / 跳过 / 失败", f"{job.written} / {job.skipped} / {job.failed}"],
        [],
        ["项", "轮", "原帧 → 新帧", "期望伤害（基线 → 本项最优）"],
    ]
    for item in job.accepted:
        rows.append([
            item.label,
            item.pass_no,
            f"{text(item.old_raw)} → {text(item.new_raw)}",
            f"{item.old_metric:,.0f} → {item.new_metric:,.0f}",
        ])
    rows.append([])
    rows.append(["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    rows.append(["说明", "本文件由 PcrTimelineTester 生成；导入 PCRCalculator 即可使用（saveSheet 为隐藏数据页）。"])
    return rows


class TuneSession:
    """Create the job, run it, and write the run folder."""

    def __init__(
        self,
        save: SaveFile,
        settings: JobSettings,
        search: SearchSettings,
        run_dir: Path,
        *,
        log: Callable[[str], None] = print,
        resume_state: dict | None = None,
        client: Any = None,
    ) -> None:
        self.save = save
        self.settings = settings
        self.search = search
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.summary: dict = {}
        self.workbook: Path | None = None
        write_json(self.run_dir / CONFIG, {
            "excel": str(save.path),
            "settings": settings.to_dict(),
            "search": search.to_dict(),
            "header": save.header,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.job = TuneJob(save, settings, search, log=log, client=client)
        if resume_state:
            self.job.apply_state(resume_state)
            log(f"续跑：已完成 {len(self.job.done)} 项，已有写回 {self.job.written} 项")

    # ---------------------------------------------------------------- control

    def cancel(self) -> None:
        self.job.cancel()

    @property
    def cancelled_requested(self) -> bool:
        return self.job.cancelled_requested

    @property
    def log_path(self) -> Path:
        return self.run_dir / LOG

    @property
    def report_path(self) -> Path:
        return self.run_dir / REPORT_MD

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        """Blocking: returns ``OK`` or ``CANCELLED``; nothing is raised for cancels."""
        summary = self.job.run()
        summary["run_dir"] = str(self.run_dir)
        self.summary = summary
        write_json(self.run_dir / STATE, self.job.state())
        if self.job.cancelled:
            write_json(self.run_dir / REPORT_JSON, summary)
            write_report(self.run_dir / REPORT_MD, summary, self.job.items)
            self.log("已中断：没有写回任何帧（存档保持原样）。")
            self.log(f"续跑：PcrTimelineTester resume \"{self.run_dir}\"")
            return CANCELLED
        damage = int((summary.get("final") or {}).get("damage") or 0)
        workbook = self.run_dir / RESULT_WORKBOOK
        write_tuned_save(
            workbook, self.save, self.job.frames,
            battle_seed=self.settings.seed, damage=damage, summary=workbook_summary(summary, self.job),
        )
        summary["result_workbook"] = str(workbook)
        self.workbook = workbook
        write_json(self.run_dir / REPORT_JSON, summary)
        write_report(self.run_dir / REPORT_MD, summary, self.job.items)
        self.log(f"结果轴 {workbook}")
        self.log(f"报告 {self.run_dir / REPORT_MD}")
        self.log(f"日志 {self.run_dir / LOG}")
        return OK

    def close(self) -> None:
        self.job.close()

    def __enter__(self) -> "TuneSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def ga_workbook_summary(summary: dict) -> list[list[Any]]:
    """Human readable sheet that ships next to the optimized semantic axis."""
    profile = summary.get("semantic") or {}
    rows: list[list[Any]] = [
        ["PCR 语义 UB 优化结果", summary.get("excel")],
        ["优化模式", "语义 GA"],
        ["接口", f"{summary.get('endpoint')}（{summary.get('battle_time')} 秒 / 种子 {summary.get('seed')}）"],
        ["转换口径", f"UB类型 {profile.get('ub_type')} / 技能类型 {profile.get('skill_type')} / 序列类型 {profile.get('sequence_type')}"],
        ["种群 / 代数", f"{summary.get('population')} 个体 × {summary.get('generations')} 代（岛屿 {summary.get('islands')}）"],
        ["基线期望伤害", _number_text(summary.get("baseline"))],
        ["最优期望伤害", _number_text(summary.get("final"))],
        ["模拟场次", f"{summary.get('simulations'):,} 场（重复 {summary.get('exact_duplicates', 0) + summary.get('behavior_duplicates', 0)} 次）"],
        ["最优来自", f"第 {summary.get('best_generation')} 代"],
        [],
        ["角色", "UB 序列"],
    ]
    for index, lane in enumerate((summary.get("export") or {}).get("lanes") or []):
        rows.append([f"角色{index + 1}", "、".join(str(gene) for gene in lane)])
    rows.append([])
    rows.append(["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    rows.append(["说明", "本文件由 PcrTimelineTester 生成；导入 PCRCalculator 即可使用（saveSheet 为隐藏数据页，已开启语义 UB）。"])
    return rows


class GaSession:
    """语义 GA 模式的会话：同一套运行目录与产物，结果轴叫「优化结果.xlsx」。"""

    workbook_name = "优化结果.xlsx"

    def __init__(
        self,
        save: SaveFile,
        settings: "GaSettings",
        run_dir: Path,
        *,
        log: Callable[[str], None] = print,
        resume_state: dict | None = None,
        evaluator: Any = None,
    ) -> None:
        from .ga.job import GaJob  # 延迟导入：帧模式不应该为 GA 付导入成本

        self.save = save
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.summary: dict = {}
        self.workbook: Path | None = None
        write_json(self.run_dir / CONFIG, {
            "excel": str(save.path),
            "mode": "ga",
            "settings": settings.to_dict(),
            "header": save.header,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.job = GaJob(save, settings, log=log, evaluator=evaluator)
        if resume_state:
            self.job.apply_state(resume_state)

    # ---------------------------------------------------------------- control

    def cancel(self) -> None:
        self.job.cancel_run()

    @property
    def cancelled_requested(self) -> bool:
        return self.job.cancelled_requested

    @property
    def log_path(self) -> Path:
        return self.run_dir / LOG

    @property
    def report_path(self) -> Path:
        return self.run_dir / REPORT_MD

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        from .ga.job import GaCancelled
        from .report import write_ga_report

        started = time.perf_counter()
        try:
            summary = self.job.run()
        except GaCancelled:
            summary = self.job.summary(time.perf_counter() - started)
            self.log("已停止：没有导出任何结果轴（原存档保持原样）。")
        summary["run_dir"] = str(self.run_dir)
        self.summary = summary
        write_json(self.run_dir / STATE, self.job.state_dict())
        cancelled = bool(summary.get("cancelled"))
        write_json(self.run_dir / REPORT_JSON, summary)
        write_ga_report(self.run_dir / REPORT_MD, summary)
        if cancelled:
            self.log(f"续跑：PcrTimelineTester resume \"{self.run_dir}\"")
            return CANCELLED
        export = summary.get("export") or {}
        workbook = self.run_dir / self.workbook_name
        write_tuned_save(
            workbook, self.save, export.get("frames") or [],
            battle_seed=self.settings.seed,
            damage=int(summary.get("final") or 0),
            summary=ga_workbook_summary({**summary, **self._spread()}),
            sheet_name="优化结果",
            semantic=export.get("lanes"),
            sequence=export.get("sequence"),
        )
        summary["result_workbook"] = str(workbook)
        self.workbook = workbook
        write_json(self.run_dir / REPORT_JSON, summary)
        write_ga_report(self.run_dir / REPORT_MD, summary)
        self.log(f"结果轴 {workbook}")
        self.log(f"报告 {self.run_dir / REPORT_MD}")
        self.log(f"日志 {self.run_dir / LOG}")
        return OK

    def _spread(self) -> dict:
        """把 GAConfig 的字段摊平进 summary，方便写表格。"""
        config = self.settings.ga_config()
        data = config.to_dict()
        data.pop("seed", None)
        return data

    def close(self) -> None:
        self.job.close()

    def __enter__(self) -> "GaSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def anneal_workbook_summary(summary: dict) -> list[list[Any]]:
    """退火模式的结果表：和 GA 一样带语义序列，另加温度与接受率。"""
    profile = summary.get("semantic") or {}
    anneal = summary.get("anneal") or {}
    accepted = int(summary.get("accepted") or 0)
    rejected = int(summary.get("rejected") or 0)
    rows: list[list[Any]] = [
        ["PCR 语义 UB 退火结果", summary.get("excel")],
        ["优化模式", "退火（模拟退火）"],
        ["接口", f"{summary.get('endpoint')}（{summary.get('battle_time')} 秒 / 种子 {summary.get('seed')}）"],
        ["转换口径", f"UB类型 {profile.get('ub_type')} / 技能类型 {profile.get('skill_type')} / 序列类型 {profile.get('sequence_type')}"],
        ["温度", f"{anneal.get('initial_temperature')} → {anneal.get('final_temperature')}（几何降温）"],
        ["迭代 / 重启", f"{summary.get('iterations')} / {summary.get('total_iterations')} 次 × {summary.get('restarts')} 轮"],
        ["接受 / 拒绝", f"{accepted:,} / {rejected:,}"],
        ["基线期望伤害", _number_text(summary.get("baseline"))],
        ["最优期望伤害", _number_text(summary.get("final"))],
        ["模拟场次", f"{summary.get('simulations'):,} 场"],
        ["最优来自", f"第 {summary.get('best_iteration')} 次迭代"],
        [],
        ["角色", "UB 序列"],
    ]
    for index, lane in enumerate((summary.get("export") or {}).get("lanes") or []):
        rows.append([f"角色{index + 1}", "、".join(str(gene) for gene in lane)])
    rows.append([])
    rows.append(["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    rows.append(["说明", "本文件由 PcrTimelineTester 生成；导入 PCRCalculator 即可使用（saveSheet 为隐藏数据页，已开启语义 UB）。"])
    return rows


class AnnealSession:
    """退火模式的会话：产物与 GA 一致，结果轴叫「退火结果.xlsx」。"""

    workbook_name = "退火结果.xlsx"

    def __init__(
        self,
        save: SaveFile,
        settings: "AnnealSettings",
        run_dir: Path,
        *,
        log: Callable[[str], None] = print,
        resume_state: dict | None = None,
        evaluator: Any = None,
    ) -> None:
        from .anneal import AnnealJob

        self.save = save
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.summary: dict = {}
        self.workbook: Path | None = None
        write_json(self.run_dir / CONFIG, {
            "excel": str(save.path),
            "mode": "anneal",
            "settings": settings.to_dict(),
            "header": save.header,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.job = AnnealJob(save, settings, log=log, evaluator=evaluator)
        if resume_state:
            self.job.apply_state(resume_state)

    # ---------------------------------------------------------------- control

    def cancel(self) -> None:
        self.job.cancel_run()

    @property
    def cancelled_requested(self) -> bool:
        return self.job.cancelled_requested

    @property
    def log_path(self) -> Path:
        return self.run_dir / LOG

    @property
    def report_path(self) -> Path:
        return self.run_dir / REPORT_MD

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        from .anneal import AnnealCancelled
        from .report import write_anneal_report

        started = time.perf_counter()
        try:
            summary = self.job.run()
        except AnnealCancelled:
            summary = self.job.summary(time.perf_counter() - started)
            self.log("已停止：没有导出任何结果轴（原存档保持原样）。")
        summary["run_dir"] = str(self.run_dir)
        self.summary = summary
        write_json(self.run_dir / STATE, self.job.state_dict())
        cancelled = bool(summary.get("cancelled"))
        write_json(self.run_dir / REPORT_JSON, summary)
        write_anneal_report(self.run_dir / REPORT_MD, summary)
        if cancelled:
            self.log(f"续跑：PcrTimelineTester resume \"{self.run_dir}\"")
            return CANCELLED
        export = summary.get("export") or {}
        workbook = self.run_dir / self.workbook_name
        write_tuned_save(
            workbook, self.save, export.get("frames") or [],
            battle_seed=self.settings.seed,
            damage=int(summary.get("final") or 0),
            summary=anneal_workbook_summary(summary),
            sheet_name="退火结果",
            semantic=export.get("lanes"),
            sequence=export.get("sequence"),
        )
        summary["result_workbook"] = str(workbook)
        self.workbook = workbook
        write_json(self.run_dir / REPORT_JSON, summary)
        write_anneal_report(self.run_dir / REPORT_MD, summary)
        self.log(f"结果轴 {workbook}")
        self.log(f"报告 {self.run_dir / REPORT_MD}")
        self.log(f"日志 {self.run_dir / LOG}")
        return OK

    def close(self) -> None:
        self.job.close()

    def __enter__(self) -> "AnnealSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------- 阵容搜索
def roster_workbook_summary(summary: dict, *, names: dict[int, str] | None = None) -> list[list[Any]]:
    """阵容搜索的结果表：最优阵容一行，带角色名与每人的 AUTO/SET。"""
    names = names or {}
    export = summary.get("export") or {}
    combos = int(summary.get("combos") or 1)
    groups = summary.get("groups") or []
    rows: list[list[Any]] = [
        ["PCR 阵容搜索结果", summary.get("excel")],
        ["优化模式", "阵容搜索（把所有队伍组合排完）"],
        ["接口", f"{summary.get('endpoint')}（{summary.get('battle_time')} 秒 / 种子 {summary.get('seed')}）"],
        ["候选角色", f"{len(summary.get('pool') or [])} 个，队伍固定 5 人"],
        ["分组", "；".join(str(group.get("label") or group.get("name")) for group in groups) or "没有分组"],
        ["队伍 / 组合", f"{int(summary.get('teams_total') or 0):,} 支 × {combos} 种 AUTO/SET"],
        ["属性", "满级满 Rank" if summary.get("to_max_attr") else "沿用轴里的练度"],
        ["参考伤害", _number_text((summary.get("reference") or {}).get("damage"))],
        ["最优期望伤害", _number_text(summary.get("final"))],
        ["模拟场次", f"{int(summary.get('battles') or 0):,} 场"],
        [],
        ["队伍", "角色", "每位角色的操作"],
    ]
    team = export.get("team") or []
    toggles = export.get("toggles") or []
    if team:
        rows.append(
            [
                "最优阵容（按站位）",
                lineup_names(team, names),
                lineup_text(team, toggles, names),
            ]
        )
    rows.append([])
    rows.append(["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    teams = summary.get("teams") or []
    if teams:
        rows.append([])
        rows.append([f"前 {len(teams)} 支队伍", "", ""])
        for rank, item in enumerate(teams, start=1):
            members = item.get("team") or []
            flags = item.get("toggles") or []
            rows.append(
                [
                    f"第 {rank} 名",
                    lineup_names(members, names),
                    f"{lineup_text(members, flags, names)} ｜ 伤害 {_number_text(item.get('damage'))}",
                ]
            )
    rows.append(["说明", "本文件由 PcrTimelineTester 生成；导入 PCRCalculator 即可使用（savePage 为隐藏数据页，角色已换成最优阵容）。"])
    return rows


class RosterSession:
    """阵容搜索的会话：结果轴叫「阵容结果.xlsx」，角色表换成最优阵容。"""

    workbook_name = "阵容结果.xlsx"

    def __init__(
        self,
        save: SaveFile,
        settings: "RosterSettings",
        run_dir: Path,
        *,
        log: Callable[[str], None] = print,
        resume_state: dict | None = None,
        evaluator: Any = None,
        names: dict[int, str] | None = None,
    ) -> None:
        from .roster import RosterJob

        self.save = save
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.summary: dict = {}
        self.workbook: Path | None = None
        self.names = dict(names or {})
        write_json(self.run_dir / CONFIG, {
            "excel": str(save.path),
            "mode": "roster",
            "settings": settings.to_dict(),
            "header": save.header,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.job = RosterJob(save, settings, log=log, evaluator=evaluator)
        self.job.names = dict(self.names)
        if resume_state:
            self.job.apply_state(resume_state)

    # ---------------------------------------------------------------- control

    def cancel(self) -> None:
        self.job.cancel_run()

    @property
    def cancelled_requested(self) -> bool:
        return self.job.cancelled_requested

    @property
    def log_path(self) -> Path:
        return self.run_dir / LOG

    @property
    def report_path(self) -> Path:
        return self.run_dir / REPORT_MD

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        from .report import write_roster_report
        from .roster import save_with_team

        started = time.perf_counter()
        summary = self.job.run()
        summary["run_dir"] = str(self.run_dir)
        self.summary = summary
        write_json(self.run_dir / STATE, self.job.state_dict())
        cancelled = bool(summary.get("cancelled"))
        write_json(self.run_dir / REPORT_JSON, summary)
        write_roster_report(self.run_dir / REPORT_MD, summary, names=self.names)
        if cancelled:
            self.log("已停止：没有导出任何阵容。")
            self.log(f"续跑：PcrTimelineTester resume \"{self.run_dir}\"")
            return CANCELLED
        export = summary.get("export") or {}
        team = tuple(int(unit) for unit in (export.get("team") or self.job.best_team or ()))
        tuned = save_with_team(self.save, team) if team else self.save
        workbook = self.run_dir / self.workbook_name
        write_tuned_save(
            workbook, tuned, self.save.frames,
            battle_seed=self.settings.seed,
            damage=int(summary.get("final") or 0),
            summary=roster_workbook_summary(summary, names=self.names),
            sheet_name="阵容结果",
        )
        summary["result_workbook"] = str(workbook)
        self.workbook = workbook
        write_json(self.run_dir / REPORT_JSON, summary)
        write_roster_report(self.run_dir / REPORT_MD, summary, names=self.names)
        self.log(f"结果轴 {workbook}")
        self.log(f"报告 {self.run_dir / REPORT_MD}")
        self.log(f"日志 {self.run_dir / LOG}")
        return OK


    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        self.job.close()
