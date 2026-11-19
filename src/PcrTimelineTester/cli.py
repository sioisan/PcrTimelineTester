"""Command line entry point: inspect / probe / tune / resume.

    PcrTimelineTester                      # 交互式向导
    PcrTimelineTester inspect 轴.xlsx      # 只读：看轴内容和可调项
    PcrTimelineTester probe 轴.xlsx        # 打一场基线，验证接口连通
    PcrTimelineTester tune 轴.xlsx         # 正式调帧，结果写进 runs/<时间>_<轴名>/
    PcrTimelineTester tune 轴.xlsx --mode ga   # 语义 UB 遗传算法优化
    PcrTimelineTester resume runs/<目录>    # 中断后续跑
"""

from __future__ import annotations

import argparse
from dataclasses import fields, replace
from enum import Enum
from pathlib import Path
from typing import Any
import json
import os
from pathlib import Path
import signal
import sys

from .client import DEFAULT_CALC_STATS, DEFAULT_ENDPOINT, SimulatorClient, TuneError, result_name
from .frames import build_targets, text
from .ga.settings import GaSettings
from .job import JobSettings, TuneJob
from . import modes as mode_registry
from .modes import FrameSettings, ModeSpec
from .report import (
    CONFIG, LOG, STATE, RunLog, read_json, stderr, timestamp_dir,
)
from .savefile import SaveFile, read_save
from .search import SearchSettings, ThresholdMode
from .session import GaSession, TuneSession

MODES = mode_registry.keys()
_MODE_HELP = "优化模式：" + " / ".join(f"{spec.key}（{spec.label}）" for spec in mode_registry.modes())


# --------------------------------------------------------------------- helpers


def _path(value: str | Path) -> Path:
    if isinstance(value, Path):
        return value.expanduser().resolve()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return Path(value).expanduser().resolve()


def _ask(label: str, default: str | int | float) -> str:
    return input(f"{label} [{default}] ").strip() or str(default)


def _number(label: str, default: int, low: int, high: int) -> int:
    while True:
        try:
            value = int(_ask(label, default))
            if low <= value <= high:
                return value
        except ValueError:
            pass
        print(f"请输入 {low} 到 {high} 之间的整数。")


def _choose(label: str, paths: list[Path]) -> Path:
    choices = paths[:40]
    for index, path in enumerate(choices, 1):
        print(f"  {index}. {path.name}")
    while True:
        raw = input(f"{label}，输入编号或路径 ").strip()
        if raw.isdecimal() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        if raw:
            return _path(raw)
        print("请输入编号或路径。")


def _items(value: str | None) -> frozenset[tuple[int, int]] | None:
    if not value:
        return None
    pairs: set[tuple[int, int]] = set()
    for part in value.replace("，", ",").split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            slot, ub = (int(item) for item in piece.replace("：", ":").split(":"))
        except ValueError as exc:
            raise ValueError(f"项格式应为 角色序号:第几刀，例如 1:2，实际收到 {piece!r}") from exc
        if slot < 1 or ub < 1:
            raise ValueError(f"项编号必须从 1 开始：{piece}")
        pairs.add((slot, ub))
    return frozenset(pairs) or None


def _config_data(path: Path | None) -> dict:
    if path is None:
        return {}
    data = read_json(path)
    return data


def _search_settings(args: argparse.Namespace) -> SearchSettings:
    config = _config_data(getattr(args, "config", None))
    raw = config.get("search") if isinstance(config.get("search"), dict) else config
    settings = SearchSettings.from_dict(raw or {})
    overrides: dict = {}
    for name in ("step", "max_offset", "outer_probes", "frame_min", "frame_max", "max_battles", "chunk"):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    if getattr(args, "threshold", None) is not None:
        overrides["threshold_value"] = float(args.threshold)
    if getattr(args, "threshold_mode", None):
        overrides["threshold_mode"] = ThresholdMode.parse(args.threshold_mode)
    if getattr(args, "peak_tolerance", None) is not None:
        overrides["peak_tolerance"] = float(args.peak_tolerance)
    if getattr(args, "no_memoize", False):
        overrides["memoize"] = False
    return replace(settings, **overrides)


def _job_settings(args: argparse.Namespace, save: SaveFile) -> JobSettings:
    config = _config_data(getattr(args, "config", None))
    raw = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    settings = JobSettings.from_dict(raw)
    battle_time = getattr(args, "battle_time", None)
    if battle_time is None:
        battle_time = save.suggested_duration(save.header, save.path.stem, save.path.name)
    if not 1 <= int(battle_time) <= 90:
        raise ValueError("战斗秒数必须在 1~90 之间")
    seed = getattr(args, "seed", None)
    if seed is None:
        seed = save.battle_seed if save.battle_seed is not None else settings.seed
    values: dict = {"battle_time": int(battle_time), "seed": int(seed)}
    endpoint = getattr(args, "endpoint", None)
    if endpoint:
        values["endpoint"] = endpoint
    for name in ("samples", "batch_size", "calc_stats", "max_items", "passes"):
        value = getattr(args, name, None)
        if value is not None:
            values[name] = int(value)
    if getattr(args, "timeout", None) is not None:
        values["timeout"] = float(args.timeout)
    if getattr(args, "include_boss", False):
        values["include_boss"] = True
    if getattr(args, "no_verify", False):
        values["verify"] = False
    items = _items(getattr(args, "items", None))
    if items:
        values["items"] = items
    settings = replace(settings, **values)
    if not 1 <= settings.samples <= 100:
        raise ValueError("每个候选的种子数必须在 1~100 之间")
    if settings.batch_size < 1:
        raise ValueError("单批战斗数必须 ≥ 1")
    if settings.timeout <= 0:
        raise ValueError("超时秒数必须为正数")
    if not 1 <= settings.passes <= 20:
        raise ValueError("循环轮数必须在 1~20 之间")
    return settings


def _print_timeline(save: SaveFile, settings: JobSettings, search_settings: SearchSettings) -> None:
    targets = build_targets(
        save.frames, save.unit_ids, names=save.names, boss_name=save.boss_name,
        include_boss=settings.include_boss, items=settings.items, max_items=settings.max_items,
    )
    print(f"轴文件 {save.path.name}（{save.header}）")
    print(f"角色 {'、'.join(save.unit_names)} / Boss {save.boss_label}")
    print(f"战斗 {settings.battle_time} 秒 / 种子 {settings.seed}"
          f"{'（每个候选 ' + str(settings.samples) + ' 个种子取平均）' if settings.samples > 1 else ''}")
    rounds = f"；循环 {settings.passes} 轮" if settings.passes > 1 else ""
    print(f"可调项 {len(targets)} 项；{search_settings.describe()}{rounds}")
    for position, target in enumerate(targets, 1):
        print(f"  {position:>2}. {target.label} @ {target.raw_text}")


def _ga_settings(args: argparse.Namespace, save: SaveFile) -> GaSettings:
    """语义 GA 参数：配置文件里的 ga 段 + 命令行覆盖。"""
    config = _config_data(getattr(args, "config", None))
    raw = config.get("ga") if isinstance(config.get("ga"), dict) else {}
    settings = GaSettings.from_dict(raw)
    battle_time = getattr(args, "battle_time", None)
    if battle_time is None:
        battle_time = save.suggested_duration(save.header, save.path.stem, save.path.name)
    seed = getattr(args, "seed", None)
    if seed is None:
        seed = save.battle_seed if save.battle_seed is not None else settings.seed
    values: dict = {"battle_time": int(battle_time), "seed": int(seed)}
    endpoint = getattr(args, "endpoint", None)
    if endpoint:
        values["endpoint"] = endpoint
    for name in ("samples", "batch_size", "population", "generations", "islands",
                 "migration_interval", "max_lane_length", "ga_seed", "delay",
                 "ub_type", "skill_type", "sequence_type"):
        value = getattr(args, name, None)
        if value is not None:
            values[name] = int(value)
    for name in ("timeout", "crossover", "mutation", "migrants_ratio", "fingerprint_distance"):
        value = getattr(args, name, None)
        if value is not None:
            values[name] = float(value)
    if getattr(args, "legacy_ub", False):
        values["legacy_ub"] = True
    if getattr(args, "no_own_skill", False):
        values["own_skill"] = False
    if getattr(args, "other_skill", False):
        values["other_skill"] = True
    if getattr(args, "no_verify", False):
        values["verify"] = False
    return replace(settings, **values)


def _print_ga_plan(save: SaveFile, settings: GaSettings) -> None:
    profile = settings.conversion_profile
    print(f"轴文件 {save.path.name}（{save.header}）")
    print(f"角色 {'、'.join(save.unit_names)} / Boss {save.boss_label}")
    print(f"优化模式 语义 GA；战斗 {settings.battle_time} 秒 / 种子 {settings.seed}"
          f"{'（每个候选 ' + str(settings.samples) + ' 个种子取平均）' if settings.samples > 1 else ''}")
    print(f"转换口径 UB类型 {int(profile.ub_type)} / 技能类型 {int(profile.skill_type)} / 序列类型 {int(profile.sequence_type)}")
    print(f"种群 {settings.population} × {settings.generations} 代（岛屿 {settings.islands}，迁移每 {settings.migration_interval} 代）")
    if save.has_semantic:
        print("起点 现有语义轴（先还原成帧再重新转换）")
    else:
        print("起点 现有帧轴（先转换成语义轴）")


# ------------------------------------------------------------------- commands


def _install_cancel_handler(job: object, log: RunLog, note: str = "不会写回任何帧"):
    """First Ctrl+C = graceful stop (nothing is written), second = hard exit."""
    counter = {"hits": 0}
    try:
        previous = signal.getsignal(signal.SIGINT)
    except (ValueError, AttributeError):
        return None

    def stop() -> None:
        for name in ("cancel_run", "cancel"):
            method = getattr(job, name, None)
            if callable(method):
                method()
                return

    def handler(signum, frame):  # noqa: ANN001, ARG001
        counter["hits"] += 1
        if counter["hits"] == 1:
            log(f"收到中断：正在收尾，{note}；再按一次 Ctrl+C 强制退出。")
            stop()
        else:
            log("强制退出。")
            os._exit(130)

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        return None
    return previous


def _run_tune(
    save: SaveFile,
    job_settings: JobSettings,
    search_settings: SearchSettings,
    run_dir: Path,
    *,
    resume_state: dict | None = None,
) -> int:
    spec = mode_registry.get(mode_registry.FRAME)
    return _run_session(spec, save, FrameSettings(job_settings, search_settings), run_dir,
                        resume_state=resume_state)


def _run_session(
    spec: ModeSpec,
    save: SaveFile,
    settings: object,
    run_dir: Path,
    *,
    resume_state: dict | None = None,
    note: str = "不会写回任何帧",
) -> int:
    """跑一个会话：模式只提供 session 工厂，收尾逻辑共用。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = RunLog(run_dir / LOG)
    session = spec.open_session(save, settings, run_dir, log=log, resume_state=resume_state)
    previous = _install_cancel_handler(session.job, log, note)
    try:
        return session.run()
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass
        session.close()
        log.close()


def _run_tune(
    save: SaveFile,
    job_settings: JobSettings,
    search_settings: SearchSettings,
    run_dir: Path,
    *,
    resume_state: dict | None = None,
) -> int:
    spec = mode_registry.get(mode_registry.FRAME)
    return _run_session(spec, save, FrameSettings(job_settings, search_settings), run_dir,
                        resume_state=resume_state)


def _run_ga(
    save: SaveFile,
    ga_settings: GaSettings,
    run_dir: Path,
    *,
    resume_state: dict | None = None,
) -> int:
    spec = mode_registry.get(mode_registry.GA)
    return _run_session(spec, save, ga_settings, run_dir,
                        resume_state=resume_state, note="不会导出结果轴")


def _command_inspect(args: argparse.Namespace) -> int:
    mode = getattr(args, "mode", "frame") or "frame"
    save = mode_registry.get(mode).read_save(_path(args.excel))
    if mode == mode_registry.CYCLE:
        spec = mode_registry.get(mode)
        settings = spec.from_values({**_arg_overrides(spec, args), **_overrides(args)}, save)
        print(f"轴文件 {save.path.name}，队伍 {'、'.join(save.unit_names)}")
        for line in spec.plan(save, settings):
            print(line)
        return 0
    if mode == "ga":
        _print_ga_plan(save, _ga_settings(args, save))
        return 0
    _print_timeline(save, _job_settings(args, save), _search_settings(args))
    return 0


def _command_probe(args: argparse.Namespace) -> int:
    mode = getattr(args, "mode", "frame") or "frame"
    save = mode_registry.get(mode).read_save(_path(args.excel))
    if mode == "ga":
        return _probe_ga(save, _ga_settings(args, save))
    settings = _job_settings(args, save)
    print(f"接口 {settings.endpoint}，战斗 {settings.battle_time} 秒，种子 {settings.seed}")
    client = SimulatorClient(
        context=save.request_context(battle_time=settings.battle_time, battle_seed=settings.seed),
        endpoint=settings.endpoint, seeds=settings.seeds(), timeout=settings.timeout,
        batch_size=settings.batch_size, calc_stats=settings.calc_stats,
    )
    try:
        outcome = client.evaluate([save.frames_copy()])[0]
    finally:
        client.close()
    expected = f"{outcome.expected:,.1f}" if outcome.expected is not None else "（摸轴器没有返回期望伤害）"
    print(f"接口正常：期望伤害 {expected} / 实战伤害 {outcome.damage:,} × 战斗帧 {outcome.battle_frames} / "
          f"结果 {result_name(outcome.result)} / 种子 {outcome.seed} / CalcStats {client.calc_stats}")
    return 0


def _probe_ga(save: SaveFile, settings: GaSettings) -> int:
    """语义模式探活：把轴转成语义轴再打一场。"""
    from .ga.evaluator import GaEvaluator

    print(f"接口 {settings.endpoint}，战斗 {settings.battle_time} 秒，种子 {settings.seed}")
    evaluator = GaEvaluator(
        context=save.request_context(battle_time=settings.battle_time, battle_seed=settings.seed),
        endpoint=settings.endpoint, battle_time=settings.battle_time, battle_seed=settings.seed,
        profile=settings.conversion_profile, seeds=settings.seeds(),
        batch_size=settings.batch_size, timeout=settings.timeout, unit_ids=save.unit_ids,
    )
    try:
        axis = evaluator.initial_axis(frames=save.frames, lanes=save.semantic, roles=save.sequence)
        outcome, frames = evaluator.replay(axis.lanes, axis.sequence)
        print(f"语义轴：{' / '.join('、'.join(str(gene) for gene in lane) for lane in axis.lanes)}")
        print(f"还原成帧：{len(frames)} 行，第一行 {frames[0] if frames else '—'}")
        print(f"接口正常：期望伤害 {outcome.mean_damage:,} × 战斗帧 {outcome.frames} / 结果 {result_name(outcome.result)}")
    finally:
        evaluator.close()
    return 0


def _mode_of(args: argparse.Namespace, config: dict | None = None) -> str:
    key = getattr(args, "mode", None) or (config or {}).get("mode") or mode_registry.FRAME
    mode_registry.get(key)  # 不认识的模式在这里就报错
    return key


def _arg_overrides(spec: ModeSpec, args: argparse.Namespace) -> dict:
    """命令行里和模式参数同名的选项（--battle-time 之类）也算覆盖值。"""
    data: dict[str, Any] = {}
    for param in spec.params:
        value = getattr(args, param.name, None)
        if value is not None:
            data[param.name] = value
    return data


def _overrides(args: argparse.Namespace) -> dict:
    """``--set 名字=值``：给任意模式（包括以后新接的）传参数。"""
    data: dict[str, Any] = {}
    for item in getattr(args, "set_values", None) or ():
        if "=" not in item:
            raise ValueError(f"--set 需要 名字=值 的形式（现在是 {item!r}）")
        name, value = item.split("=", 1)
        data[name.strip()] = value.strip()
    return data


def _apply_overrides(spec: ModeSpec, settings: object, overrides: dict) -> object:
    """把 ``--set`` 的值套到已有设置对象上。

    值先按模式声明的 ``Param`` 强转（命令行传进来的都是字符串），再只套到
    这个对象自己有的字段上——同一个 ``--set`` 往往会同时落到 job 与 search 两个
    对象，缺哪个字段不算错，整模式都不认识的字段才报错。
    """
    if not overrides:
        return settings
    declared = {param.name: param for param in spec.params}
    names = {item.name for item in fields(settings)}
    unknown = sorted(set(overrides) - names - set(declared))
    if unknown:
        raise ValueError(f"{spec.label} 模式没有这些参数：{'、'.join(unknown)}")
    values: dict[str, Any] = {}
    for name, value in overrides.items():
        if name not in names:
            continue
        param = declared.get(name)
        coerced = param.coerce(value) if param is not None else value
        current = getattr(settings, name)
        # 枚举字段：Param 里存的是线上值（字符串或整数），这里还原成枚举本身。
        if isinstance(current, Enum) and not isinstance(coerced, Enum):
            coerced = type(current)(coerced)
        values[name] = coerced
    return replace(settings, **values) if values else settings


def _command_tune(args: argparse.Namespace) -> int:
    spec = mode_registry.get(_mode_of(args))
    mode = spec.key
    save = spec.read_save(_path(args.excel))
    root = Path(args.out) if args.out else Path.cwd() / "runs"
    run_dir = timestamp_dir(root, save.path.stem)
    overrides = _overrides(args)
    if mode == mode_registry.GA:
        settings = _apply_overrides(spec, _ga_settings(args, save), overrides)
        _print_ga_plan(save, settings)
        print(f"运行目录 {run_dir}")
        return _run_ga(save, settings, run_dir)
    if mode == mode_registry.FRAME:
        job_settings = _apply_overrides(spec, _job_settings(args, save), overrides)
        search_settings = _apply_overrides(spec, _search_settings(args), overrides)
        _print_timeline(save, job_settings, search_settings)
        print(f"运行目录 {run_dir}")
        return _run_tune(save, job_settings, search_settings, run_dir)
    # 其它（以后新接的）模式：参数全部由模式自己声明，这里只按名字传值。
    values = {**spec.defaults(), **_arg_overrides(spec, args), **overrides}
    if mode == mode_registry.CYCLE:
        common = _job_settings(args, save)
        for name in ("seed", "battle_time", "samples", "timeout", "batch_size"):
            if name not in overrides:
                values[name] = getattr(common, name)
    settings = spec.from_values(values, save)
    endpoint = getattr(args, "endpoint", None)
    if endpoint and hasattr(settings, "endpoint"):
        settings = replace(settings, endpoint=endpoint)
    for line in spec.plan(save, settings):
        print(line)
    print(f"运行目录 {run_dir}")
    return _run_session(spec, save, settings, run_dir, note="不会导出任何结果")


def _command_resume(args: argparse.Namespace) -> int:
    run_dir = _path(args.folder)
    if not (run_dir / CONFIG).is_file():
        raise ValueError(f"{run_dir} 不是运行目录（缺少 {CONFIG}）")
    config = read_json(run_dir / CONFIG)
    excel = Path(str(config.get("excel") or ""))
    if not excel.is_file():
        raise ValueError(f"运行目录里记录的轴文件不存在：{excel}")
    spec = mode_registry.get(_mode_of(args, config))
    mode = spec.key
    save = spec.read_save(excel)
    state = read_json(run_dir / STATE) if (run_dir / STATE).is_file() else None
    print(f"续跑 {run_dir}，轴 {excel.name}，模式 {spec.label}")
    if mode == mode_registry.GA:
        settings = GaSettings.from_dict(config.get("settings") or {})
        return _run_ga(save, settings, run_dir, resume_state=state)
    if mode == mode_registry.FRAME:
        job_settings = JobSettings.from_dict(config.get("settings") or {})
        search_settings = SearchSettings.from_dict(config.get("search") or {})
        return _run_tune(save, job_settings, search_settings, run_dir, resume_state=state)
    saved = dict(config.get("settings") or {})
    endpoint = saved.pop("endpoint", None) if mode == mode_registry.CYCLE else None
    settings = spec.from_values(saved, save)
    if endpoint:
        settings = replace(settings, endpoint=getattr(args, "endpoint", None) or endpoint)
    return _run_session(spec, save, settings, run_dir, resume_state=state, note="不会导出任何结果")


def _command_modes(args: argparse.Namespace) -> int:
    for spec in mode_registry.modes():
        need = "需要帧轴" if spec.needs_frames else "也可只给语义轴"
        print(f"{spec.key}　{spec.label}　（{need}，结果表 {spec.workbook_name}）")
        print(f"  {spec.description}")
        for param in spec.params:
            print(f"  --set {param.name}={param.default!r}".replace("'", "")
                  + f"　{param.label}：{param.tip}")
        print()
    return 0


def _interactive() -> int:
    print("PCR 调帧（独立版，走 PCRCalculator 接口）")
    mode_raw = _ask("优化模式 1=逐帧搜索（帧） 2=语义 UB 遗传算法（ga）", "1")
    mode = "ga" if mode_raw.strip().lower() in ("2", "ga", "语义", "遗传") else "frame"
    workbooks = sorted(
        path for path in Path.cwd().glob("*.xlsx") if not path.name.startswith("~$")
    )
    if workbooks:
        print(f"当前目录有 {len(workbooks)} 个 .xlsx")
    excel = _choose("选择待调帧的轴", workbooks) if workbooks else _path(_ask("轴文件路径", ""))
    save = mode_registry.get(mode).read_save(excel)
    if mode == "ga":
        ga_defaults = _ga_settings(argparse.Namespace(), save)
        print()
        print(f"轴 {save.path.name}（{save.header}）")
        print(f"角色 {'、'.join(save.unit_names)} / Boss {save.boss_label} / 建议 {ga_defaults.battle_time} 秒")
        settings = replace(
            ga_defaults,
            endpoint=_ask("接口地址", ga_defaults.endpoint),
            battle_time=_number("战斗秒数", ga_defaults.battle_time, 1, 90),
            seed=_number("战斗随机种子", ga_defaults.seed, -(2 ** 31), 2 ** 31 - 1),
            samples=_number("每个候选的种子数（1 = 最快）", ga_defaults.samples, 1, 100),
            population=_number("种群大小", ga_defaults.population, 2, 500),
            generations=_number("迭代代数", ga_defaults.generations, 1, 10000),
        )
        print()
        _print_ga_plan(save, settings)
        if _ask("开始优化？y/n", "n").lower() not in ("y", "yes"):
            return 0
        run_dir = timestamp_dir(Path.cwd() / "runs", save.path.stem)
        print(f"运行目录 {run_dir}")
        return _run_ga(save, settings, run_dir)
    defaults = _job_settings(argparse.Namespace(), save)
    print()
    print(f"轴 {save.path.name}（{save.header}）")
    print(f"角色 {'、'.join(save.unit_names)} / Boss {save.boss_label} / 建议 {defaults.battle_time} 秒")
    settings = replace(
        defaults,
        endpoint=_ask("接口地址", defaults.endpoint),
        battle_time=_number("战斗秒数", defaults.battle_time, 1, 90),
        seed=_number("战斗随机种子", defaults.seed, -(2 ** 31), 2 ** 31 - 1),
        samples=_number("每个候选的种子数（1 = 最快）", defaults.samples, 1, 100),
        passes=_number("循环轮数（每轮用上一轮最优重跑，1 = 只跑一遍）", defaults.passes, 1, 20),
    )
    search_settings = replace(
        SearchSettings(),
        step=_number("帧步长", 5, 1, 60),
        max_offset=_number("最大帧偏移（上下各）", 100, 1, 2000),
        outer_probes=_number("外探次数", 3, 0, 50),
    )
    targets = build_targets(
        save.frames, save.unit_ids, names=save.names, boss_name=save.boss_name,
        include_boss=settings.include_boss, items=settings.items, max_items=settings.max_items,
    )
    print()
    print(f"将测 {len(targets)} 项；{search_settings.describe()}")
    print(f"接口 {settings.endpoint}（{settings.battle_time} 秒，种子 {settings.seed}）")
    if _ask("开始调帧？y/n", "n").lower() not in ("y", "yes"):
        return 0
    run_dir = timestamp_dir(Path.cwd() / "runs", save.path.stem)
    print(f"运行目录 {run_dir}")
    return _run_tune(save, settings, search_settings, run_dir)


# --------------------------------------------------------------------- parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", help=f"接口地址（默认 {DEFAULT_ENDPOINT}）")
    parser.add_argument("--battle-time", type=int, help="战斗秒数（默认取轴名里的尾刀秒数，否则 90）")
    parser.add_argument("--seed", type=int, help="战斗随机种子（默认取轴里的 RandomSeed，否则 666）")
    parser.add_argument("--samples", type=int, help="每个候选模拟几个种子取平均伤害（默认 1）")
    parser.add_argument("--passes", type=int, help="循环轮数：每轮都用上一轮的最优帧当起点重跑（默认 1）")
    parser.add_argument("--batch-size", type=int, help="单次请求携带的战斗数（默认 16）")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数（默认 180）")
    parser.add_argument("--calc-stats", type=int, help=f"CalcStats 位掩码（默认 {DEFAULT_CALC_STATS}）")
    parser.add_argument("--include-boss", action="store_true", help="连 Boss 那一行一起调")
    parser.add_argument("--items", help="只测指定项，例如 1:2,3:1（第几个角色:第几刀，从 1 开始）")
    parser.add_argument("--max-items", type=int, help="只测前 N 项")
    parser.add_argument("--config", type=_path, help="JSON 参数文件（可含 settings / search 两段）")
    parser.add_argument(
        "--set", dest="set_values", action="append", metavar="名字=值",
        help="按名字给优化模式传参数，可重复；PcrTimelineTester modes 能列出每个模式的参数名",
    )


def _add_search(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--step", type=int, help="帧步长（默认 5）")
    parser.add_argument("--max-offset", type=int, help="最大帧偏移，上下各（默认 100）")
    parser.add_argument("--outer-probes", type=int, help="超过阈值后的外探次数（默认 3，可调）")
    parser.add_argument("--threshold", type=float, help="允许掉幅的阈值（默认 500000 伤害）")
    parser.add_argument("--threshold-mode", choices=[mode.value for mode in ThresholdMode] + [mode.label for mode in ThresholdMode],
                        help="阈值口径：绝对伤害 / 百分比（0~1）/ 不允许掉伤害（也接受 absolute/percent/strict）")
    parser.add_argument("--frame-min", type=int, help="帧下限（默认 0）")
    parser.add_argument("--frame-max", type=int, help="帧上限（默认 5399）")
    parser.add_argument("--peak-tolerance", type=float, help="同伤害容差（默认 0.5，取更早的帧）")
    parser.add_argument("--max-battles", type=int, help="单项场次上限（默认 0 = 不限制）")
    parser.add_argument("--chunk", type=int, help="每批探针帧数（默认 8）")
    parser.add_argument("--no-memoize", action="store_true", help="同一帧重复采样（默认去重）")


def _add_ga(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--population", type=int, help="GA 种群大小（默认 48）")
    parser.add_argument("--generations", type=int, help="GA 迭代代数（默认 30）")
    parser.add_argument("--islands", type=int, help="GA 岛屿数（默认 3）")
    parser.add_argument("--crossover", type=float, help="交叉概率（默认 0.75）")
    parser.add_argument("--mutation", type=float, help="变异概率（默认 0.22）")
    parser.add_argument("--migration-interval", type=int, help="每隔几代迁移一次（默认 10）")
    parser.add_argument("--migrants-ratio", type=float, help="迁移比例（默认 0.08）")
    parser.add_argument("--disaster-patience", type=int, help="多少代没有进步就灾变（默认 20）")
    parser.add_argument("--max-lane-length", type=int, help="每条 UB 序列最大长度（默认 96）")
    parser.add_argument("--fingerprint-distance", type=float, help="行为去重阈值（默认 1.0，0 表示关闭）")
    parser.add_argument("--ga-seed", type=int, help="GA 随机种子（默认 42，可复现）")
    parser.add_argument("--ub-type", type=int, help="语义 UB 类型（默认 0 = 单发）")
    parser.add_argument("--skill-type", type=int, help="语义技能类型（默认 0 = 按帧）")
    parser.add_argument("--sequence-type", type=int, help="语义序列类型（0 = 并发，1 = 队列）")
    parser.add_argument("--legacy-ub", action="store_true", help="基因池里加入旧版 UB 编码")
    parser.add_argument("--no-own-skill", action="store_true", help="基因池里去掉自己的技能 UB")
    parser.add_argument("--other-skill", action="store_true", help="基因池里加入别人的技能 UB")
    parser.add_argument("--delay", type=int, help="退避帧数的基因池上限（默认 3）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="PcrTimelineTester", description="PcrTimelineTester：逐帧搜索 / 语义 GA 调轴（命令行）")
    commands = parser.add_subparsers(dest="command")

    inspect = commands.add_parser("inspect", help="只读：查看轴内容与可调项")
    inspect.add_argument("excel", type=_path)
    inspect.add_argument("--mode", choices=MODES, help=_MODE_HELP)
    _add_common(inspect)
    _add_search(inspect)
    _add_ga(inspect)

    probe = commands.add_parser("probe", help="打一场基线战斗，验证接口连通")
    probe.add_argument("excel", type=_path)
    probe.add_argument("--mode", choices=MODES, help=_MODE_HELP)
    _add_common(probe)
    _add_ga(probe)

    tune = commands.add_parser("tune", help="正式优化并写回结果轴")
    tune.add_argument("excel", type=_path)
    tune.add_argument("--mode", choices=MODES, help=_MODE_HELP)
    tune.add_argument("--out", type=_path, help="运行目录根（默认 ./runs）")
    tune.add_argument("--no-verify", action="store_true", help="结束时不复核最终伤害")
    _add_common(tune)
    _add_search(tune)
    _add_ga(tune)

    resume = commands.add_parser("resume", help="从中断的运行目录继续")
    resume.add_argument("folder", type=_path)
    resume.add_argument("--mode", choices=MODES, help="覆盖运行目录里记录的优化模式")

    commands.add_parser("modes", help="列出所有优化模式与它们的参数名")

    gui = commands.add_parser("gui", help="打开图形界面")
    gui.add_argument("excel", nargs="?", type=_path, help="可选：启动时直接打开这条轴")
    return parser


def _configure_output() -> None:
    """Always speak UTF-8, so redirected logs keep the Chinese text intact."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = _parser().parse_args(argv)
    try:
        if args.command is None:
            return _interactive()
        if args.command == "inspect":
            return _command_inspect(args)
        if args.command == "probe":
            return _command_probe(args)
        if args.command == "tune":
            return _command_tune(args)
        if args.command == "resume":
            return _command_resume(args)
        if args.command == "modes":
            return _command_modes(args)
        if args.command == "gui":
            from .gui import main as gui_main

            return gui_main([str(args.excel)] if args.excel else [])
        return 0
    except (EOFError, KeyboardInterrupt):
        stderr("\n已中断。")
        return 130
    except (ValueError, TuneError, OSError) as exc:
        stderr(f"错误 {exc}")
        return 1
    except json.JSONDecodeError as exc:
        stderr(f"错误 JSON 解析失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
