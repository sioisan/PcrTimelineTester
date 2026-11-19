"""Tkinter front end: pick an axis, poke the parameters, watch the search.

Only the standard library is involved (``tkinter`` ships with CPython on
Windows), so the GUI adds no dependency to the tool.  Everything it does goes
through :class:`PcrTimelineTester.session.TuneSession`, i.e. the same job, the same
files, the same "write the axis once at the end" rule as the console.

Threading rule: the session runs in a worker thread, and *only* the main thread
touches widgets.  The worker talks to the UI through a ``queue.Queue`` that a
``after()`` pump drains every 120 ms (worker enqueues, a timer pumps); widgets
are only ever touched from the main thread.

The server is addressed by "where PCRCalculator lives" + "which port", not by URL:
starting a run launches ``PCRCalculatorServer.exe`` on that port when nothing is
listening there yet, and stops only the process this window started.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import faulthandler
import math
import os
from pathlib import Path
import queue
import sys
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import modes as mode_registry
from .client import DEFAULT_CALC_STATS
from .character_grid import CharacterGrid
from .frames import build_targets, text
from .ga.semantics import (
    SemanSequenceType, SemanSkillType, SemanUBType,
)
from .ga.settings import GaSettings
from .job import DECISION_TEXT, JobSettings
from .modes import FrameSettings
from .report import CONFIG, STATE, read_json, timestamp_dir
from .savefile import SaveFile, read_save
from .search import SearchSettings, ThresholdMode
from .server import DEFAULT_PORT, HostedServer, endpoint_for, find_server
from .session import CANCELLED, GaSession, TuneSession, OK
from .settings import read_settings, settings_path, write_settings


def _attach_std_streams() -> None:
    """给 windowed 冻结版补上 stdout/stderr。

    PyInstaller 的 ``--windowed`` 下 ``sys.stdout``/``sys.stderr`` 都是 ``None``，
    只要有任何一句 ``print`` 就会抛 ``AttributeError: 'NoneType' object has no attribute 'write'``，
    而窗口模式下这个异常连提示都看不到（进程直接消失）。所以启动第一件事就是兜住它们。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
            except OSError:
                pass


def crash_log_path() -> Path:
    return settings_path().parent / "crash.log"


def _install_crash_handler() -> Path:
    """任何没被捕获的异常都写进 crash.log；窗口还在就顺手写到界面日志。"""
    path = crash_log_path()

    def report(exc_type, exc, tb) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
                handle.writelines(traceback.format_exception(exc_type, exc, tb))
        except OSError:
            pass
        app = globals().get("_LAST_APP")
        if app is not None:
            try:
                app._report_error(
                    f"程序出错了：{exc_type.__name__}: {exc}（详细信息见 {path}）"
                )
            except Exception:  # 连日志都写不了就别再抛了
                pass

    sys.excepthook = report
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        faulthandler.enable(file=path.open("a", encoding="utf-8"), all_threads=True)
    except (OSError, ValueError):
        pass
    return path


PUMP_MS = 120


def _icon_dir_text() -> str:
    """图标缓存目录（给人看的那种短路径）。"""
    from .datas import icon_dir

    return str(icon_dir())
CHECK_ON, CHECK_OFF = "√", "□"
TITLE = "PcrTimelineTester"
GEOMETRY_HINT = (1040, 620)

# 工作区、面板和输入区使用不同明度，口径相同的颜色也用于非 ttk 控件。
@dataclasses.dataclass(frozen=True)
class Palette:
    workspace: str
    panel: str
    field: str
    header: str
    text: str
    muted: str
    edge: str
    violet: str
    selection: str
    cyan: str
    tab: str
    disabled: str
    button: str
    hover: str
    pressed: str
    heading: str
    alternate: str
    thumb: str
    check_empty: str
    primary: str
    primary_ink: str

DARK = Palette(
    workspace="#0e0f1c", panel="#1b1b30", field="#121323",
    header="#24203d", text="#f2f1fb", muted="#b5b4cb",
    edge="#45415d", violet="#c0a2ff", selection="#493465",
    cyan="#77e3d8", tab="#342847", disabled="#9591a7",
    button="#302d45", hover="#413656", pressed="#46325d",
    heading="#302a43", alternate="#212035", thumb="#565170",
    check_empty="#1b1b30", primary="#bc91ff", primary_ink="#160c22",
)

LIGHT = Palette(
    workspace="#eceef5", panel="#fafaff", field="#ffffff",
    header="#e5e0f4", text="#232239", muted="#595970",
    edge="#b9b7cb", violet="#6639a7", selection="#e2d5f9",
    cyan="#147774", tab="#ede4fb", disabled="#6f6c7d",
    button="#f0edf8", hover="#e4dcf2", pressed="#d9ccea",
    heading="#e7e4ef", alternate="#f0eff7", thumb="#a29bb9",
    check_empty="#fafaff", primary="#7145ad", primary_ink="#ffffff",
)

MODES = mode_registry.labels()

UB_TYPE_LABELS = {
    SemanUBType.SERIES_1: "单段 UB（SERIES_1）",
    SemanUBType.SERIES_4: "四段 UB（SERIES_4）",
}
SKILL_TYPE_LABELS = {
    SemanSkillType.FRAME: "按帧（FRAME）",
    SemanSkillType.SERIES_1: "SERIES_1",
    SemanSkillType.SERIES_2: "SERIES_2",
    SemanSkillType.SERIES_3: "SERIES_3",
    SemanSkillType.SERIES_5: "SERIES_5",
    SemanSkillType.SERIES_10: "SERIES_10",
    SemanSkillType.SERIES_20: "SERIES_20",
    SemanSkillType.SERIES_30: "SERIES_30",
}
SEQUENCE_TYPE_LABELS = {
    SemanSequenceType.LANES: "只调五条 UB 序列（LANES）",
    SemanSequenceType.SEQUENCE: "同时调角色序列（SEQUENCE）",
}


def _label_value(labels: dict, label: str):
    for value, text_ in labels.items():
        if text_ == label:
            return value
    return next(iter(labels))


def _label_of(labels: dict, value) -> str:
    return labels.get(value) or next(iter(labels.values()))


def _is_iterative_job(job) -> bool:
    """按迭代/代走进度的那几种任务（GA / 退火 / 阵容搜索）。"""
    return type(job).__name__ in ("AnnealJob", "RosterJob")


def _is_anneal_job(job) -> bool:
    """退火任务按「次迭代」报进度，语义 GA 按「代」。"""
    return job is not None and type(job).__name__ == "AnnealJob"


def _step_suffix(job) -> str:
    return "次迭代" if (type(job).__name__ in ("AnnealJob", "RosterJob")) else "代"


def _open_path(path: Path) -> None:
    """Open a file or folder with the system handler (Windows explorer)."""
    if hasattr(os, "startfile"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:  # pragma: no cover - only on non-Windows
        import subprocess
        import sys

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)])


class Tooltip:
    """Hover bubble for one widget (the tool has no other documentation)."""

    DELAY_MS = 420
    WRAP = 340

    def __init__(self, widget: tk.Widget, message: str) -> None:
        self.widget = widget
        self.message = message
        self.tip: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        try:
            self._after = self.widget.after(self.DELAY_MS, self._show)
        except tk.TclError:
            self._after = None

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        if self.tip is not None or not self.message:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        label = ttk.Label(
            tip, text=self.message, justify="left", style="HeaderInfo.TLabel",
            relief="solid", borderwidth=1, wraplength=self.WRAP, padding=(7, 5),
        )
        label.pack()
        tip.update_idletasks()
        width, height = tip.winfo_reqwidth(), tip.winfo_reqheight()
        screen_w, screen_h = tip.winfo_screenwidth(), tip.winfo_screenheight()
        x = max(0, min(x, screen_w - width - 8))
        if y + height > screen_h - 8:
            y = max(0, self.widget.winfo_rooty() - height - 6)
        tip.wm_geometry(f"+{x}+{y}")
        self.tip = tip

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


def _control_styles(root, style, colors):
    """保留 ttk 的鼠标和键盘操作，只替换控件的绘制元素。"""
    tint, track, disabled = colors.selection, colors.field, colors.disabled
    if "Workspace.indicator" not in style.element_names():
        # PhotoImage 的生命周期要和窗口一样长，列表也供同一窗口的所有控件共用。
        images = root.__dict__.setdefault("_control_images", [])

        def image(width, height):
            item = tk.PhotoImage(master=root, width=width, height=height)
            images.append(item)
            return item

        unit = max(2, round(root.winfo_fpixels("1.5p")))
        size = 7 * unit

        def square(outline, fill, tick=None):
            item = image(size + 3 * unit, size)
            item.put(outline, to=(0, 0, size, size))
            item.put(fill, to=(1, 1, size - 1, size - 1))
            if tick:
                for x in range(unit, size - unit):
                    y = 5 * unit - abs(x - 5 * unit // 2)
                    item.put(tick, to=(x, y, x + 1, y + unit))
            return item

        style.element_create("Workspace.indicator", "image", square(colors.muted, colors.check_empty),
                             ("disabled", "selected", square(disabled, track, disabled)),
                             ("disabled", square(disabled, track)),
                             ("selected", square(colors.primary, colors.primary, colors.primary_ink)),
                             ("focus", square(colors.violet, colors.check_empty)))

        def tab_image(background, line):
            item = image(96, 32 + unit)
            item.put(background, to=(0, 0, 96, 32))
            item.put(line, to=(0, 32, 96, 32 + unit))
            return item

        style.element_create("Workspace.tab", "image", tab_image(colors.panel, colors.panel),
                             ("selected", "focus", tab_image(tint, colors.cyan)),
                             ("selected", tab_image(colors.tab, colors.violet)),
                             ("active", tab_image(track, track)), border=(0, 0, 0, unit), width=1, height=unit+1)

        def chevron(color):
            item = image(9 * unit, 5 * unit)
            for x in range(4 * unit + 1):
                y = unit + min(x, 4 * unit - x)
                item.put(color, to=(2 * unit + x, y, 2 * unit + x + 1, y + unit))
            return item

        style.element_create("Workspace.downarrow", "image", chevron(colors.muted),
                             ("disabled", chevron(disabled)), ("active", chevron(colors.violet)))

        def thumb(color, vertical):
            side = 7 * unit
            width, height = (side, side + 48) if vertical else (side + 48, side)
            item = image(width, height)
            for y in range(unit, height - unit):
                inset = max(0, 2 * unit - min(y, height - 1 - y))
                item.put(color, to=(unit + inset, y, width - unit - inset, y + 1))
            return item

        for orient in ("Vertical", "Horizontal"):
            vertical = orient == "Vertical"
            style.element_create(f"{orient}.Workspace.thumb", "image", thumb(colors.thumb, vertical),
                                 ("disabled", thumb(track, vertical)), ("pressed", thumb(colors.violet, vertical)),
                                 ("active", thumb(colors.muted, vertical)), border=3 * unit,
                                 width=7 * unit, height=7 * unit)

        # 九宫格只拉伸中心，按钮宽度变化时圆角和焦点描边保持原尺寸。
        radius = 3 * unit
        def button_face(fill, outline):
            # Tk 平铺中心区域。在 Windows 上逐像素平铺会产生数千次绘图调用。
            side = 2 * radius + 50
            item = image(side, side)
            for y in range(side):
                dy = max(0, radius - min(y, side - 1 - y))
                inset = round(radius - (radius ** 2 - dy ** 2) ** 0.5)
                item.put(outline, to=(inset, y, side - inset, y + 1))
                if 0 < y < side - 1:
                    item.put(fill, to=(inset + 1, y, side - inset - 1, y + 1))
            return item

        for name, fill, rim, hover, pressed in (
            ("Workspace.button", colors.button, colors.edge, colors.hover, colors.pressed),
            ("Workspace.primary", colors.primary, colors.violet, colors.primary, colors.primary),
        ):
            style.element_create(name, "image", button_face(fill, rim),
                                 ("disabled", button_face(colors.panel, colors.edge)),
                                 ("pressed", button_face(pressed, colors.violet)),
                                 ("focus", button_face(fill, colors.cyan)),
                                 ("active", button_face(hover, colors.violet)), border=radius + 1,
                                 padding=2, width=2 * radius + 3, height=2 * radius + 3)

    for name, face in (("TButton", "Workspace.button"), ("Primary.TButton", "Workspace.primary")):
        style.layout(name, [(face, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]}),
        ]})])

    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Workspace.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "w"}),
    ]})])
    style.map("TCheckbutton", background=[("!disabled", colors.panel), ("disabled", colors.panel)],
              foreground=[("disabled", disabled), ("focus", colors.violet), ("active", colors.violet)])

    style.layout("TNotebook.Tab", [("Workspace.tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"sticky": "nswe", "children": [("Notebook.label", {"sticky": ""})]}),
    ]})])
    style.configure("TNotebook.Tab", padding=(11, 5))
    style.map("TNotebook.Tab", padding=[], foreground=[("disabled", disabled), ("selected", colors.violet),
                                                      ("active", colors.text), ("!selected", colors.muted)])

    style.layout("TCombobox", [("Entry.field", {"sticky": "nswe", "children": [
        ("Workspace.downarrow", {"side": "right", "sticky": ""}),
        ("Combobox.padding", {"sticky": "nswe", "children": [
            ("Combobox.textarea", {"sticky": "nswe"}),
        ]}),
    ]})])
    style.configure("TCombobox", padding=(5, 3), fieldbackground=colors.field, lightcolor=colors.field,
                    darkcolor=colors.field, bordercolor=colors.edge)
    style.map("TCombobox", fieldbackground=[("disabled", track), ("readonly", colors.field)],
              foreground=[("disabled", disabled), ("readonly", colors.text)],
              selectbackground=[("readonly", colors.field)], selectforeground=[("readonly", colors.text)],
              bordercolor=[("focus", colors.violet), ("active", colors.muted)])
    style.configure("ComboboxPopdownFrame", relief="flat", background=colors.edge)
    for option, value in {"font": "TkDefaultFont", "background": colors.panel, "foreground": colors.text,
                          "selectBackground": tint, "selectForeground": colors.violet,
                          "selectBorderWidth": 0, "borderWidth": 0, "highlightThickness": 0}.items():
        root.option_add(f"*TCombobox*Listbox.{option}", value)

    for orient in ("Vertical", "Horizontal"):
        name = f"{orient}.TScrollbar"
        style.layout(name, [(f"{orient}.Scrollbar.trough", {"sticky": "nswe", "children": [
            (f"{orient}.Workspace.thumb", {"sticky": "nswe"}),
        ]})])
        style.configure(name, troughcolor=track, bordercolor=track, troughborderwidth=0)


def _workspace_theme(root: tk.Tk, colors: Palette) -> None:
    style = ttk.Style(root)
    name = "pcr-light" if colors is LIGHT else "pcr-dark"
    if name not in style.theme_names():
        style.theme_create(name, parent="clam")
    style.theme_use(name)
    font = tkfont.nametofont("TkDefaultFont", root=root)
    if "Microsoft YaHei UI" in tkfont.families(root):
        font.configure(family="Microsoft YaHei UI")
    font.configure(size=max(10, font.actual("size")))
    heading = (font.actual("family"), font.actual("size") + 2, "bold")
    style.configure(".", background=colors.panel, foreground=colors.text, font=font, bordercolor=colors.edge,
                    lightcolor=colors.panel, darkcolor=colors.panel, focuscolor=colors.violet,
                    selectbackground=colors.selection, selectforeground=colors.text)
    style.map(".", background=[("disabled", colors.panel), ("active", colors.header)],
              foreground=[("disabled", colors.disabled)],
              selectbackground=[("!focus", colors.selection)], selectforeground=[("!focus", colors.text)])
    style.configure("Workspace.TFrame", background=colors.workspace)
    style.configure("TPanedwindow", background=colors.workspace)
    style.configure("Sash", gripcount=0, sashthickness=8)
    style.configure("Header.TFrame", background=colors.header)
    style.configure("Header.TLabel", font=(font.actual("family"), font.actual("size") + 4, "bold"),
                    background=colors.header, foreground=colors.text)
    style.configure("HeaderInfo.TLabel", background=colors.header, foreground=colors.muted)
    style.configure("Title.TLabel", font=heading)
    style.configure("Muted.TLabel", foreground=colors.muted)
    style.configure("Status.TLabel", foreground=colors.muted, background=colors.workspace)
    style.configure("TLabelframe", borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", foreground=colors.violet)
    style.configure("TButton", padding=(9, 4))
    style.configure("Primary.TButton", padding=(18, 5))
    style.map("Primary.TButton", foreground=[("disabled", colors.disabled), ("!disabled", colors.primary_ink)])
    for field in ("TEntry", "TSpinbox"):
        style.configure(field, fieldbackground=colors.field, padding=4, insertcolor=colors.text,
                        arrowcolor=colors.muted, lightcolor=colors.field, darkcolor=colors.field)
        style.map(field, bordercolor=[("focus", colors.violet)], lightcolor=[("focus", colors.field)],
                  darkcolor=[("focus", colors.field)])
    style.configure("TNotebook", background=colors.panel, borderwidth=0, tabmargins=0,
                    bordercolor=colors.panel, lightcolor=colors.panel, darkcolor=colors.panel)
    _control_styles(root, style, colors)
    style.configure("Treeview", rowheight=font.metrics("linespace") + 10,
                    background=colors.field, fieldbackground=colors.field, borderwidth=0)
    style.configure("Treeview.Heading", background=colors.heading, foreground=colors.text,
                    padding=(5, 5), relief="flat", lightcolor=colors.heading, darkcolor=colors.heading)
    style.map("Treeview", background=[("selected", colors.selection)], foreground=[("selected", colors.text)])
    style.configure("Horizontal.TProgressbar", background=colors.cyan, troughcolor=colors.field,
                    borderwidth=0, lightcolor=colors.cyan, darkcolor=colors.cyan)
    root.option_add("*Toplevel.background", colors.panel)
    root.configure(background=colors.workspace)


class App(ttk.Frame):
    """The whole window."""

    def __init__(self, master: tk.Tk, excel: Path | None = None) -> None:
        saved = read_settings()
        self.colors = LIGHT if saved.get("theme") == "light" else DARK
        _workspace_theme(master, self.colors)
        super().__init__(master, padding=12, style="Workspace.TFrame")
        self.master: tk.Tk = master
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self.save: SaveFile | None = None
        self.session: TuneSession | None = None
        self.worker: threading.Thread | None = None
        self.events: queue.Queue = queue.Queue()
        self.roster_results: list[dict] = []
        self.shown_team: tuple[int, ...] = ()
        self._probe_team: tuple | None = None
        self.targets: list = []
        self.selection: set[str] = set()
        self.log_file = None
        self.shown_results = 0
        self.shown_history = 0
        self.last_run_dir: Path | None = None
        self.axis_dir = Path.cwd()
        # 输出根目录：载入结果轴时沿用最初那条轴的目录，免得 runs 套 runs
        self.output_root: Path | None = None
        self.hosted: HostedServer | None = None
        self._tips: list[Tooltip] = []

        self._make_vars()
        self._build_axis_row()
        self.workspace = ttk.Panedwindow(self, orient="horizontal")
        self.workspace.grid(row=1, column=0, sticky="nsew", pady=10)
        self._build_settings()
        self.output = ttk.Notebook(self.workspace, width=520)
        self.workspace.add(self.output, weight=3)
        self._build_items()
        self._build_actions()
        self._build_output()

        self._sync_mode()
        self._set_running(False)
        self._restore_settings(saved)
        if excel is not None:
            self.open_axis(Path(excel))
        self._pump()

    # ------------------------------------------------------------------ layout

    def _make_vars(self) -> None:
        self.axis_var = tk.StringVar()
        self.axis_info = tk.StringVar(value="尚未选择轴文件")
        self.calculator_var = tk.StringVar()
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.autostart_var = tk.BooleanVar(value=True)
        self.battle_time_var = tk.StringVar(value="90")
        self.seed_var = tk.StringVar(value="666")
        self.samples_var = tk.StringVar(value="1")
        self.batch_size_var = tk.StringVar(value="16")
        self.passes_var = tk.StringVar(value="1")
        self.timeout_var = tk.StringVar(value="180")
        self.calc_stats_var = tk.StringVar(value=str(DEFAULT_CALC_STATS))
        self.step_var = tk.StringVar(value="5")
        self.max_offset_var = tk.StringVar(value="100")
        self.outer_probes_var = tk.StringVar(value="3")
        self.threshold_var = tk.StringVar(value="500000")
        self.threshold_mode_var = tk.StringVar(value=ThresholdMode.ABSOLUTE.label)
        self.peak_tolerance_var = tk.StringVar(value="0.5")
        self.chunk_var = tk.StringVar(value="8")
        self.include_boss_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=True)
        self.only_checked_var = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value=dict(MODES)["frame"])
        self.ub_type_var = tk.StringVar(value=UB_TYPE_LABELS[SemanUBType.SERIES_1])
        self.skill_type_var = tk.StringVar(value=SKILL_TYPE_LABELS[SemanSkillType.SERIES_1])
        self.sequence_type_var = tk.StringVar(value=SEQUENCE_TYPE_LABELS[SemanSequenceType.LANES])
        self.population_var = tk.StringVar(value="48")
        self.generations_var = tk.StringVar(value="30")
        self.islands_var = tk.StringVar(value="3")
        self.crossover_var = tk.StringVar(value="0.75")
        self.mutation_var = tk.StringVar(value="0.22")
        self.elite_var = tk.StringVar(value="2")
        self.mutation_decay_var = tk.StringVar(value="1.0")
        self.mutation_floor_var = tk.StringVar(value="0.0")
        self.stagnation_var = tk.StringVar(value="0")
        self.restarts_var = tk.StringVar(value="1")
        self.migration_var = tk.StringVar(value="10")
        self.migrants_ratio_var = tk.StringVar(value="0.08")
        self.disaster_var = tk.StringVar(value="20")
        self.max_lane_var = tk.StringVar(value="96")
        self.fingerprint_var = tk.StringVar(value="1.0")
        self.ga_seed_var = tk.StringVar(value="42")
        self.delay_var = tk.StringVar(value="3")
        self.legacy_ub_var = tk.BooleanVar(value=False)
        self.own_skill_var = tk.BooleanVar(value=True)
        self.other_skill_var = tk.BooleanVar(value=False)
        # 退火模式的参数
        self.iterations_var = tk.StringVar(value="400")
        self.initial_temperature_var = tk.StringVar(value="0.02")
        self.final_temperature_var = tk.StringVar(value="0.0002")
        self.chain_range_var = tk.StringVar(value="4")
        self.swap_chance_var = tk.StringVar(value="0.55")
        self.double_chance_var = tk.StringVar(value="0.15")
        self.chain_chance_var = tk.StringVar(value="0.2")
        self.manual_chance_var = tk.StringVar(value="0.1")
        self.allow_duplicates_var = tk.BooleanVar(value=False)
        self.anneal_restarts_var = tk.StringVar(value="1")
        self.report_every_var = tk.StringVar(value="25")
        self.anneal_delay_var = tk.StringVar(value="3")
        # 0 押轴：只多一个「锁定站位」，其余复用「通用」页的战斗秒数/种子/采样/场次/超时/复核
        self.zero_iterations_var = tk.StringVar(value="1200")
        self.zero_batch_var = tk.StringVar(value="4")
        self.zero_ub_limit_var = tk.StringVar(value="12")
        self.roster_pool_var = tk.StringVar(value="")
        self.to_max_attr_var = tk.BooleanVar(value=True)
        self.every_combo_var = tk.BooleanVar(value=True)
        self.roster_book = None
        self.roster_groups: list = []
        self._icons: dict = {}
        self._icon_worker: threading.Thread | None = None
        self._icon_cancel = threading.Event()
        self._icon_refresh = set()
        self._icon_total = 0
        self.status_var = tk.StringVar(value="就绪")

    def _hint(self, widget: tk.Widget, message: str) -> tk.Widget:
        self._tips.append(Tooltip(widget, message))
        return widget

    def _build_axis_row(self) -> None:
        frame = ttk.Frame(self, padding=(16, 10), style="Header.TFrame")
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        mark = tk.Canvas(frame, width=44, height=44, highlightthickness=0, background=self.colors.header)
        self.header_mark = mark
        mark.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self.axis_title = tk.StringVar(value="公会战时间轴")
        title = ttk.Label(frame, textvariable=self.axis_title, style="Header.TLabel")
        title.grid(row=0, column=1, sticky="ew")
        self.axis_tip = Tooltip(title, "选择 PCRCalculator 导出的 .xlsx 时间轴。")
        self.axis_overview = tk.Canvas(frame, width=148, height=44, background=self.colors.header, highlightthickness=0)
        self._hint(self.axis_overview, "五条横线依次对应五名角色，亮点表示原表中各次 UB 的计划帧。")
        button = ttk.Button(frame, text="打开时间轴…", command=self.ask_axis)
        button.grid(row=0, column=3, rowspan=2, padx=(12, 0))
        self._hint(button, "选择 PCRCalculator 导出的 .xlsx 轴文件。")
        info = ttk.Label(frame, textvariable=self.axis_info, style="HeaderInfo.TLabel")
        info.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        # 文件栏底部的光带独立绘制，不占用文件名或队伍信息的宽度。
        rail = tk.Canvas(frame, height=2, highlightthickness=0, background=self.colors.header)
        rail.place(relx=0, rely=1, relwidth=1, anchor="sw", bordermode="outside")
        def draw_rail(event):
            rail.delete("all")
            stops = ((63, 47, 88), (160, 111, 229), (100, 174, 183), (64, 56, 88))
            for segment, (left, right) in enumerate(zip(stops, stops[1:])):
                for step in range(16):
                    color = "#%02x%02x%02x" % tuple(a + (b - a) * step // 16 for a, b in zip(left, right))
                    x = (segment * 16 + step) * event.width // 48
                    end = (segment * 16 + step + 1) * event.width // 48
                    rail.create_rectangle(x, 0, end, 2, fill=color, outline="")
        rail.bind("<Configure>", draw_rail)

        def fit_header(event):
            if event.width >= 960:
                self.axis_overview.grid(row=0, column=2, rowspan=2, padx=(10, 0))
            else:
                self.axis_overview.grid_remove()
            info.configure(wraplength=max(160, frame.grid_bbox(1, 1)[2]))

        frame.bind("<Configure>", fit_header)
        self._draw_axis_overview()

    def _draw_axis_overview(self) -> None:
        mark = self.header_mark
        mark.configure(background=self.colors.header)
        mark.delete("all")
        mark.create_polygon(2, 11, 12, 2, 41, 2, 41, 31, 31, 41, 2, 41,
                            fill=self.colors.tab, outline=self.colors.violet)
        for y, shift, color in ((14, 0, self.colors.violet), (22, 5, self.colors.text), (30, 2, self.colors.cyan)):
            mark.create_line(9, y, 34, y, fill=self.colors.edge, width=2)
            mark.create_line(13 + shift, y, 20 + shift, y, fill=color, width=4)
        chart = self.axis_overview
        chart.configure(background=self.colors.header)
        chart.delete("all")
        for x in range(4, 146, 14):
            chart.create_line(x, 3, x, 41, fill=self.colors.edge, dash=(2, 3))
        lanes = self.save.frames[:5] if self.save else []
        duration = self.save.suggested_duration(self.save.header) if self.save else 90
        span = max(1, duration * 60, *(max(row, default=0) for row in lanes))
        for i, color in enumerate((self.colors.violet, "#dac4ff", "#a997ff", "#7bb9dc", self.colors.cyan)):
            y = 6 + i * 8
            chart.create_line(4, y, 144, y, fill=self.colors.edge)
            for frame in lanes[i] if i < len(lanes) else ():
                x = 4 + 140 * frame / span
                chart.create_line(x, y - 2, x, y + 3, fill=color, width=2)

    def _build_settings(self) -> None:
        holder = ttk.Frame(self.workspace, padding=(12, 10), width=460)
        self.workspace.add(holder, weight=2)
        holder.grid_propagate(False)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(1, weight=1)
        bar = ttk.Frame(holder)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(bar, text="优化模式", style="Title.TLabel").pack(side="left")
        self.mode_box = ttk.Combobox(bar, textvariable=self.mode_var, width=13, state="readonly",
                                     values=[label for _, label in MODES])
        self.mode_box.pack(side="right")
        self.mode_box.bind("<<ComboboxSelected>>", lambda _: self._sync_mode())
        self.settings_tabs = ttk.Notebook(holder)
        self.settings_tabs.grid(row=1, column=0, sticky="nsew")
        self.tab_mode = ttk.Frame(self.settings_tabs)
        self.tab_common = ttk.Frame(self.settings_tabs)
        self.settings_tabs.add(self.tab_mode, text="模式参数")
        self.settings_tabs.add(self.tab_common, text="通用设置")
        self.scroll_views = {}
        modes_area = self._scrollable_settings(self.tab_mode)
        shared_area = self._scrollable_settings(self.tab_common)
        server = ttk.LabelFrame(shared_area, text="摸轴器", padding=8)
        server.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        server.columnconfigure(1, weight=1)
        ttk.Label(server, text="算器目录").grid(row=0, column=0, sticky="w")
        calc_entry = ttk.Entry(server, textvariable=self.calculator_var, width=12)
        calc_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self._hint(calc_entry, "请选择 PCRCalculator 安装目录或其中的 server 目录。")
        ttk.Button(server, text="浏览…", command=self.ask_calculator).grid(row=0, column=2)
        ttk.Label(server, text="端口").grid(row=1, column=0, pady=5, sticky="w")
        port_entry = ttk.Entry(server, textvariable=self.port_var, width=8)
        port_entry.grid(row=1, column=1, sticky="w", padx=6)
        self._hint(port_entry, "摸轴器监听端口，默认 8080。")
        ttk.Checkbutton(server, text="开始搜索时自动启动摸轴器", variable=self.autostart_var).grid(
            row=2, column=0, columnspan=3, sticky="w")
        self.tab_shared = ttk.Frame(shared_area)
        self.tab_shared.grid(row=1, column=0, sticky="ew")
        self.mode_pages = {key: ttk.Frame(modes_area) for key, _ in MODES}
        self.tab_frame = self.mode_pages["frame"]
        self.tab_ga = self.mode_pages["ga"]
        self.tab_anneal = self.mode_pages["anneal"]
        self.tab_roster = self.mode_pages["roster"]
        self.tab_zero = self.mode_pages["zero"]
        for tab in (self.tab_shared, *self.mode_pages.values()):
            tab.columnconfigure(1, weight=1)
            tab.columnconfigure(3, weight=1)
        cycle = self.mode_pages["cycle"]
        self.cycle_vars = {}
        for row, param in enumerate(mode_registry.CYCLE_PARAMS):
            var = tk.StringVar(value=str(param.default))
            self.cycle_vars[param.name] = var
            ttk.Label(cycle, text=param.label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(cycle, textvariable=var, width=10)
            entry.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(10, 0), pady=3)
            self._hint(entry, param.tip)
        note = ttk.Label(cycle, style="Muted.TLabel", justify="left", wraplength=300,
                         text="循环搜索会根据 TP 到账和技能命中，尝试增减 UB 或重排队友的出手顺序。"
                              "搜索层数决定能连续改几步，保留分支决定每层留下几种打法，每支候选决定每种打法试多少次。"
                              "\n\n结果表保留整场期望伤害最高的轴。找到比原轴多开 UB 的打法时，还会另存多发分支。")
        note.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self._build_shared(self.tab_shared)
        self._build_frame_tab(self.tab_frame)
        self._build_ga(self.tab_ga)
        self._build_anneal(self.tab_anneal)
        self._build_roster(self.tab_roster)
        self._build_zero(self.tab_zero)
        for tab in (self.tab_shared, *self.mode_pages.values()):
            notes = [w for w in tab.winfo_children() if isinstance(w, ttk.Label)
                     and w.cget("style") == "Muted.TLabel"]
            for w in notes:
                w.configure(wraplength=300, justify="left")
                w.grid_configure(sticky="ew")
            def wrap_notes(event, labels=notes):
                width = max(150, event.width - 12)
                for label in labels:
                    if int(label.cget("wraplength")) != width:
                        label.configure(wraplength=width)
            tab.bind("<Configure>", wrap_notes)
        self.master.bind("<MouseWheel>", self._scroll_settings, add="+")

    def _scrollable_settings(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        view = tk.Canvas(tab, width=100, height=100, background=self.colors.panel, highlightthickness=0)
        view.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(tab, orient="vertical", command=view.yview)
        bar.grid(row=0, column=1, sticky="ns")
        view.configure(yscrollcommand=bar.set)
        body = ttk.Frame(view, padding=(3, 12, 5, 5))
        body.columnconfigure(0, weight=1)
        window = view.create_window(0, 0, anchor="nw", window=body)
        view.bind("<Configure>", lambda e: view.itemconfigure(window, width=e.width))
        body.bind("<Configure>", lambda _: view.configure(scrollregion=view.bbox("all")))
        self.scroll_views[tab] = view
        return body

    def _scroll_settings(self, event):
        for tab, view in self.scroll_views.items():
            if str(event.widget).startswith(str(tab) + ".") and view.yview() != (0.0, 1.0):
                if event.widget.winfo_class() not in ("Treeview", "TCombobox"):
                    view.yview_scroll(-1 if event.delta > 0 else 1, "units")
                break

    def _build_shared(self, tab: ttk.Frame) -> None:
        """两种优化模式都要用的接口参数。"""
        pairs = [
            ("战斗秒数", self.battle_time_var, "种子", self.seed_var,
             "模拟战斗时长（1~90 秒）。打开轴时会按轴名里的「尾刀 N秒」自动填，没有就填 90。",
             "固定战斗随机种子。用轴里记录的种子才能和游戏里逐位一致。"),
            ("采样种子数", self.samples_var, "单次场次", self.batch_size_var,
             "每个候选打几场（不同种子取平均）。1 最快，结果也最确定。",
             "一次接口请求里塞几场战斗。默认 16。"),
        ]
        row = 0
        for left_label, left_var, right_label, right_var, left_tip, right_tip in pairs:
            ttk.Label(tab, text=left_label).grid(row=row, column=0, sticky="w", pady=1)
            left = ttk.Entry(tab, textvariable=left_var, width=10)
            left.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
            self._hint(left, left_tip)
            ttk.Label(tab, text=right_label).grid(row=row, column=2, sticky="w", pady=1)
            right = ttk.Entry(tab, textvariable=right_var, width=10)
            right.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=1)
            self._hint(right, right_tip)
            row += 1

        ttk.Label(tab, text="超时秒").grid(row=row, column=0, sticky="w", pady=1)
        timeout = ttk.Entry(tab, textvariable=self.timeout_var, width=10)
        timeout.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
        self._hint(timeout, "单次接口请求的超时时间（秒）。摸轴器卡住时不用等太久。")
        verify_check = ttk.Checkbutton(tab, text="跑完复核", variable=self.verify_var)
        verify_check.grid(row=row, column=2, columnspan=2, sticky="w", pady=1)
        self._hint(verify_check, "结束后再用最终结果打一场，把伤害写进报告。")
        row += 1

        note = ttk.Label(
            tab,
            text="战斗秒数和种子会在载入轴时自动填入，采样种子数大于 1 时按多场平均伤害比较。",
            style="Muted.TLabel",
        )
        note.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _build_frame_tab(self, tab: ttk.Frame) -> None:
        """逐帧搜索的参数。"""
        pairs = [
            ("步长", self.step_var, "偏移±", self.max_offset_var,
             "每次挪动多少帧。步长越大搜得越快，越小越细。",
             "以原帧为中心，向上/向下最多探多少帧（窗口半径）。"),
            ("外探", self.outer_probes_var, "峰值容差", self.peak_tolerance_var,
             "超阈值后继续往外探几帧，用来确认没有更大的峰。0 表示不外探。",
             "判定「同一档伤害」的容差：差值小于它就当一样，取更早的帧。"),
            ("阈值", self.threshold_var, "每批帧数", self.chunk_var,
             "允许伤害下降多少还算「可接受」。口径由下面的下拉框决定。",
             "一次请求里预取多少帧（只影响速度，不影响结果）。"),
        ]
        row = 0
        for left_label, left_var, right_label, right_var, left_tip, right_tip in pairs:
            ttk.Label(tab, text=left_label).grid(row=row, column=0, sticky="w", pady=1)
            left_entry = ttk.Entry(tab, textvariable=left_var, width=10)
            left_entry.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
            self._hint(left_entry, left_tip)
            ttk.Label(tab, text=right_label).grid(row=row, column=2, sticky="w", pady=1)
            right_entry = ttk.Entry(tab, textvariable=right_var, width=10)
            right_entry.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=1)
            self._hint(right_entry, right_tip)
            row += 1

        ttk.Label(tab, text="阈值口径").grid(row=row, column=0, sticky="w", pady=1)
        mode_box = ttk.Combobox(
            tab, textvariable=self.threshold_mode_var, width=12, state="readonly",
            values=[mode.label for mode in ThresholdMode],
        )
        mode_box.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
        self._hint(
            mode_box,
            "绝对伤害：允许掉固定数值；百分比：允许掉基线伤害的百分之几；"
            "不允许掉伤害：只要掉一点就当成边界。",
        )
        boss_check = ttk.Checkbutton(
            tab, text="含 Boss 行", variable=self.include_boss_var, command=self.rebuild_targets,
        )
        boss_check.grid(row=row, column=2, columnspan=2, sticky="w", pady=1)
        self._hint(boss_check, "把第六条 Boss 帧行也当作可调项（默认只调 5 个角色）。")
        row += 1

        ttk.Label(tab, text="循环轮数").grid(row=row, column=0, sticky="w", pady=1)
        passes = ttk.Entry(tab, textvariable=self.passes_var, width=10)
        passes.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
        self._hint(
            passes,
            "整套流程重复跑几轮：每轮都拿上一轮写回后的最优帧当起点再调一遍，"
            "后期收益会越来越小。1 = 只跑一遍。",
        )
        ttk.Label(tab, text="CalcStats").grid(row=row, column=2, sticky="w", pady=1)
        stats = ttk.Entry(tab, textvariable=self.calc_stats_var, width=10)
        stats.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=1)
        self._hint(
            stats,
            "统计位掩码。默认 3（Core|UBTime）已经够读伤害；读不到伤害时会自动改用 511 重试。",
        )
        row += 1

        note = ttk.Label(
            tab,
            text="这一页只在「逐帧搜索」模式生效；想整条轴一起优化，把上面的优化模式切到语义 GA。",
            style="Muted.TLabel",
        )
        note.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _build_ga(self, tab: ttk.Frame) -> None:
        """语义 GA 的参数（在「语义 GA」标签页里）。"""
        ga = tab
        pairs = [
            ("种群", self.population_var, "代数", self.generations_var,
             "每一代保留多少个候选 UB 序列。越大越不容易早熟，但每代更慢。",
             "总共跑多少代。中途停止不会导出结果，可以从检查点续跑。"),
            ("岛屿", self.islands_var, "交叉率", self.crossover_var,
             "把种群分成几个岛各自进化，隔若干代互相迁入个体，避免全都撞在一起。",
             "两个父代按比例混合的强度（0~1）。"),
            ("突变率", self.mutation_var, "迁移间隔", self.migration_var,
             "每个基因被随机改动的概率（0~1）。太小容易卡住，太大变成乱搜。",
             "每隔几代从上一个岛迁一批个体过来。"),
            ("迁移比例", self.migrants_ratio_var, "灾变间隔", self.disaster_var,
             "每次迁移换掉本岛多少比例的个体（0~1）。",
             "连着多少代没有进步就注入一批全新个体，跳出局部最优。"),
            ("单行上限", self.max_lane_var, "指纹容差", self.fingerprint_var,
             "一条 UB 序列最多允许多少个基因（防止序列无限长）。",
             "动作进度指纹的容差：小于它就算行为重复，直接复用已有结果。0 = 不判重。"),
            ("精英保留", self.elite_var, "重启轮数", self.restarts_var,
             "每一代原样留到下一代的最优个体数。留着保证不会把已经找到的好序列弄丢；0 = 不保留。",
             "换不同随机种子把整套 GA 重跑几轮，最后取最好的一轮（更稳，但更慢）。"),
            ("突变衰减", self.mutation_decay_var, "突变下限", self.mutation_floor_var,
             "每代把突变率乘上这个系数：前期敢乱跳，后期慢慢收细（1 = 不衰减）。",
             "突变率衰减到这个值就不再降（0 = 一直降到 0）。"),
            ("停滞停止", self.stagnation_var, "GA 种子", self.ga_seed_var,
             "连着多少代没有进步就提前结束，省时间（0 = 一直跑到代数上限）。",
             "遗传算法自己的随机种子。同一种子 + 同一配置可复现。"),
            ("退避帧数", self.delay_var, "", None,
             "遗传算法自己的随机种子。同一种子 + 同一配置可复现。",
             "基因池额外加入 1..N 帧的候选值，用来给 UB 留一点提前/推迟的余地。"),
        ]
        row = 0
        for left_label, left_var, right_label, right_var, left_tip, right_tip in pairs:
            ttk.Label(ga, text=left_label).grid(row=row, column=0, sticky="w", pady=1)
            left = ttk.Entry(ga, textvariable=left_var, width=10)
            left.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
            self._hint(left, left_tip)
            if right_label and right_var is not None:
                ttk.Label(ga, text=right_label).grid(row=row, column=2, sticky="w", pady=1)
                right = ttk.Entry(ga, textvariable=right_var, width=10)
                right.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=1)
                self._hint(right, right_tip)
            row += 1

        ttk.Label(ga, text="UB 类型").grid(row=row, column=0, sticky="w", pady=1)
        ub_box = ttk.Combobox(
            ga, textvariable=self.ub_type_var, width=18, state="readonly",
            values=list(UB_TYPE_LABELS.values()),
        )
        ub_box.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
        self._hint(ub_box, "语义 UB 的分段方式，必须和摸轴器里这条轴的口径一致。")
        ttk.Label(ga, text="技能类型").grid(row=row, column=2, sticky="w", pady=1)
        skill_box = ttk.Combobox(
            ga, textvariable=self.skill_type_var, width=14, state="readonly",
            values=list(SKILL_TYPE_LABELS.values()),
        )
        skill_box.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=1)
        self._hint(skill_box, "语义技能族，决定基因池里放哪些技能段（默认 SERIES_20）。")
        row += 1

        ttk.Label(ga, text="序列类型").grid(row=row, column=0, sticky="w", pady=1)
        seq_box = ttk.Combobox(
            ga, textvariable=self.sequence_type_var, width=26, state="readonly",
            values=list(SEQUENCE_TYPE_LABELS.values()),
        )
        seq_box.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=1)
        self._hint(
            seq_box,
            "只调五条 UB 序列：角色顺序沿用原轴。"
            "同时调角色序列：连「谁先放」一起搜，搜索空间更大、更慢。",
        )
        row += 1

        flags = ttk.Frame(ga)
        flags.grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
        legacy = ttk.Checkbutton(flags, text="加入旧版 UB 候选", variable=self.legacy_ub_var)
        legacy.grid(row=0, column=0, sticky="w")
        self._hint(legacy, "往基因池里加入旧版 UB 的编码，适合这条轴本来就混着旧版 UB 的情况。")
        own = ttk.Checkbutton(flags, text="加入本角色技能", variable=self.own_skill_var)
        own.grid(row=0, column=1, sticky="w", padx=6)
        self._hint(own, "让基因池包含本角色自己的技能段（一般都要开）。")
        other = ttk.Checkbutton(flags, text="加入其他角色技能", variable=self.other_skill_var)
        other.grid(row=1, column=0, sticky="w")
        self._hint(other, "把其他角色的技能段也放进池子，搜索面更大但更慢。")
        row += 1

        note = ttk.Label(
            ga,
            text="种群决定每代保留的候选数量。增加代数可以继续探索，增加岛屿可以同时尝试不同打法。",
            style="Muted.TLabel",
        )
        note.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _build_anneal(self, tab: ttk.Frame) -> None:
        """模拟退火的参数（在「退火」标签页里）。"""
        anneal = tab
        pairs = [
            ("迭代次数", self.iterations_var, "重启轮数", self.anneal_restarts_var,
             "总共试多少个邻居，每个邻居打一场。越大搜得越久、可能越好。",
             "换不同随机种子把整套退火重跑几轮，最后取最好的一轮。"),
            ("初始温度", self.initial_temperature_var, "最低温度", self.final_temperature_var,
             "按基线伤害的比例算（0.02 = 2%）。越大越敢接受变差的一步。",
             "降到这个比例就基本不再接受变差的步。"),
            ("链长上限", self.chain_range_var, "日志间隔", self.report_every_var,
             "「链式替换」一次最多连着改几刀。",
             "每多少次迭代打一行进度。"),
            ("单点替换", self.swap_chance_var, "双点互换", self.double_chance_var,
             "随机挑一刀换一个候选基因的概率。",
             "随机挑两刀互换位置的概率。"),
            ("链式替换", self.chain_chance_var, "手动指定", self.manual_chance_var,
             "某条序列上连续一段整体换掉基因的概率。",
             "先等概率挑角色、再改这一条里的一刀的概率。"),
            ("退避帧数", self.anneal_delay_var, "", None,
             "基因池额外加入 1..N 帧的候选值，给 UB 留一点提前/推迟的余地。",
             ""),
        ]
        row = 0
        for left_label, left_var, right_label, right_var, left_tip, right_tip in pairs:
            ttk.Label(anneal, text=left_label).grid(row=row, column=0, sticky="w", pady=1)
            left = ttk.Entry(anneal, textvariable=left_var, width=10)
            left.grid(row=row, column=1, sticky="ew", padx=(6, 10), pady=1)
            self._hint(left, left_tip)
            if right_label and right_var is not None:
                ttk.Label(anneal, text=right_label).grid(row=row, column=2, sticky="w", pady=1)
                right = ttk.Entry(anneal, textvariable=right_var, width=10)
                right.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=1)
                self._hint(right, right_tip)
            row += 1

        allow = ttk.Checkbutton(
            anneal, text="允许重访已评估的状态", variable=self.allow_duplicates_var,
        )
        allow.grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self._hint(
            allow,
            "关着的时候，算过的基因直接用上次的分数，不重复打；打开就每次都重新算（更慢、结果更抖）。",
        )
        row += 1

        note = ttk.Label(
            anneal,
            text="这一页只在「退火」模式生效；语义口径（UB/技能/序列类型）跟「语义 GA」那页共用。",
            style="Muted.TLabel",
        )
        note.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _build_roster(self, tab: ttk.Frame) -> None:
        """阵容搜索：候选池 + 角色分组；队伍固定 5 人，组合全部排完。"""
        roster = tab
        roster.columnconfigure(1, weight=1)
        roster.columnconfigure(3, weight=1)
        ttk.Label(roster, text="候选角色").grid(row=0, column=0, sticky="w", pady=1)
        pool = ttk.Entry(roster, textvariable=self.roster_pool_var)
        pool.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(6, 6), pady=1)
        self._hint(pool, "要让程序挑的角色（逗号分隔的 unitId）。点右边按钮可以从摸轴器的角色表里挑。")
        pick = ttk.Button(roster, text="选择角色…", command=self.choose_characters)
        pick.grid(row=0, column=3, sticky="w", pady=1)
        self._hint(pick, "打开角色选择窗口：可以按职能 / 属性过滤，点一下勾选，图标会缓存到本机。")

        ttk.Label(roster, text="角色分组").grid(row=1, column=0, sticky="nw", pady=(8, 1))
        holder = ttk.Frame(roster)
        holder.grid(row=1, column=1, columnspan=3, sticky="nsew", pady=(8, 1))
        holder.columnconfigure(0, weight=1)
        self.group_tree = ttk.Treeview(
            holder, columns=("name", "minimum", "members"), show="headings", height=5
        )
        font = tkfont.nametofont("TkDefaultFont", root=self.master)
        for column, heading in (("name", "分组"), ("minimum", "最少人数"), ("members", "成员")):
            minimum = font.measure(heading) + 22
            self.group_tree.heading(column, text=heading)
            self.group_tree.column(column, width=max(minimum, 64), minwidth=minimum,
                                   stretch=column == "members", anchor="center" if column == "minimum" else "w")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.group_tree.yview)
        self.group_tree.configure(yscrollcommand=scroll.set)
        self.group_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.group_tree.bind("<Double-1>", lambda _event: self.edit_group())
        self._hint(
            self.group_tree,
            "只从候选池选人，各组可以超过最少人数；"
            "跨组角色同时计入各组，最少人数填 0 表示不限。",
        )

        buttons = ttk.Frame(roster)
        buttons.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 6))
        new = ttk.Button(buttons, text="新建分组", command=self.new_group)
        new.grid(row=0, column=0, padx=2, pady=2, sticky="ew")
        self._hint(new, "起个组名、填最少要上几个人，再从候选池里勾成员。")
        edit = ttk.Button(buttons, text="编辑", command=self.edit_group)
        edit.grid(row=0, column=1, padx=2, pady=2, sticky="ew")
        self._hint(edit, "改选中分组的名字、最少人数和成员（双击一行也行）。")
        remove = ttk.Button(buttons, text="删除", command=self.delete_group)
        remove.grid(row=0, column=2, padx=2, pady=2, sticky="ew")
        self._hint(remove, "把选中的分组删掉，成员回到未分组。")
        save = ttk.Button(buttons, text="保存预设…", command=self.save_preset)
        save.grid(row=1, column=0, padx=2, pady=2, sticky="ew")
        self._hint(save, "把候选角色和分组存成一份预设，下次可以一键载入。")
        load = ttk.Button(buttons, text="载入预设…", command=self.load_preset)
        load.grid(row=1, column=1, padx=2, pady=2, sticky="ew")
        self._hint(load, "载入之前保存的候选池与分组预设。")

        row = 3
        full = ttk.Checkbutton(roster, text="按满属性算", variable=self.to_max_attr_var)
        full.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._hint(full, "勾上就把这几个角色按满级满 Rank 算（不同练度的角色才能直接比）；不勾就沿用轴里的练度。")
        combos = ttk.Checkbutton(roster, text="每队试 32 种 AUTO/SET", variable=self.every_combo_var)
        combos.grid(row=row, column=2, columnspan=2, sticky="w", pady=(4, 0))
        self._hint(combos, "每支队伍都把「每个人 AUTO / SET」的 2^5 种组合各打一遍取最高；"
                           "关掉只打全 SET（OOOOO）。")
        row += 1
        verify = ttk.Checkbutton(roster, text="跑完复核", variable=self.verify_var)
        verify.grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._hint(verify, "导出前用最优阵容再打一场确认伤害。")
        row += 1
        icon_row = ttk.Frame(roster)
        icon_row.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        pack_button = ttk.Button(icon_row, text="导入图标包…", command=self.import_icon_pack)
        pack_button.grid(row=0, column=0, sticky="w")
        self._hint(pack_button, "选一个 icon.zip，把里面的图标解压到本机缓存目录（随发布包附带一份）。")
        fetch = ttk.Button(icon_row, text="下载选中角色的图标", command=self.download_pool_icons)
        fetch.grid(row=0, column=1, sticky="w", padx=6)
        self._hint(fetch, "按候选池里的角色从 wthee.xyz 下载图标；后台多线程，界面不会卡。")
        ttk.Button(buttons, text="查看结果…", command=self.show_roster_results).grid(
            row=1, column=2, padx=2, pady=2, sticky="ew")
        self.icon_bar = ttk.Progressbar(icon_row, mode="determinate", length=180)
        self.icon_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        self.icon_status = ttk.Label(icon_row, text="图标缓存：%s" % _icon_dir_text(), wraplength=270, style="Muted.TLabel")
        self.icon_status.grid(row=2, column=0, columnspan=2, sticky="w")
        row += 1
        note = ttk.Label(
            roster,
            text="这一页只在「阵容搜索」模式生效：队伍固定 5 人，所有队伍组合都会排完；"
                 "勾选「每队试 32 种组合」会比较全队的 AUTO / SET 搭配。",
            style="Muted.TLabel",
        )
        note.grid(row=row, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _build_zero(self, tab: ttk.Frame) -> None:
        """0 押轴：整场不打手动 UB，退火逐刀决定每刀是 O（SET）还是 X（AUTO）。"""
        zero = tab
        zero.columnconfigure(1, weight=1)
        zero.columnconfigure(3, weight=1)
        ttk.Label(zero, text="退火轮数").grid(row=0, column=0, sticky="w", pady=1)
        iterations = ttk.Entry(zero, textvariable=self.zero_iterations_var, width=10)
        iterations.grid(row=0, column=1, sticky="w", padx=(6, 0), pady=1)
        self._hint(iterations, "每轮同时提几个新方案、挑能接受的那个；1200 轮大约一两分钟，5000 轮更稳。")
        ttk.Label(zero, text="每轮候选").grid(row=0, column=2, sticky="e", pady=1)
        batch = ttk.Entry(zero, textvariable=self.zero_batch_var, width=10)
        batch.grid(row=0, column=3, sticky="w", padx=(6, 0), pady=1)
        self._hint(batch, "一轮里并行试几个方案（4 已经能喂满摸轴器，再大只是排队）。")
        ttk.Label(zero, text="最多几刀").grid(row=1, column=0, sticky="w", pady=1)
        limit = ttk.Entry(zero, textvariable=self.zero_ub_limit_var, width=10)
        limit.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=1)
        self._hint(limit, "每个角色最多排到第几刀；更后面的刀沿用最后一刀的设定。")
        note = ttk.Label(
            zero,
            text="这一页只在「0押轴」模式生效：整场不打手动 UB，同一角色的每一刀都可以单独选 O（SET，一有机会就按）"
                 "或 X（AUTO，交给角色自己找时机），比如 1 号位 O X O O（之后都是 O）。退火会一步步试，越试越好。",
            style="Muted.TLabel",
            wraplength=760,
            justify="left",
        )
        note.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        edge = ttk.Label(
            zero,
            text="战斗秒数 / 种子 / 采样 / 超时 / 跑完复核 都在「通用」页里；起始温度与结束温度用默认值即可。",
            style="Muted.TLabel",
        )
        edge.grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _zero_settings(self):
        from .zero import ZeroSettings

        return ZeroSettings(
            endpoint=endpoint_for(self._port()),
            battle_time=self._number(self.battle_time_var, "战斗秒数", 1, 90),
            seed=self._number(self.seed_var, "战斗种子", -(2 ** 31), 2 ** 31 - 1),
            samples=self._number(self.samples_var, "采样种子数", 1, 100),
            iterations=self._number(self.zero_iterations_var, "退火轮数", 1, 200000),
            batch=self._number(self.zero_batch_var, "每轮候选", 1, 32),
            ub_limit=self._number(self.zero_ub_limit_var, "最多几刀", 1, 24),
            timeout=float(self._number(self.timeout_var, "超时秒", 1, 3600, integer=False)),
            to_max_attr=bool(self.to_max_attr_var.get()),
            verify=bool(self.verify_var.get()),
        )

    def _apply_zero_settings(self, settings) -> None:
        self.zero_iterations_var.set(str(settings.iterations))
        self.zero_batch_var.set(str(settings.batch))
        self.zero_ub_limit_var.set(str(settings.ub_limit))
        self.battle_time_var.set(str(settings.battle_time))
        self.seed_var.set(str(settings.seed))
        self.samples_var.set(str(settings.samples))
        self.to_max_attr_var.set(bool(settings.to_max_attr))
        self.verify_var.set(bool(settings.verify))

    # ---------------------------------------------------------------- 分组
    def _roster_groups(self) -> list:
        return list(self.roster_groups)

    def _center_window(self, window: tk.Misc, *, parent: tk.Misc | None = None) -> None:
        """把子窗口摆到主窗口正中间（Tk 默认放左上角）。"""
        anchor = parent if parent is not None else self.master
        try:
            window.update_idletasks()
            width = max(int(window.winfo_reqwidth()), int(window.winfo_width()))
            height = max(int(window.winfo_reqheight()), int(window.winfo_height()))
            left = int(anchor.winfo_rootx()) + (int(anchor.winfo_width()) - width) // 2
            top = int(anchor.winfo_rooty()) + (int(anchor.winfo_height()) - height) // 2
        except tk.TclError:
            return
        screen_width = int(window.winfo_screenwidth())
        screen_height = int(window.winfo_screenheight())
        left = max(0, min(left, screen_width - width))
        top = max(0, min(top, screen_height - height))
        window.geometry(f"{width}x{height}+{left}+{top}")

    def _refresh_groups(self) -> None:
        tree = getattr(self, "group_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        book = self.roster_book
        for index, group in enumerate(self.roster_groups):
            members = "、".join(
                (book.label(unit) if book is not None else str(unit)) for unit in group.members
            ) or "（空）"
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(group.name, group.minimum, members),
            )

    def new_group(self) -> None:
        group = self._group_dialog()
        if group is not None:
            self.roster_groups.append(group)
            self._refresh_groups()
            self._append_log(f"新增分组 {group.label}")

    def edit_group(self) -> None:
        selection = self.group_tree.selection() if hasattr(self, "group_tree") else ()
        if not selection:
            self._notice("先在上面选中一个分组。")
            return
        index = int(selection[0])
        group = self._group_dialog(self.roster_groups[index])
        if group is not None:
            self.roster_groups[index] = group
            self._refresh_groups()

    def delete_group(self) -> None:
        selection = self.group_tree.selection() if hasattr(self, "group_tree") else ()
        if not selection:
            self._notice("先在上面选中一个分组。")
            return
        index = int(selection[0])
        group = self.roster_groups.pop(index)
        self._refresh_groups()
        self._append_log(f"删除分组 {group.name}")

    def _group_dialog(self, group=None):
        """新建 / 编辑一个分组：组名、最少人数、成员（从候选池里勾）。"""
        from .roster import GroupSpec

        if self.roster_book is None:
            try:
                self._load_roster_book()
            except ValueError as exc:
                self._report_error(str(exc))
                return None
        book = self.roster_book
        names = {character.unit_id: character.label for character in book.characters()}
        members = list(group.members if group else ())

        window = tk.Toplevel(self)
        window.title("分组" if group is None else f"分组 {group.name}")
        window.transient(self.master)
        window.columnconfigure(1, weight=1)
        window.rowconfigure(3, weight=1)

        name_var = tk.StringVar(value=group.name if group else "")
        minimum_var = tk.StringVar(value=str(group.minimum if group else 1))
        ttk.Label(window, text="组名").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        name_entry = ttk.Entry(window, textvariable=name_var)
        name_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(8, 2))
        ttk.Label(window, text="最少人数").grid(row=1, column=0, sticky="w", padx=8, pady=2)
        minimum_entry = ttk.Spinbox(window, from_=0, to=5, textvariable=minimum_var, width=6)
        minimum_entry.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=2)
        self._hint(minimum_entry, "每支队伍里这个分组至少上几个人；填 0 等于不限制（成员回到未分组）。")
        ttk.Label(window, text="成员").grid(row=2, column=0, sticky="nw", padx=8, pady=2)
        members_var = tk.StringVar(value="")
        member_label = ttk.Label(window, textvariable=members_var, wraplength=320)
        member_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        def refresh_members() -> None:
            text = "、".join(names.get(int(unit), str(unit)) for unit in members) or "（还没选人）"
            members_var.set(f"{len(members)} 人：{text}")

        def pick_members() -> None:
            picked = self._character_picker(
                title=f"分组 {name_var.get().strip() or '新分组'} 的成员", initial=list(members)
            )
            if picked is None:
                return
            members[:] = picked
            refresh_members()

        choose = ttk.Button(window, text="选择成员…", command=pick_members)
        choose.grid(row=2, column=1, sticky="e", padx=(0, 8))
        self._hint(choose, "和「选择角色…」是同一个窗口：可以按职能 / 属性 / 名字筛，也可以选候选池之外的角色。")
        refresh_members()

        result = {}

        def confirm() -> None:
            try:
                result["group"] = GroupSpec(
                    name=name_var.get().strip(),
                    members=tuple(int(unit) for unit in members),
                    minimum=int(minimum_var.get() or 0),
                )
            except (TypeError, ValueError) as exc:
                self._report_error(str(exc))
                return
            window.destroy()

        actions = ttk.Frame(window)
        actions.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        ttk.Button(actions, text="确定", command=confirm).pack(side="right")
        self._center_window(window, parent=self.master)
        self.wait_window(window)
        return result.get("group")

    # ---------------------------------------------------------------- 预设
    def _presets_path(self):
        from .settings import app_dir

        return app_dir() / "roster_presets.json"

    def _read_presets(self) -> dict:
        import json

        try:
            raw = json.loads(self._presets_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _preset_dialog(self, title: str, choices: list[str] | None = None) -> str | None:
        window = tk.Toplevel(self)
        window.withdraw()
        window.title(title)
        window.transient(self.master)
        window.resizable(False, False)
        body = ttk.Frame(window, padding=(18, 14))
        body.pack(fill="both", expand=True)
        name = tk.StringVar(master=window)
        ttk.Label(body, text="预设名称" if choices is None else "选择预设").pack(anchor="w", pady=(0, 6))
        if choices is None:
            field = ttk.Entry(body, textvariable=name, width=32)
        else:
            field = ttk.Combobox(body, textvariable=name, values=choices, state="readonly", width=32)
            field.current(0)
        field.pack(fill="x")
        result = None

        def confirm(_event=None):
            nonlocal result
            if name.get().strip():
                result = name.get().strip()
                window.destroy()
            return "break"

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(16, 0))
        accept = ttk.Button(actions, text="保存" if choices is None else "载入",
                            style="Primary.TButton", command=confirm)
        accept.pack(side="right")
        ttk.Button(actions, text="取消", command=window.destroy).pack(side="right", padx=(0, 8))
        if choices is None:
            def update_button(*_):
                accept.state(["!disabled"] if name.get().strip() else ["disabled"])
            name.trace_add("write", update_button)
            update_button()
        field.bind("<Return>", confirm)
        window.bind("<Escape>", lambda _: window.destroy())
        self._center_window(window, parent=self.master)
        window.deiconify()
        window.wait_visibility()
        window.grab_set()
        field.focus_set()
        self.wait_window(window)
        return result

    def save_preset(self) -> None:
        import json

        name = self._preset_dialog("保存预设")
        if name is None:
            return
        presets = self._read_presets()
        presets[name] = {
            "pool": [int(unit) for unit in self._pool_ids()],
            "groups": [group.to_dict() for group in self.roster_groups],
        }
        try:
            path = self._presets_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(presets, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            self._report_error(f"预设写不进去：{exc}")
            return
        self._append_log(f"已保存预设：{name}（{path}）")

    def load_preset(self) -> None:
        from .roster import GroupSpec

        presets = self._read_presets()
        if not presets:
            self._notice("还没有保存过预设。")
            return
        name = self._preset_dialog("载入预设", sorted(presets))
        if name is None:
            return
        data = presets[name]
        pool = [int(unit) for unit in data.get("pool") or []]
        self.roster_pool_var.set(",".join(str(unit) for unit in pool))
        self.roster_groups = [
            GroupSpec.from_dict(item) for item in data.get("groups") or [] if isinstance(item, dict)
        ]
        self._refresh_groups()
        self._append_log(f"已载入预设：{name}（{len(pool)} 个候选，{len(self.roster_groups)} 个分组）")

    def choose_characters(self) -> None:
        """候选角色：用通用挑选窗口选完写回候选池。"""
        units = self._character_picker(title="选择角色（候选池）", initial=self._pool_ids())
        if units is None:
            return
        self.roster_pool_var.set(",".join(str(unit) for unit in units))

    def _character_picker(self, *, title: str, initial) -> list[int] | None:
        """通用角色挑选窗口：按职能 / 属性 / 名字筛选 + 图标，返回选中的 unitId 列表。

        「候选角色」和「分组里的成员」都用这一套逻辑：分组可以选任意角色（不需要先在候选池里）。
        """
        if self.roster_book is None:
            try:
                self._load_roster_book()
            except ValueError as exc:
                self._report_error(str(exc))
                return
        book = self.roster_book
        chosen = {int(unit) for unit in initial}
        picked: list[int] | None = None
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self.master)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(3, weight=1)

        search_var = tk.StringVar(value="")
        entry = ttk.Entry(window, textvariable=search_var)
        entry.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        Tooltip(entry, "按名字或 ID 过滤（留空就是全部）。")

        filters = ttk.Frame(window)
        filters.grid(row=1, column=0, sticky="ew", padx=8)
        from .datas import ROLE_LABELS, TALENT_LABELS

        role_var = tk.StringVar(value="全部职能")
        talent_var = tk.StringVar(value="全部属性")
        role_box = ttk.Combobox(
            filters, textvariable=role_var, state="readonly", width=10,
            values=["全部职能", *ROLE_LABELS.values()],
        )
        role_box.pack(side="left")
        Tooltip(role_box, "按职能过滤：攻击型 / 破防型 / 增益型 / 减益型 / 强化型 / 治疗型 / 坦克型 / 干扰型。")
        talent_box = ttk.Combobox(
            filters, textvariable=talent_var, state="readonly", width=8,
            values=["全部属性", *TALENT_LABELS.values()],
        )
        talent_box.pack(side="left", padx=(8, 0))
        Tooltip(talent_box, "按属性过滤：火 / 水 / 风 / 光 / 暗。")
        ttk.Label(
            filters,
            text="按站位排列（攻击范围小的在前）；职能、属性、宽度来自 unit_info.php。",
            style="Muted.TLabel",
        ).pack(side="left", padx=(10, 0))

        holder = ttk.Frame(window)
        holder.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        from .roster import lineup_order

        by_id = {c.unit_id: c for c in book.characters()}
        ordered = [by_id[unit] for unit in lineup_order(tuple(by_id))]

        def request_icons(units):
            if self.icon_busy():
                return False
            self.start_icon_download(units)
            return True

        canvas = CharacterGrid(holder, chosen, self._icons, request_icons,
                               background=self.colors.panel, foreground=self.colors.text, accent=self.colors.violet,
                               empty=self.colors.check_empty)
        bar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        def scrolled(first, last):
            bar.set(first, last)
            canvas.paint()
        canvas.configure(yscrollcommand=scrolled)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")

        def on_wheel(event):
            if event.num in (4, 5):
                steps = -1 if event.num == 4 else 1
            else:
                steps = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
            canvas.yview_scroll(steps * 3, "units")
            return "break"

        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            window.bind(event, on_wheel)

        def render(*_args):
            query = search_var.get().strip().lower()
            role, talent = role_var.get(), talent_var.get()
            canvas.set_rows([c for c in ordered
                             if (role == "全部职能" or c.role_label == role)
                             and (talent == "全部属性" or c.talent_label == talent)
                             and (not query or query in c.label.lower() or query in c.name.lower()
                                  or query in str(c.unit_id))])

        search_var.trace_add("write", render)
        role_box.bind("<<ComboboxSelected>>", render)
        talent_box.bind("<<ComboboxSelected>>", render)
        self._icon_refresh.add(canvas.icons_changed)

        actions = ttk.Frame(window)
        actions.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))

        def confirm() -> None:
            nonlocal picked
            ordered = [int(unit) for unit in initial if int(unit) in chosen]
            ordered += sorted(chosen - set(ordered))
            picked = ordered
            window.destroy()

        ttk.Button(actions, text="清空", command=lambda: (chosen.clear(), render())).pack(side="left")
        ttk.Button(actions, text="确定", command=confirm).pack(side="right")
        render()
        self._center_window(window, parent=self.master)
        entry.focus_set()
        self.wait_window(window)
        self._icon_refresh.discard(canvas.icons_changed)
        return picked

    def show_roster_results(self) -> None:
        """列出伤害最高的 100 支队伍，可以挑一支再打一遍 32 种组合。"""
        if not self.roster_results:
            self._notice("还没有结果：先跑一次阵容搜索。")
            return
        window = tk.Toplevel(self.master)
        window.title("阵容结果（前 100 支）")
        window.geometry("760x420")
        self._center_window(window, parent=self.master)
        columns = ("rank", "team", "operation", "damage")
        tree = ttk.Treeview(window, columns=columns, show="headings", height=14)
        for key, text, width, anchor in (
            ("rank", "#", 46, "center"),
            ("team", "队伍", 320, "w"),
            ("operation", "每位角色的操作（按站位）", 330, "w"),
            ("damage", "伤害", 110, "e"),
        ):
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor=anchor)
        from .roster import lineup_names, lineup_text

        names = self._roster_names()
        for rank, item in enumerate(self.roster_results, start=1):
            members = item.get("team") or []
            flags = item.get("toggles") or []
            text = lineup_names(members, names)
            operation = lineup_text(members, flags, names)
            tree.insert(
                "",
                "end",
                values=(rank, text, operation, f"{int(item.get('damage') or 0):,}"),
            )
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        detail = ttk.Treeview(window, columns=("combo", "damage", "frames", "result"), show="headings", height=10)
        for key, text, width in (
            ("combo", "组合", 140),
            ("damage", "伤害", 140),
            ("frames", "战斗帧", 100),
            ("result", "结果", 100),
        ):
            detail.heading(key, text=text)
            detail.column(key, width=width, anchor="w")

        def probe() -> None:
            selection = tree.selection()
            if not selection:
                self._notice("先在列表里选一支队伍。")
                return
            index = tree.index(selection[0])
            item = self.roster_results[index]
            team = tuple(int(unit) for unit in (item.get("team") or []))
            self._probe_team = team
            if self.session is not None:
                self._notice("等这次跑完再看。")
                return
            # Tk 变量只能在主线程读：worker 里碰界面变量会抛
            # RuntimeError: main thread is not in main loop（以前「点了一行日志就没反应」就是这个）
            try:
                settings = self._roster_settings()
                # 这个按钮就是「32 种都试」，不看「每队试 32 种」那个勾选
                settings = dataclasses.replace(settings, every_combo=True)
                save = self.save
                if save is None:
                    raise ValueError("先打开一条轴（结果里的队伍要照抄轴里的角色属性）。")
                names = self._roster_names()
                shown = lineup_names(team, names)
            except Exception as exc:  # noqa: BLE001 - report to the UI
                self._report_error(f"试这队失败：\n{exc}")
                return
            self.status_var.set(f"正在试这队：{shown}（{2 ** len(team)} 种组合）")
            self._append_log(f"试这队 {2 ** len(team)} 种组合：{shown}")

            def work() -> None:
                import time as _time

                from .roster import lineup_combo, evaluate_team

                began = _time.time()
                try:
                    rows = evaluate_team(save, settings, team, log=self._log)
                    spent = _time.time() - began
                    if rows:
                        self._log(
                            f"{len(rows)} 种组合打完（{spent:.1f} 秒），"
                            f"最高 {int(rows[0][1].damage):,}（{lineup_combo(team, rows[0][0])}）"
                        )
                    self.events.put(("probe", rows))
                except Exception as exc:  # noqa: BLE001 - report to the UI
                    self.events.put(("probe_error", f"{type(exc).__name__}: {exc}"))

            threading.Thread(target=work, daemon=True).start()

        bar = ttk.Frame(window, padding=(8, 0, 8, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="试这队 32 种组合", command=probe).pack(side="left")
        ttk.Button(bar, text="关窗", command=window.destroy).pack(side="right")
        detail.pack(fill="both", expand=True, padx=8)

        def fill(rows) -> None:
            detail.delete(*detail.get_children())
            from .client import result_name
            from .roster import lineup_combo

            probe_team = tuple(int(unit) for unit in (getattr(self, "_probe_team", None) or ()))
            for flags, score in rows:
                detail.insert(
                    "",
                    "end",
                    values=(
                        lineup_combo(probe_team, flags),
                        f"{int(score.damage):,}",
                        int(score.frames),
                        result_name(int(score.result)),
                    ),
                )
            window.lift()

        self._probe_fill = fill
        self._probe_window = window

    def _roster_names(self) -> dict[int, str]:
        names: dict[int, str] = {}
        book = self.roster_book
        pool = []
        try:
            pool = [int(unit) for unit in self._pool_ids()]
        except Exception:  # noqa: BLE001 - 界面还没准备好
            pool = []
        for unit in pool:
            if book is not None:
                try:
                    names[unit] = book.label(unit)
                except Exception:  # noqa: BLE001
                    names[unit] = str(unit)
            else:
                names[unit] = str(unit)
        return names

    def icon_busy(self) -> bool:
        return self._icon_worker is not None and self._icon_worker.is_alive()

    def start_icon_download(self, units) -> None:
        """后台多线程下载图标；进度通过队列回到界面，窗口不会卡。"""
        if self.icon_busy():
            return
        units = [int(unit) for unit in units]
        if not units:
            return
        from . import datas as datas_module

        self._icon_cancel.clear()
        self._icon_total = len(units)
        self.icon_bar.configure(maximum=max(1, len(units)), value=0)
        self.icon_status.configure(text=f"图标：0 / {len(units)}")

        def work() -> None:
            try:
                datas_module.download_icons(
                    units,
                    workers=8,
                    progress=lambda done, total, _unit: self.events.put(("icons", (done, total))),
                    log=self._log,
                    cancel=self._icon_cancel.is_set,
                )
            except Exception as exc:  # noqa: BLE001 - 下载失败不该弄崩界面
                self._log(f"图标下载出错：{exc}")
            finally:
                self.events.put(("icons_done", None))

        self._icon_worker = threading.Thread(target=work, name="icon-download", daemon=True)
        self._icon_worker.start()

    def download_pool_icons(self) -> None:
        units = self._pool_ids()
        if not units:
            self._notice("先选候选角色。")
            return
        self.start_icon_download(units)

    def import_icon_pack(self) -> None:
        """把 icon.zip 解压进图标缓存目录（随发布包附带一份）。"""
        from tkinter import filedialog

        from . import datas as datas_module

        start = Path(self.calculator_var.get().strip() or ".")
        guess = datas_module.find_icon_pack(
            start, Path(__file__).resolve().parents[2], Path.cwd()
        )
        chosen = filedialog.askopenfilename(
            parent=self.master,
            title="选择图标包",
            filetypes=(("图标包", "*.zip"), ("所有文件", "*.*")),
            initialdir=str(guess.parent if guess else start),
            initialfile=guess.name if guess else "icon.zip",
        )
        if not chosen:
            return
        self._icon_cancel.clear()
        self.icon_bar.configure(maximum=1, value=0)
        self.icon_status.configure(text="图标包：正在解压…")
        self._icons.clear()

        def work() -> None:
            try:
                count = datas_module.import_icon_pack(
                    Path(chosen),
                    progress=lambda done, total, _name: self.events.put(("icons", (done, total))),
                    log=self._log,
                )
                self.events.put(("icons_pack", count))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("icons_error", str(exc)))

        self._icon_worker = threading.Thread(target=work, name="icon-pack", daemon=True)
        self._icon_worker.start()

    def _pool_ids(self) -> list[int]:
        raw = self.roster_pool_var.get().replace("，", ",").replace("、", ",")
        units: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                units.append(int(part))
        return list(dict.fromkeys(units))

    def _roster_settings(self):
        from .roster import RosterSettings, GroupSpec

        pool = tuple(self._pool_ids()) or tuple(self.save.unit_ids if self.save is not None else ())
        groups = [GroupSpec.from_dict(group.to_dict()) for group in self.roster_groups]
        return RosterSettings(
            endpoint=endpoint_for(self._port()),
            battle_time=self._number(self.battle_time_var, "战斗秒数", 1, 90),
            seed=self._number(self.seed_var, "战斗种子", -(2 ** 31), 2 ** 31 - 1),
            samples=self._number(self.samples_var, "采样种子数", 1, 100),
            batch_size=self._number(self.batch_size_var, "单次场次", 1, 512),
            timeout=float(self._number(self.timeout_var, "超时秒", 1, 3600, integer=False)),
            pool=pool,
            groups=tuple(groups),
            to_max_attr=bool(self.to_max_attr_var.get()),
            every_combo=bool(self.every_combo_var.get()),
            verify=bool(self.verify_var.get()),
        )

    def _reset_roster_extras(self) -> None:
        """新载入轴时把分组清掉（分组是跟着候选池走的）。"""
        self.roster_groups = []
        self._refresh_groups()

    def _load_roster_book(self):
        """加载角色表（名字 + 职能 / 属性 / 站位宽度）；失败就抛中文错误。"""
        from . import datas

        folder = self.calculator_var.get().strip() or "."
        info = datas.load_unit_info(log=self._append_log)  # 没缓存时在这里抓一次
        book = datas.open_book(Path(folder), info=info)
        if book is not None and not getattr(self, "_info_refreshed", False):
            missing = book.units_without_info()
            if missing:
                names = "、".join(book.label(unit) for unit in missing[:5])
                self._append_log(
                    f"有 {len(missing)} 个角色在缓存的资料里查不到（{names}"
                    f"{' 等' if len(missing) > 5 else ''}），重新抓一次 unit_info.php …"
                )
                info = datas.load_unit_info(refresh=True, log=self._append_log)
                book = datas.open_book(Path(folder), info=info)
            self._info_refreshed = True
        if book is None:
            raise ValueError(
                "在 PCRCalculator 目录里找不到 StreamingAssets/Datas 下的角色表（chara_name.json 等）。\n"
                "请把「PCRCalculator 目录」指到安装目录（例如 B:\\IDM\\script\\mzq\\2.11.0）。"
            )
        self.roster_book = book
        if self.roster_groups:
            self._refresh_groups()
        self._append_log(
            f"角色表：{len(book.characters())} 个角色（职能 / 属性 / 宽度 {len(book.info)} 条）"
        )
        return book

    def _apply_roster_settings(self, settings) -> None:
        self.roster_pool_var.set(",".join(str(unit) for unit in settings.pool))
        self.roster_groups = [group for group in settings.groups]
        self._refresh_groups()
        self.to_max_attr_var.set(bool(settings.to_max_attr))
        self.every_combo_var.set(bool(settings.every_combo))
        self.verify_var.set(bool(settings.verify))
        self.battle_time_var.set(str(settings.battle_time))
        self.seed_var.set(str(settings.seed))
        self.samples_var.set(str(settings.samples))
        self.batch_size_var.set(str(settings.batch_size))

    def _build_items(self) -> None:
        frame = ttk.LabelFrame(self.output, text="可调项", padding=8)
        self.output.add(frame, text="时间轴")
        self.items_frame = frame
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        bar = ttk.Frame(frame)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.item_controls = bar
        ttk.Button(bar, text="全选", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(bar, text="全不选", command=lambda: self.set_all(False)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="反选", command=self.invert).pack(side="left", padx=(6, 0))
        only = ttk.Checkbutton(bar, text="只调选中的项", variable=self.only_checked_var)
        only.pack(side="left", padx=(12, 0))
        self._hint(only, "勾上后只调表格里打了勾的项，按顺序一个一个来。")
        self.items_count = ttk.Label(bar, text="")
        self.items_count.pack(side="right")

        self.items_tree = ttk.Treeview(
            frame, columns=("check", "label", "frame"), show="headings", height=7, selectmode="browse",
        )
        self.items_tree.heading("check", text="选")
        self.items_tree.heading("label", text="角色 / UB", anchor="w")
        self.items_tree.tag_configure("alternate", background=self.colors.alternate)
        self.items_tree.heading("frame", text="当前帧")
        self.items_tree.column("check", width=44, anchor="center", stretch=False)
        self.items_tree.column("label", width=240)
        self.items_tree.column("frame", width=90, anchor="e", stretch=False)
        self.items_tree.grid(row=1, column=0, sticky="nsew")
        self._hint(self.items_tree, "每个可调项是一格 UB 帧。点一行或按空格切换选中，帧号是当前值。")
        bar_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.items_tree.yview)
        bar_scroll.grid(row=1, column=1, sticky="ns")
        self.items_tree.configure(yscrollcommand=bar_scroll.set)
        self.items_tree.bind("<Button-1>", self._click_item)
        self.items_tree.bind("<space>", self._toggle_focused)

    def _build_actions(self) -> None:
        bar = ttk.Frame(self, padding=(12, 8))
        bar.grid(row=2, column=0, sticky="ew")
        self.start_button = ttk.Button(bar, text="开始调帧", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self._hint(
            self.start_button,
            "按当前设置逐项搜索最优帧。搜索期间只在内存里累积，全部结束后才写回一次；"
            "中途停止不会改动存档。",
        )
        self.stop_button = ttk.Button(bar, text="停止", command=self.stop)
        self.stop_button.pack(side="left", padx=(6, 0))
        self._hint(self.stop_button, "收尾并丢弃本次结果（不写回任何帧），已完成项留在 state.json 里可以续跑。")
        resume_button = ttk.Button(bar, text="续跑…", command=self.resume)
        resume_button.pack(side="left", padx=(12, 0))
        self._hint(resume_button, "挑一个中断的运行目录，从 state.json 记录的进度接着调。")
        self.open_dir_button = ttk.Button(bar, text="打开运行目录", command=self.open_run_dir)
        self.open_dir_button.pack(side="left", padx=(12, 0))
        self._hint(self.open_dir_button, "打开本次运行的目录（日志、报告、结果轴都在里面）。")
        self.open_report_button = ttk.Button(bar, text="打开报告", command=self.open_report)
        self.open_report_button.pack(side="left", padx=(6, 0))
        self._hint(self.open_report_button, "打开本次运行的 报告.md。")
        self.clock = tk.Canvas(bar, width=46, height=34, background=self.colors.panel, cursor="hand2",
                               takefocus=True, highlightthickness=1, highlightbackground=self.colors.panel,
                               highlightcolor=self.colors.violet)
        self.clock.pack(side="right")
        for event in ("<Button-1>", "<space>", "<Return>"):
            self.clock.bind(event, self._toggle_theme)
        self._hint(self.clock, "点击时钟切换白天和黑夜主题。")
        self._tick_clock()

        progress_row = ttk.Frame(self, padding=(12, 2, 12, 8))
        progress_row.grid(row=3, column=0, sticky="ew")
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.pack(side="right", fill="x", expand=True)
        self._hint(self.progress, "已完成的可调项数 / 总项数。")
        self.counter_label = ttk.Label(progress_row, text="写回 0 / 跳过 0 / 失败 0 / 场次 0")
        self.counter_label.pack(side="left", padx=(0, 12))
        self._hint(self.counter_label, "写回=已采纳的最优帧数，场次=实际打了多少场模拟。")

    def _draw_clock(self, now):
        dial, colors = self.clock, self.colors
        dial.delete("all")
        dial.configure(background=colors.panel, highlightbackground=colors.panel, highlightcolor=colors.violet)
        dial.create_oval(3, 4, 29, 30, fill=colors.field, outline=colors.violet, width=2)
        for angle, length in ((now.hour * 30 + now.minute / 2, 7), (now.minute * 6, 10)):
            radians = math.radians(angle)
            dial.create_line(16, 17, 16 + length * math.sin(radians), 17 - length * math.cos(radians),
                             fill=colors.text, width=2)
        if colors is LIGHT:
            dial.create_oval(32, 6, 40, 14, fill="#a15b07", outline="")
            for degree in range(0, 360, 45):
                x, y = math.cos(math.radians(degree)), math.sin(math.radians(degree))
                dial.create_line(36+6*x, 10+6*y, 36+8*x, 10+8*y, fill="#a15b07")
        else:
            dial.create_oval(32, 4, 44, 16, fill=colors.violet, outline="")
            dial.create_oval(36, 2, 46, 12, fill=colors.panel, outline="")

    def _tick_clock(self):
        now = datetime.datetime.now()
        self._draw_clock(now)
        self.after(60000 - now.second * 1000 - now.microsecond // 1000, self._tick_clock)

    def _toggle_theme(self, _event=None):
        self.colors = colors = LIGHT if self.colors is DARK else DARK
        _workspace_theme(self.master, colors)
        self._draw_clock(datetime.datetime.now())
        self._draw_axis_overview()
        self.items_tree.tag_configure("alternate", background=colors.alternate)
        self.log_text.configure(background=colors.field, foreground=colors.text, insertbackground=colors.cyan,
                                selectbackground=colors.selection, selectforeground=colors.text)
        for view in self.scroll_views.values():
            view.configure(background=colors.panel)
        widgets = list(self.master.winfo_children())
        while widgets:
            widget = widgets.pop()
            widgets.extend(widget.winfo_children())
            if isinstance(widget, CharacterGrid):
                widget.set_colors(colors.panel, colors.text, colors.violet, colors.check_empty)
            elif isinstance(widget, tk.Toplevel):
                widget.configure(background=colors.panel)
            elif isinstance(widget, ttk.Combobox):
                # Tk 的下拉列表首次展开后就保留，已有列表须与新列表一起换色。
                listing = f"{widget}.popdown.f.l"
                if int(widget.tk.call("winfo", "exists", listing)):
                    widget.tk.call(listing, "configure", "-background", colors.panel, "-foreground", colors.text,
                                   "-selectbackground", colors.selection, "-selectforeground", colors.violet)
        self.save_settings()
        return "break"

    def _build_output(self) -> None:
        notebook = self.output

        log_tab = ttk.Frame(notebook, padding=2)
        log_tab.rowconfigure(0, weight=1)
        log_tab.columnconfigure(0, weight=1)
        self.log_tab = log_tab
        self.log_text = tk.Text(log_tab, width=30, height=10, wrap="word", state="disabled",
                                foreground=self.colors.text, background=self.colors.field, insertbackground=self.colors.cyan,
                                selectforeground=self.colors.text, selectbackground=self.colors.selection, relief="flat", padx=10, pady=8,
                                spacing1=2, spacing3=2, font="TkDefaultFont", highlightthickness=0)
        log_scroll = ttk.Scrollbar(log_tab, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        notebook.add(log_tab, text="日志")

        result_tab = ttk.Frame(notebook, padding=2)
        result_tab.rowconfigure(0, weight=1)
        result_tab.columnconfigure(0, weight=1)
        columns = ("label", "pass", "frames", "decision", "gain", "battles")
        self.result_tree = ttk.Treeview(result_tab, columns=columns, show="headings", height=12)
        for column, heading, width, anchor in (
            ("label", "项", 220, "w"),
            ("pass", "轮", 46, "center"),
            ("frames", "原帧 → 新帧", 130, "center"),
            ("decision", "决策", 120, "center"),
            ("gain", "期望伤害（基线 → 本项）", 210, "e"),
            ("battles", "场次", 70, "e"),
        ):
            self.result_tree.heading(column, text=heading)
            self.result_tree.column(column, width=width, minwidth=36, stretch=False, anchor=anchor)
        self._enable_column_resize(self.result_tree)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(result_tab, orient="vertical", command=self.result_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=scroll.set)
        xscroll = ttk.Scrollbar(result_tab, orient="horizontal", command=self.result_tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.result_tree.configure(xscrollcommand=xscroll.set)
        notebook.add(result_tab, text="结果")

        status = ttk.Label(self, textvariable=self.status_var, anchor="w", style="Status.TLabel")
        status.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self._hint(status, "当前状态：就绪 / 运行中 / 完成 / 出错。")

    @staticmethod
    def _enable_column_resize(tree):
        # Tk 默认的 separator 拖动会从后面的列借宽度，整表因此无法重新拉宽。
        drag = None
        def begin(event):
            nonlocal drag
            if tree.identify_region(event.x, event.y) == "separator":
                column = tree.identify_column(event.x)
                drag = (column, event.x_root, tree.column(column, "width"))
                return "break"

        def resize(event):
            if drag:
                column, origin, width = drag
                tree.column(column, width=max(tree.column(column, "minwidth"), width + event.x_root - origin))
                return "break"

        def finish(event):
            nonlocal drag
            if drag:
                resize(event)
                drag = None
                return "break"

        tree.bind("<ButtonPress-1>", begin)
        tree.bind("<B1-Motion>", resize)
        tree.bind("<ButtonRelease-1>", finish)

    def _result_columns(self, mode):
        names = {
            "frame": ("可调项", "轮次", "原帧 → 新帧", "决策", "期望伤害（基线 → 本项）", "场次"),
            "ga": ("迭代", "代数", "", "刷新记录", "最高期望伤害", "场次"),
            "anneal": ("迭代", "步数", "", "温度", "最高期望伤害", "场次"),
            "cycle": ("模式", "层数", "", "记录", "最高期望伤害", "场次"),
            "roster": ("队伍", "进度", "角色", "AUTO / SET", "最高期望伤害", "场次"),
            "zero": ("进度", "阵容", "逐刀操作", "", "最高期望伤害", "场次"),
        }[mode]
        columns = self.result_tree["columns"]
        for col, title in zip(columns, names):
            self.result_tree.heading(col, text=title)
        self.result_tree.configure(displaycolumns=[col for col, title in zip(columns, names) if title])

    # ------------------------------------------------------------------ items

    def rebuild_targets(self) -> None:
        if self.save is None:
            return
        self.targets = build_targets(
            self.save.frames, self.save.unit_ids,
            names=self.save.names, boss_name=self.save.boss_name,
            include_boss=self.include_boss_var.get(),
        )
        self.selection = {target.key for target in self.targets}
        self.refresh_items()

    def refresh_items(self) -> None:
        selected = self.items_tree.selection()
        self.items_tree.delete(*self.items_tree.get_children())
        for index, target in enumerate(self.targets):
            mark = CHECK_ON if target.key in self.selection else CHECK_OFF
            self.items_tree.insert("", "end", iid=target.key, values=(mark, target.label, text(target.raw)),
                                   tags=("alternate",) if index % 2 else ())
        if selected:
            keep = [iid for iid in selected if iid in self.selection]
            if keep:
                self.items_tree.selection_set(keep)
        self.items_count.configure(text=f"{len(self.selection)} / {len(self.targets)} 项选中")

    def _click_item(self, event: tk.Event) -> None:
        iid = self.items_tree.identify_row(event.y)
        if iid and self._mode() == "frame":
            self._toggle(iid)

    def _toggle_focused(self, _event: tk.Event) -> str:
        if self._mode() != "frame":
            return "break"
        for iid in self.items_tree.selection():
            self._toggle(iid)
        return "break"

    def _toggle(self, iid: str) -> None:
        if iid in self.selection:
            self.selection.discard(iid)
            mark = CHECK_OFF
        else:
            self.selection.add(iid)
            mark = CHECK_ON
        values = list(self.items_tree.item(iid, "values"))
        values[0] = mark
        self.items_tree.item(iid, values=values)
        self.items_count.configure(text=f"{len(self.selection)} / {len(self.targets)} 项选中")

    def set_all(self, chosen: bool) -> None:
        self.selection = {target.key for target in self.targets} if chosen else set()
        self.refresh_items()

    def invert(self) -> None:
        everything = {target.key for target in self.targets}
        self.selection = everything - self.selection
        self.refresh_items()

    # -------------------------------------------------------------------- axis

    def ask_axis(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 PCRCalculator 导出的轴",
            initialdir=str(self.axis_dir),
            filetypes=[("Excel 轴", "*.xlsx"), ("所有文件", "*.*")],
        )
        if path:
            self.open_axis(Path(path))

    def ask_calculator(self) -> None:
        folder = filedialog.askdirectory(
            title="选择 PCRCalculator 目录（或它里面的 server 子目录）",
            initialdir=self.calculator_var.get() or str(self.axis_dir),
        )
        if not folder:
            return
        self.calculator_var.set(folder)
        exe = find_server(folder)
        if exe is None:
            self._report_error("这个目录里没找到 PCRCalculatorServer.exe。\n"
                "请选 PCRCalculator 的安装目录，或它里面的 server 子目录。")
            return
        self.status_var.set(f"摸轴器：{exe}")

    def open_axis(self, path: Path, *, keep_root: bool = False) -> None:
        try:
            # 语义 GA 也能吃「只有语义数据」的轴，所以这里不强求帧轴；
            # 切到逐帧搜索时再检查有没有帧。
            save = read_save(path, require_frames=False, allow_empty=True, log=self._append_log)
        except (ValueError, OSError) as exc:
            self._report_error(f"打不开这条轴：\n{exc}")
            self.status_var.set("轴文件读取失败")
            return
        self.save = save
        self.axis_dir = save.path.parent
        if not keep_root or self.output_root is None:
            self.output_root = save.path.parent
        self.axis_var.set(str(save.path))
        self.axis_title.set(save.path.name)
        self._draw_axis_overview()
        self.axis_tip.message = str(save.path)
        duration = save.suggested_duration(save.header)
        self.battle_time_var.set(str(duration))
        self.seed_var.set(str(save.battle_seed if save.battle_seed is not None else 666))
        self.axis_info.set(
            f"{'、'.join(save.unit_names)}　｜　{save.boss_label}"
            f"｜建议 {duration} 秒｜种子 {save.battle_seed if save.battle_seed is not None else '未指定'}"
        )
        self.rebuild_targets()
        self._sync_profile_to_axis()
        self.status_var.set(f"已载入 {save.path.name}：{len(self.targets)} 个可调项")
        self._append_log(f"载入轴 {save.path}")
        self._set_running(False)

    def _sync_profile_to_axis(self) -> None:
        """按轴里实际有的东西纠正「序列类型」。

        轴里没有角色序列（``SemanUBSequenceData`` 为空）时，选「同时调角色序列」会让
        摸轴器按角色序列去解释基因，整条 UB 排程都变、伤害跟帧轴对不上，所以这里直接
        退回「只调五条 UB 序列」。
        """
        save = self.save
        if save is None:
            return
        if not save.has_semantic:
            return
        if save.sequence:
            return
        label = SEQUENCE_TYPE_LABELS[SemanSequenceType.LANES]
        if self.sequence_type_var.get() != label:
            self.sequence_type_var.set(label)
            self._append_log("提示：这条轴里没有角色序列（SemanUBSequenceData），序列类型已改成只调五条 UB 序列")

    # ---------------------------------------------------------------- settings

    def _number(self, var: tk.StringVar, name: str, low: float, high: float, *, integer: bool = True):
        raw = var.get().strip()
        try:
            value = int(raw) if integer else float(raw)
        except ValueError:
            raise ValueError(f"{name} 必须是{'整数' if integer else '数字'}（现在是 {raw!r}）") from None
        if not low <= value <= high:
            raise ValueError(f"{name} 必须在 {low:g} ~ {high:g} 之间（现在是 {value:g}）")
        return value

    def _port(self) -> int:
        return self._number(self.port_var, "端口", 1, 65535)

    def _job_settings(self) -> JobSettings:
        return JobSettings(
            endpoint=endpoint_for(self._port()),
            battle_time=self._number(self.battle_time_var, "战斗秒数", 1, 90),
            seed=self._number(self.seed_var, "战斗种子", -(2 ** 31), 2 ** 31 - 1),
            passes=self._number(self.passes_var, "循环轮数", 1, 20),
            samples=self._number(self.samples_var, "采样种子数", 1, 100),
            batch_size=self._number(self.batch_size_var, "单次场次", 1, 512),
            timeout=float(self._number(self.timeout_var, "超时秒", 1, 3600, integer=False)),
            calc_stats=self._number(self.calc_stats_var, "CalcStats", 0, 511),
            include_boss=self.include_boss_var.get(),
            items=self._items_filter(),
            verify=self.verify_var.get(),
        )

    def _items_filter(self) -> frozenset | None:
        """JobSettings wants one-based ``(slot, ub)`` pairs; target keys are zero-based."""
        if not self.only_checked_var.get():
            return None
        keys = set()
        for key in self.selection:
            row, index = key.split(":")
            keys.add((int(row) + 1, int(index) + 1))
        return frozenset(keys)

    def _search_settings(self) -> SearchSettings:
        return SearchSettings(
            step=self._number(self.step_var, "步长", 1, 60),
            max_offset=self._number(self.max_offset_var, "最大偏移", 1, 2000),
            outer_probes=self._number(self.outer_probes_var, "外探次数", 0, 50),
            threshold_mode=ThresholdMode.parse(self.threshold_mode_var.get()),
            threshold_value=float(self._number(self.threshold_var, "阈值", 0, 10 ** 9, integer=False)),
            peak_tolerance=float(self._number(self.peak_tolerance_var, "峰值容差", 0, 10 ** 6, integer=False)),
            chunk=self._number(self.chunk_var, "每批帧数", 1, 64),
        )

    def _out_root(self) -> Path:
        return (self.output_root or self.axis_dir) / "runs"

    def _load_result_axis(self, path: Path) -> None:
        """跑完之后把刚生成的结果轴装进来，方便直接再点一次接着调。"""
        previous = self.status_var.get()
        self.open_axis(path, keep_root=True)
        if self.save is None or Path(self.save.path) != Path(path):
            return
        self._append_log(f"已自动载入本次结果：{path}（可以直接再点一次开始）")
        self.status_var.set(f"{previous}；已自动载入结果轴，可以直接再点一次")

    def _mode(self) -> str:
        label = self.mode_var.get()
        for value, text_ in MODES:
            if text_ == label:
                return value
        return mode_registry.FRAME

    def _mode_spec(self):
        return mode_registry.get(self._mode())

    def _ga_settings(self) -> GaSettings:
        return GaSettings(
            endpoint=endpoint_for(self._port()),
            battle_time=self._number(self.battle_time_var, "战斗秒数", 1, 90),
            seed=self._number(self.seed_var, "战斗种子", -(2 ** 31), 2 ** 31 - 1),
            samples=self._number(self.samples_var, "采样种子数", 1, 100),
            batch_size=self._number(self.batch_size_var, "单次场次", 1, 512),
            timeout=float(self._number(self.timeout_var, "超时秒", 1, 3600, integer=False)),
            population=self._number(self.population_var, "种群", 2, 512),
            generations=self._number(self.generations_var, "代数", 1, 100000),
            islands=self._number(self.islands_var, "岛屿", 1, 64),
            crossover=float(self._number(self.crossover_var, "交叉率", 0, 1, integer=False)),
            mutation=float(self._number(self.mutation_var, "突变率", 0, 1, integer=False)),
        elite_count=self._number(self.elite_var, "精英保留", 0, 64),
        mutation_decay=float(self._number(self.mutation_decay_var, "突变衰减", 0, 1, integer=False)),
        mutation_floor=float(self._number(self.mutation_floor_var, "突变下限", 0, 1, integer=False)),
        stagnation_limit=self._number(self.stagnation_var, "停滞停止", 0, 100000),
        restarts=self._number(self.restarts_var, "重启轮数", 1, 20),
            migration_interval=self._number(self.migration_var, "迁移间隔", 1, 10000),
            migrants_ratio=float(self._number(self.migrants_ratio_var, "迁移比例", 0, 1, integer=False)),
            disaster_patience=self._number(self.disaster_var, "灾变间隔", 1, 100000),
            max_lane_length=self._number(self.max_lane_var, "单行上限", 1, 4096),
            fingerprint_distance=float(
                self._number(self.fingerprint_var, "指纹容差", 0, 10 ** 6, integer=False)
            ),
            ga_seed=self._number(self.ga_seed_var, "GA 种子", 0, 2 ** 31 - 1),
            delay=self._number(self.delay_var, "退避帧数", 1, 600),
            ub_type=_label_value(UB_TYPE_LABELS, self.ub_type_var.get()),
            skill_type=_label_value(SKILL_TYPE_LABELS, self.skill_type_var.get()),
            sequence_type=_label_value(SEQUENCE_TYPE_LABELS, self.sequence_type_var.get()),
            legacy_ub=bool(self.legacy_ub_var.get()),
            own_skill=bool(self.own_skill_var.get()),
            other_skill=bool(self.other_skill_var.get()),
            verify=self.verify_var.get(),
        )

    def _anneal_settings(self):
        from .anneal import AnnealSettings

        return AnnealSettings(
            endpoint=endpoint_for(self._port()),
            battle_time=self._number(self.battle_time_var, "战斗秒数", 1, 90),
            seed=self._number(self.seed_var, "战斗种子", -(2 ** 31), 2 ** 31 - 1),
            samples=self._number(self.samples_var, "采样种子数", 1, 100),
            batch_size=self._number(self.batch_size_var, "单次场次", 1, 512),
            timeout=float(self._number(self.timeout_var, "超时秒", 1, 3600, integer=False)),
            iterations=self._number(self.iterations_var, "迭代次数", 1, 1000000),
            initial_temperature=float(
                self._number(self.initial_temperature_var, "初始温度", 0.00001, 1, integer=False)
            ),
            final_temperature=float(
                self._number(self.final_temperature_var, "最低温度", 0, 1, integer=False)
            ),
            chain_range=self._number(self.chain_range_var, "链长上限", 1, 96),
            swap_chance=float(self._number(self.swap_chance_var, "单点替换", 0, 1, integer=False)),
            double_swap_chance=float(
                self._number(self.double_chance_var, "双点互换", 0, 1, integer=False)
            ),
            chain_chance=float(self._number(self.chain_chance_var, "链式替换", 0, 1, integer=False)),
            manual_chance=float(self._number(self.manual_chance_var, "手动指定", 0, 1, integer=False)),
            allow_duplicates=bool(self.allow_duplicates_var.get()),
            restarts=self._number(self.anneal_restarts_var, "重启轮数", 0, 10000),
            report_every=self._number(self.report_every_var, "日志间隔", 1, 10000),
            fingerprint_distance=float(
                self._number(self.fingerprint_var, "指纹容差", 0, 10 ** 6, integer=False)
            ),
            delay=self._number(self.anneal_delay_var, "退避帧数", 1, 600),
            ub_type=_label_value(UB_TYPE_LABELS, self.ub_type_var.get()),
            skill_type=_label_value(SKILL_TYPE_LABELS, self.skill_type_var.get()),
            sequence_type=_label_value(SEQUENCE_TYPE_LABELS, self.sequence_type_var.get()),
            legacy_ub=bool(self.legacy_ub_var.get()),
            own_skill=bool(self.own_skill_var.get()),
            other_skill=bool(self.other_skill_var.get()),
            verify=self.verify_var.get(),
        )

    def _sync_mode(self) -> None:
        key = self._mode()
        previous = getattr(self, "_shown_mode", None)
        if previous == key:
            return
        if previous is not None:
            self.mode_pages[previous].grid_remove()
        self.mode_pages[key].grid(row=0, column=0, sticky="ew")
        self._shown_mode = key
        if self.session is None and not self.result_tree.get_children():
            self._result_columns(key)
        self.scroll_views[self.tab_mode].yview_moveto(0)
        self.settings_tabs.select(self.tab_mode)
        if key == "frame":
            self.item_controls.grid()
            self.items_tree.configure(displaycolumns=("check", "label", "frame"))
        elif previous in (None, "frame"):
            self.item_controls.grid_remove()
            self.items_tree.configure(displaycolumns=("label", "frame"))
        self.items_frame.configure(text="可调项（点击选择）" if key == "frame" else "原轴出手顺序")
        actions = {"frame": "开始调帧", "ga": "开始优化", "anneal": "开始退火", "roster": "开始阵容搜索",
                   "zero": "开始 0 押", "cycle": "开始循环搜索"}
        self.start_button.configure(text=actions[key])

    # ------------------------------------------------------- settings storage

    def _collect_settings(self) -> dict:
        return {
            "calculator": self.calculator_var.get().strip(),
            "port": self.port_var.get().strip(),
            "autostart": bool(self.autostart_var.get()),
            "excel": self.axis_var.get().strip(),
            "mode": self._mode(),
            "cycle": {k: v.get() for k, v in self.cycle_vars.items()},
            "job": {
                "battle_time": self.battle_time_var.get().strip(),
                "seed": self.seed_var.get().strip(),
                "passes": self.passes_var.get().strip(),
                "samples": self.samples_var.get().strip(),
                "batch_size": self.batch_size_var.get().strip(),
                "timeout": self.timeout_var.get().strip(),
                "calc_stats": self.calc_stats_var.get().strip(),
                "include_boss": bool(self.include_boss_var.get()),
                "verify": bool(self.verify_var.get()),
                "only_checked": bool(self.only_checked_var.get()),
            },
            "search": {
                "step": self.step_var.get().strip(),
                "max_offset": self.max_offset_var.get().strip(),
                "outer_probes": self.outer_probes_var.get().strip(),
                "threshold": self.threshold_var.get().strip(),
                "threshold_mode": self.threshold_mode_var.get().strip(),
                "peak_tolerance": self.peak_tolerance_var.get().strip(),
                "chunk": self.chunk_var.get().strip(),
            },
            "ga": {
                "population": self.population_var.get().strip(),
                "generations": self.generations_var.get().strip(),
                "islands": self.islands_var.get().strip(),
                "crossover": self.crossover_var.get().strip(),
                "mutation": self.mutation_var.get().strip(),
            "elite_count": self.elite_var.get().strip(),
            "mutation_decay": self.mutation_decay_var.get().strip(),
            "mutation_floor": self.mutation_floor_var.get().strip(),
            "stagnation_limit": self.stagnation_var.get().strip(),
            "restarts": self.restarts_var.get().strip(),
                "migration_interval": self.migration_var.get().strip(),
                "migrants_ratio": self.migrants_ratio_var.get().strip(),
                "disaster_patience": self.disaster_var.get().strip(),
                "max_lane_length": self.max_lane_var.get().strip(),
                "fingerprint_distance": self.fingerprint_var.get().strip(),
                "ga_seed": self.ga_seed_var.get().strip(),
                "delay": self.delay_var.get().strip(),
                "ub_type": self.ub_type_var.get().strip(),
                "skill_type": self.skill_type_var.get().strip(),
                "sequence_type": self.sequence_type_var.get().strip(),
                "legacy_ub": bool(self.legacy_ub_var.get()),
                "own_skill": bool(self.own_skill_var.get()),
                "other_skill": bool(self.other_skill_var.get()),
            },
            "anneal": {
                "iterations": self.iterations_var.get().strip(),
                "initial_temperature": self.initial_temperature_var.get().strip(),
                "final_temperature": self.final_temperature_var.get().strip(),
                "chain_range": self.chain_range_var.get().strip(),
                "swap_chance": self.swap_chance_var.get().strip(),
                "double_swap_chance": self.double_chance_var.get().strip(),
                "chain_chance": self.chain_chance_var.get().strip(),
                "manual_chance": self.manual_chance_var.get().strip(),
                "allow_duplicates": bool(self.allow_duplicates_var.get()),
                "restarts": self.anneal_restarts_var.get().strip(),
                "report_every": self.report_every_var.get().strip(),
                "delay": self.anneal_delay_var.get().strip(),
            },
            "roster": {
            "pool": [int(unit) for unit in self._pool_ids()],
            "groups": [group.to_dict() for group in self.roster_groups],
            "to_max_attr": bool(self.to_max_attr_var.get()),
            "every_combo": bool(self.every_combo_var.get()),
            "verify": bool(self.verify_var.get()),
        },
            "zero": {
                "iterations": self.zero_iterations_var.get().strip(),
                "batch": self.zero_batch_var.get().strip(),
                "ub_limit": self.zero_ub_limit_var.get().strip(),
            },
            "selection": sorted(self.selection),
            "theme": "light" if self.colors is LIGHT else "dark",
            "geometry": self.master.geometry() if self.master.winfo_exists() else "",
        }

    def save_settings(self) -> None:
        write_settings(self._collect_settings())

    def apply_saved_geometry(self) -> None:
        """Re-open at the size/position the window had last time (clamped to the screen)."""
        geometry = str(read_settings().get("geometry") or "")
        if not geometry or "x" not in geometry:
            return
        try:
            size, _, rest = geometry.partition("+")
            width_text, _, height_text = size.partition("x")
            width = int(width_text or 0)
            height = int(height_text or 0)
            max_width = max(880, self.master.winfo_screenwidth() - 80)
            max_height = max(620, self.master.winfo_screenheight() - 90)
            width = max(880, min(width, max_width))
            height = max(620, min(height, max_height))
            position = ""
            if rest:
                left_text, _, top_text = rest.partition("+")
                left, top = int(left_text or 0), int(top_text or 0)
                if 0 <= left <= max_width - 120 and 0 <= top <= max_height - 80:
                    position = f"+{left}+{top}"
            self.master.geometry(f"{width}x{height}{position}")
        except (tk.TclError, ValueError):
            pass

    def _restore_settings(self, data) -> None:
        if not data:
            return
        self.calculator_var.set(str(data.get("calculator") or ""))
        port = str(data.get("port") or DEFAULT_PORT)
        self.port_var.set(port)
        self.autostart_var.set(bool(data.get("autostart", True)))
        mode = str(data.get("mode") or mode_registry.FRAME)
        self.mode_var.set(dict(MODES).get(mode, MODES[0][1]))
        self._sync_mode()
        for k, v in self.cycle_vars.items():
            v.set((data.get("cycle") or {}).get(k, v.get()))
        ga = data.get("ga") or {}
        for var, key in (
            (self.population_var, "population"),
            (self.generations_var, "generations"),
            (self.islands_var, "islands"),
            (self.crossover_var, "crossover"),
            (self.mutation_var, "mutation"),
            (self.elite_var, "elite_count"),
            (self.mutation_decay_var, "mutation_decay"),
            (self.mutation_floor_var, "mutation_floor"),
            (self.stagnation_var, "stagnation_limit"),
            (self.restarts_var, "restarts"),
            (self.migration_var, "migration_interval"),
            (self.migrants_ratio_var, "migrants_ratio"),
            (self.disaster_var, "disaster_patience"),
            (self.max_lane_var, "max_lane_length"),
            (self.fingerprint_var, "fingerprint_distance"),
            (self.ga_seed_var, "ga_seed"),
            (self.delay_var, "delay"),
        ):
            value = ga.get(key)
            if value not in (None, ""):
                var.set(str(value))
        for var, key, labels in (
            (self.ub_type_var, "ub_type", UB_TYPE_LABELS),
            (self.skill_type_var, "skill_type", SKILL_TYPE_LABELS),
            (self.sequence_type_var, "sequence_type", SEQUENCE_TYPE_LABELS),
        ):
            saved = ga.get(key)
            if saved:
                for value, label in labels.items():
                    if saved in (label, value.name, str(value.value)):
                        var.set(label)
                        break
        for var, key in (
            (self.legacy_ub_var, "legacy_ub"),
            (self.own_skill_var, "own_skill"),
            (self.other_skill_var, "other_skill"),
        ):
            if key in ga:
                var.set(bool(ga[key]))

        anneal = data.get("anneal") or {}
        for var, key in (
            (self.iterations_var, "iterations"),
            (self.initial_temperature_var, "initial_temperature"),
            (self.final_temperature_var, "final_temperature"),
            (self.chain_range_var, "chain_range"),
            (self.swap_chance_var, "swap_chance"),
            (self.double_chance_var, "double_swap_chance"),
            (self.chain_chance_var, "chain_chance"),
            (self.manual_chance_var, "manual_chance"),
            (self.anneal_restarts_var, "restarts"),
            (self.report_every_var, "report_every"),
            (self.anneal_delay_var, "delay"),
        ):
            value = anneal.get(key)
            if value not in (None, ""):
                var.set(str(value))
        if "allow_duplicates" in anneal:
            self.allow_duplicates_var.set(bool(anneal["allow_duplicates"]))
        roster = data.get("roster")
        if isinstance(roster, dict):
            from .roster import GroupSpec

            raw_pool = roster.get("pool") or []
            if isinstance(raw_pool, str):
                raw_pool = [part for part in raw_pool.replace("，", ",").split(",") if part.strip()]
            pool = [int(unit) for unit in raw_pool if str(unit).strip().isdigit()]
            if pool:
                self.roster_pool_var.set(",".join(str(unit) for unit in pool))
            self.roster_groups = [
                GroupSpec.from_dict(item)
                for item in roster.get("groups") or []
                if isinstance(item, dict)
            ]
            self.to_max_attr_var.set(bool(roster.get("to_max_attr", True)))
            self.every_combo_var.set(bool(roster.get("every_combo", True)))
            self.verify_var.set(bool(roster.get("verify", True)))
            self._refresh_groups()  # 上次保存的分组要立刻显示出来（不然得先「新建」一次才看得见）
        zero = data.get("zero")
        if isinstance(zero, dict):
            for key, var in (
                ("iterations", self.zero_iterations_var),
                ("batch", self.zero_batch_var),
                ("ub_limit", self.zero_ub_limit_var),
            ):
                value = zero.get(key)
                if value not in (None, ""):
                    var.set(str(value))

    # -------------------------------------------------------------------- run

    def _prepare_server(self) -> None:
        """Validate the calculator folder now; the launch itself happens in the worker."""
        if not self.autostart_var.get():
            return
        folder = self.calculator_var.get().strip()
        exe = find_server(folder)
        if exe is None:
            raise ValueError(
                "没找到摸轴器：请把「PCRCalculator 目录」指到 PCRCalculator 的安装目录"
                "（里面要有 server\\PCRCalculatorServer.exe），或者取消自动启动、自己先开摸轴器。"
            )
        # ensure() runs on the worker thread, so it must log through the queue.
        self.hosted = HostedServer(exe, port=self._port(), log=self._log)

    def start(self, resume_state: dict | None = None, run_dir: Path | None = None) -> None:
        if self.save is None:
            self._notice("先选一条 PCRCalculator 导出的轴。")
            return
        if self.worker is not None and self.worker.is_alive():
            return
        spec = self._mode_spec()
        mode = spec.key
        targets: list = []
        try:
            if mode == mode_registry.FRAME:
                if not self.save.has_frames:
                    raise ValueError(
                        "这条轴里没有帧数据（只有语义 UB 数据）。\n"
                        "逐帧搜索需要帧轴，请把「优化模式」切到语义 GA。"
                    )
                job_settings = self._job_settings()
                search_settings = self._search_settings()
                settings: object = FrameSettings(job_settings, search_settings)
                targets = build_targets(
                    self.save.frames, self.save.unit_ids,
                    names=self.save.names, boss_name=self.save.boss_name,
                    include_boss=job_settings.include_boss, items=job_settings.items,
                )
                if self.only_checked_var.get() and not self.selection:
                    self._notice("没有选中任何项。")
                    return
                if not targets:
                    self._notice("按当前设置没有可调的项。")
                    return
            elif mode == mode_registry.GA:
                settings = self._ga_settings()
            elif mode == mode_registry.ANNEAL:
                settings = self._anneal_settings()
            elif mode == mode_registry.ROSTER:
                if not self._pool_ids():
                    raise ValueError("先在「候选角色」里挑人（或点「选择角色…」）。")
                settings = self._roster_settings()
            elif mode == mode_registry.CYCLE:
                values = {k: v.get() for k, v in self.cycle_vars.items()}
                for k in ("battle_time", "seed", "samples", "batch_size", "timeout"):
                    values[k] = getattr(self, k + "_var").get()
                settings = dataclasses.replace(spec.from_values(values, self.save), endpoint=endpoint_for(self._port()))
            elif mode == mode_registry.ZERO:
                settings = self._zero_settings()
            else:
                # 以后新接的模式：界面不熟它，就用它声明的默认值。
                settings = spec.from_values(spec.defaults(), self.save)
            self._prepare_server()
            if mode == mode_registry.ROSTER:
                self._load_roster_book()
        except ValueError as exc:
            self._report_error(str(exc))
            return
        run_dir = Path(run_dir) if run_dir else timestamp_dir(self._out_root(), self.save.path.stem)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.last_run_dir = run_dir
        self._open_log_file(run_dir / "log.txt")
        self._append_log(f"运行目录 {run_dir}")
        self._append_log(f"优化模式 {spec.label}；接口 {getattr(settings, 'endpoint', endpoint_for(self._port()))}")
        if targets:
            self._append_log(f"将测 {len(targets)} 项；{settings.search.describe()}")
        for line in spec.plan(self.save, settings):
            self._append_log(line)
        try:
            extra: dict = {}
            if mode == mode_registry.ROSTER and self.roster_book is not None:
                extra["names"] = {
                    unit: self.roster_book.label(unit) for unit in settings.pool
                }
            self.session = spec.open_session(
                self.save, settings, run_dir, log=self._log, resume_state=resume_state, **extra,
            )
        except (ValueError, OSError) as exc:
            self._close_log_file()
            self._report_error(f"无法开始：{exc}")
            return
        self.save_settings()
        self.shown_results = 0
        self.shown_history = 0
        self.output.select(self.log_tab)
        self._result_columns(mode)
        self.shown_team = ()
        self.shown_zero = ()
        self.result_tree.delete(*self.result_tree.get_children())
        total = len(targets) * max(1, int(getattr(settings, "job", settings).passes)) if targets else 1
        if mode == mode_registry.GA:
            total = int(getattr(settings, "generations", 1))
        elif mode == mode_registry.ANNEAL:
            total = int(getattr(settings, "iterations", 1)) * max(1, int(getattr(settings, "restarts", 1) or 1))
        elif mode == mode_registry.ROSTER:
            from .roster import team_count

            total = max(1, team_count(settings))
        elif mode == mode_registry.CYCLE:
            total = settings.depth
        elif mode == mode_registry.ZERO:
            total = max(1, int(settings.iterations) + 1)
        self.progress.configure(maximum=max(total, 1), value=0)
        self._set_running(True)
        self.status_var.set(f"运行中…（{spec.label}）")
        self.worker = threading.Thread(target=self._run_worker, name="tune", daemon=True)
        self.worker.start()

    def _run_worker(self) -> None:
        session = self.session
        assert session is not None
        try:
            if self.hosted is not None:
                self.hosted.ensure()
            self.events.put(("done", session.run()))
        except Exception as exc:  # noqa: BLE001 - surfaced in the window
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.events.put(("error", detail))
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                pass

    def stop(self) -> None:
        if self.session is None:
            return
        self.session.cancel()
        self.stop_button.configure(state="disabled")
        self.status_var.set("收到停止请求：正在收尾，不会写回任何帧…")
        self._append_log("收到停止请求：正在收尾，不会写回任何帧。")

    def on_close(self) -> None:
        """Closing the window mid-run must not silently swallow a half-done search."""
        if self.session is not None:
            if not messagebox.askyesno(
                TITLE,
                "还在调帧。\n关闭窗口会直接退出（不会写回任何帧，已完成项留在 state.json）。\n现在停止并退出吗？",
            ):
                return
            self.session.cancel()
            self._append_log("关闭窗口：已请求停止，不写回任何帧。")
            if self.worker is not None:
                self.worker.join(timeout=5.0)
        self.save_settings()
        if self.hosted is not None:
            try:
                self.hosted.stop()
            except OSError:
                pass
            self.hosted = None
        self._close_log_file()
        self.master.destroy()

    def _on_finished(self, code: int) -> None:
        session, self.session = self.session, None
        self.worker = None
        try:
            job = session.job if session else None
            if job is not None:
                self._refresh_progress(job)
            summary = getattr(session, "summary", None) or {}
            if summary.get("teams"):
                self.roster_results = [dict(item) for item in summary["teams"]]
            semantic = getattr(job, "history", None) is not None
            roster = getattr(job, "teams_total", None) is not None
            zero = getattr(job, "rounds_total", None) is not None
            if code == CANCELLED:
                self.status_var.set("已中断：没有写回任何帧（可在运行目录里续跑）")
            elif code == OK:
                if roster:
                    from .roster import lineup_combo

                    team = tuple(int(unit) for unit in (summary.get("best_team") or ()))
                    toggles = tuple(summary.get("best_toggles") or ())
                    self.status_var.set(
                        f"完成：排了 {int(summary.get('teams_done', 0))}/{int(summary.get('teams_total', 0))} 支队伍"
                        f"（{int(summary.get('battles', 0))} 场），最优 {int(summary.get('final', 0)):,}"
                        f"（{lineup_combo(team, toggles) if toggles and team else '—'}）"
                    )
                elif zero:
                    best = summary.get("best") or {}
                    rounds_done = max(0, int(summary.get("rounds_done", 0)) - 1)
                    self.status_var.set(
                        f"完成：退火了 {rounds_done}/{int(summary.get('iterations', 0))} 轮"
                        f"（{int(summary.get('battles', 0))} 场），最优 {int(float(best.get('damage') or 0)):,}"
                    )
                elif summary.get("mode") == "cycle":
                    self.status_var.set(f"循环搜索完成，最高期望伤害 {summary['final']:,.0f}")
                elif semantic:
                    settings = getattr(session, "settings", None) if session else None
                    planned = int(
                        getattr(settings, "generations", 0) or getattr(settings, "iterations", 0) or 0
                    )
                    state = getattr(job, "state", None)
                    done = int(getattr(state, "iteration", 0) or 0)
                    if not done:
                        done = max(len(getattr(job, "history", ()) or ()) - 1, 0)  # 含起点快照
                    self.status_var.set(
                        f"完成：跑了 {min(done, planned) if planned else done} {_step_suffix(job)}，结果轴已生成"
                    )
                else:
                    # 逐帧 / 其它模式严格用 job.written；阵容搜索没有这个字段
                    self.status_var.set(
                        f"完成：写回 {int(getattr(job, 'written', 0))} 项，结果轴已生成"
                    )
                # 阵容结果轴里只有一支队伍，不当下一次搜索的输入（候选池在界面上，不用从轴上读）
                if not roster and not zero:
                    workbook = getattr(session, "workbook", None) if session else None
                    if workbook is not None and Path(workbook).is_file():
                        self._load_result_axis(Path(workbook))
        finally:
            # 不管上面哪一步出错，都必须放开「开始」按钮，否则下一轮点不动
            self._set_running(False)
            self._close_log_file()

    # ------------------------------------------------------------------- pump

    def _pump(self) -> None:
        if not self.master.winfo_exists():
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._on_finished(int(payload))
                elif kind == "error":
                    self._append_log(f"[error] {payload}")
                    self.status_var.set("出错：见日志")
                    self._report_error(f"调帧过程中出错：\n{payload}")
                    self._on_finished(-1)
                elif kind == "icons":
                    done, total = payload
                    self.icon_bar.configure(maximum=max(1, total), value=done)
                    self.icon_status.configure(text=f"图标：{done} / {total}")
                elif kind == "icons_done":
                    self.icon_status.configure(text=f"图标缓存：{_icon_dir_text()}")
                    for refresh in self._icon_refresh:
                        refresh()
                elif kind == "icons_pack":
                    self._icons.clear()
                    for refresh in self._icon_refresh:
                        refresh()
                    self.icon_status.configure(text=f"图标包：已导入 {payload} 个")
                    self._append_log(f"图标包：已导入 {payload} 个图标到 {_icon_dir_text()}")
                elif kind == "probe":
                    fill = getattr(self, "_probe_fill", None)
                    if callable(fill):
                        fill(payload)
                    self.status_var.set("已列出这队的 32 种组合")
                elif kind == "probe_error":
                    self._append_log(f"[error] 试这队失败：{payload}")
                    self._report_error(f"试这队失败：\n{payload}")
                elif kind == "icons_error":
                    self.icon_status.configure(text="图标包：导入失败")
                    self._append_log(f"[error] 导入图标包失败：{payload}")
                    self._report_error(f"导入图标包失败：\n{payload}")
            except Exception as exc:  # noqa: BLE001 - keep the window alive
                self.status_var.set(f"界面更新出错：{exc}")
        if self.session is not None:
            try:
                self._refresh_progress(self.session.job)
            except Exception as exc:  # noqa: BLE001 - never stop the pump
                self.status_var.set(f"界面更新出错：{exc}")
        self.after(PUMP_MS, self._pump)

    def _refresh_progress(self, job) -> None:
        if type(job).__name__ == "CycleJob":
            self.progress.configure(value=len(job.history))
            best = f"{job.best.metric:,.0f}" if job.best else "—"
            self.counter_label.configure(text=f"已完成 {len(job.history)} 层 / {job.simulations} 场 / 最优 {best}")
            while self.shown_history < len(job.history):
                entry = job.history[self.shown_history]
                self.shown_history += 1
                self.result_tree.insert("", "end", values=("循环搜索", self.shown_history, "", "整场最优",
                                                         f"{entry['damage']:,.0f}", entry["simulations"]))
            return
        if getattr(job, "teams_total", None) is not None:  # 阵容搜索：一支一支队伍地排
            self._refresh_roster_progress(job)
            return
        if getattr(job, "rounds_total", None) is not None:  # 0 押轴：退火一轮一轮推进
            self._refresh_zero_progress(job)
            return
        items = getattr(job, "items", None)
        if items is None:  # 语义 GA / 退火：按代或按迭代推进
            history = getattr(job, "history", None) or []
            state = getattr(job, "state", None)
            steps = len(history)
            if state is not None and getattr(state, "iteration", 0):
                steps = max(steps, int(state.iteration))
            self.progress.configure(value=min(steps, int(float(self.progress["maximum"]))))
            best = None
            if history:
                last = history[-1]
                best = last.get("damage", last.get("best"))
            counter = f"第 {steps} {_step_suffix(job)} / 模拟 {getattr(job, 'simulations', 0)} 场"
            if best is not None:
                counter += f" / 最优 {float(best):,.0f}"
            self.counter_label.configure(text=counter)
            self._show_history(history, job)
            return
        self.progress.configure(value=min(len(items), int(float(self.progress["maximum"]))))
        self.counter_label.configure(
            text=f"写回 {job.written} / 跳过 {job.skipped} / 失败 {job.failed} / 场次 {job.battles}"
        )
        while self.shown_results < len(items):
            item = items[self.shown_results]
            self.shown_results += 1
            frames = f"{item['old_frame']} → {item.get('new_frame') or item['old_frame']}"
            gain = ""
            if item.get("peak_metric") is not None:
                gain = f"{item.get('baseline_metric', 0):,.0f} → {item['peak_metric']:,.0f}"
            self.result_tree.insert("", "end", values=(
                item.get("label"), item.get("pass") or 1, frames,
                item.get("decision_text") or DECISION_TEXT.get(item.get("decision"), ""),
                gain, item.get("battles", ""),
            ))

    def _refresh_roster_progress(self, job) -> None:
        """阵容搜索：进度按「排完几支队伍」，最优阵容变了就往结果页补一行。"""
        total = int(getattr(job, "teams_total", 0) or 0)
        done = int(getattr(job, "teams_done", 0) or 0)
        self.progress.configure(value=min(done, int(float(self.progress["maximum"]))))
        best = getattr(job, "best_score", None)
        counter = f"队伍 {done}/{total} / 场次 {getattr(job, 'battles_done', 0)}"
        team = tuple(int(unit) for unit in (getattr(job, "best_team", None) or ()))
        if best is not None:
            counter += f" / 最优 {float(best.damage):,.0f}"
            if team and team != self.shown_team:
                from .roster import lineup_combo, lineup_names

                self.shown_team = team
                names = getattr(job, "names", None) or {}
                self.result_tree.insert("", "end", values=(
                    f"第 {done} 支队伍", f"{done}/{total}",
                    lineup_names(team, names),
                    lineup_combo(team, best.toggles),
                    f"{float(best.damage):,.0f}", getattr(job, "battles_done", ""),
                ))
        self.counter_label.configure(text=counter)

    def _refresh_zero_progress(self, job) -> None:
        """0 押轴：进度按退火轮数走，最优方案每刷新一次就往结果页补一行。"""
        total = int(getattr(job, "rounds_total", 0) or 0)
        done = int(getattr(job, "rounds_done", 0) or 0)
        self.progress.configure(value=min(done, int(float(self.progress["maximum"]))))
        best = getattr(job, "best", None)
        counter = f"退火 {done}/{total} 轮 / 场次 {getattr(job, 'battles_done', 0)}"
        if best is not None:
            counter += f" / 最优 {float(best.damage):,.0f}"
            plan = tuple(tuple(bool(flag) for flag in row) for row in getattr(best, "plan", ()))
            if plan and plan != getattr(self, "shown_zero", ()):
                from .roster import lineup_names
                from .zero import plan_text

                self.shown_zero = plan
                units = tuple(getattr(job, "units", ()) or ())
                names = getattr(job, "names", None) or {}
                self.result_tree.insert("", "end", values=(
                    f"{done}/{total}",
                    lineup_names(units, names) if units else "",
                    plan_text(plan), "",
                    f"{float(best.damage):,.0f}",
                    getattr(job, "battles_done", ""),
                ))
        self.counter_label.configure(text=counter)

    def _show_history(self, history, job=None) -> None:
        """语义 GA 每跑完一代、退火每次日志间隔就往结果页补一行。"""
        anneal = _is_anneal_job(job)
        while self.shown_history < len(history):
            entry = history[self.shown_history]
            self.shown_history += 1
            if anneal:
                step = entry.get("iteration", self.shown_history)
                best = entry.get("best", entry.get("damage", 0))
                self.result_tree.insert("", "end", values=(
                    f"第 {step} 次迭代", step, "",
                    f"温度 {float(entry.get('temperature', 0)):,.4f}",
                    f"{float(best or 0):,.0f}", entry.get("simulations", ""),
                ))
                continue
            generation = entry.get("generation", self.shown_history)
            best_generation = entry.get("best_generation", generation)
            note = "本代刷新" if best_generation == generation else f"最优仍来自第 {best_generation} 代"
            self.result_tree.insert("", "end", values=(
                f"第 {generation} 代", generation, "",
                note, f"{float(entry.get('damage', 0)):,.0f}", entry.get("simulations", ""),
            ))

    # ------------------------------------------------------------------ output

    def _log(self, message: str) -> None:
        self.events.put(("log", message))
        if self.log_file is not None:
            try:
                self.log_file.write(f"{message}\n")
                self.log_file.flush()
            except OSError:
                pass

    LOG_LIMIT = 4000  # 界面里只留最近这么多行，完整日志在日志文件里

    def _notice(self, message: str) -> None:
        """提示只写界面日志，不弹窗。"""
        self._append_log(str(message))

    def _report_error(self, message: str) -> None:
        """错误只写界面日志，不弹窗。"""
        text = "；".join(part.strip() for part in str(message).splitlines() if part.strip())
        self._append_log("错误：" + (text or "未知错误"))
        self.status_var.set("出错：见日志")

    def _append_log(self, message: str) -> None:
        follow = self.log_text.yview()[1] >= 0.999 or not self.log_text.winfo_ismapped()
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > self.LOG_LIMIT:
            self.log_text.delete("1.0", f"{self.LOG_LIMIT // 4}.0")
        if follow:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _open_log_file(self, path: Path) -> None:
        self._close_log_file()
        try:
            self.log_file = open(path, "a", encoding="utf-8")
        except OSError:
            self.log_file = None

    def _close_log_file(self) -> None:
        if self.log_file is not None:
            try:
                self.log_file.close()
            finally:
                self.log_file = None

    def _run_dir(self) -> Path | None:
        if self.session is not None:
            return self.session.run_dir
        return self.last_run_dir

    def open_run_dir(self) -> None:
        run_dir = self._run_dir()
        if not run_dir or not Path(run_dir).is_dir():
            self._notice("还没有跑过，或运行目录已经不在。")
            return
        _open_path(Path(run_dir))

    def open_report(self) -> None:
        run_dir = self._run_dir()
        if not run_dir:
            self._notice("还没有跑过。")
            return
        report = Path(run_dir) / "报告.md"
        target = report if report.is_file() else Path(run_dir)
        _open_path(target)

    # ------------------------------------------------------------------ resume

    def resume(self) -> None:
        folder = filedialog.askdirectory(title="选择中断的运行目录", initialdir=str(self._out_root()))
        if not folder:
            return
        run_dir = Path(folder)
        config_path = run_dir / CONFIG
        if not config_path.is_file():
            self._report_error(f"{run_dir} 不是运行目录（缺少 {CONFIG}）")
            return
        try:
            config = read_json(config_path)
            excel = Path(str(config.get("excel") or ""))
            if not excel.is_file():
                self._report_error(f"运行目录里记录的轴文件不在了：\n{excel}")
                return
            state = read_json(run_dir / STATE) if (run_dir / STATE).is_file() else None
            self.open_axis(excel)
            if self.save is None:
                return
            key = str(config.get("mode") or mode_registry.FRAME)
            spec = mode_registry.get(key)
            self.mode_var.set(dict(MODES).get(key, spec.label))
            self._sync_mode()
            if key == mode_registry.CYCLE:
                stored = config.get("settings", {})
                for name, var in self.cycle_vars.items():
                    var.set(stored[name])
                for name in ("battle_time", "seed", "samples", "batch_size", "timeout"):
                    getattr(self, name + "_var").set(stored[name])
            elif key == mode_registry.GA:
                self._apply_ga_settings(GaSettings.from_dict(config.get("settings") or {}))
            elif key == mode_registry.ANNEAL:
                from .anneal import AnnealSettings

                self._apply_anneal_settings(AnnealSettings.from_dict(config.get("settings") or {}))
            elif key == mode_registry.ROSTER:
                from .roster import RosterSettings

                self._apply_roster_settings(RosterSettings.from_dict(config.get("settings") or {}))
            else:
                settings = JobSettings.from_dict(config.get("settings") or {})
                search = SearchSettings.from_dict(config.get("search") or {})
                self._apply_settings(settings, search)
        except (ValueError, OSError) as exc:
            self._report_error(f"读不了这个运行目录：\n{exc}")
            return
        self._append_log(f"续跑 {run_dir}")
        self.start(resume_state=state, run_dir=run_dir)

    def _apply_anneal_settings(self, settings) -> None:
        """续跑退火时把配置写回界面。"""
        self.battle_time_var.set(str(settings.battle_time))
        self.seed_var.set(str(settings.seed))
        self.samples_var.set(str(settings.samples))
        self.batch_size_var.set(str(settings.batch_size))
        self.timeout_var.set(f"{settings.timeout:g}")
        self.iterations_var.set(str(settings.iterations))
        self.initial_temperature_var.set(f"{settings.initial_temperature:g}")
        self.final_temperature_var.set(f"{settings.final_temperature:g}")
        self.chain_range_var.set(str(settings.chain_range))
        self.swap_chance_var.set(f"{settings.swap_chance:g}")
        self.double_chance_var.set(f"{settings.double_swap_chance:g}")
        self.chain_chance_var.set(f"{settings.chain_chance:g}")
        self.manual_chance_var.set(f"{settings.manual_chance:g}")
        self.allow_duplicates_var.set(bool(settings.allow_duplicates))
        self.anneal_restarts_var.set(str(settings.restarts))
        self.report_every_var.set(str(settings.report_every))
        self.anneal_delay_var.set(str(settings.delay))
        self.verify_var.set(bool(settings.verify))
        ga = settings.ga_equivalent()
        self.ub_type_var.set(_label_of(UB_TYPE_LABELS, ga.ub_type))
        self.skill_type_var.set(_label_of(SKILL_TYPE_LABELS, ga.skill_type))
        self.sequence_type_var.set(_label_of(SEQUENCE_TYPE_LABELS, ga.sequence_type))
        self.fingerprint_var.set(f"{settings.fingerprint_distance:g}")
        self.legacy_ub_var.set(bool(settings.legacy_ub))
        self.own_skill_var.set(bool(settings.own_skill))
        self.other_skill_var.set(bool(settings.other_skill))

    def _apply_ga_settings(self, settings: GaSettings) -> None:
        self.battle_time_var.set(str(settings.battle_time))
        self.seed_var.set(str(settings.seed))
        self.samples_var.set(str(settings.samples))
        self.batch_size_var.set(str(settings.batch_size))
        self.timeout_var.set(f"{settings.timeout:g}")
        self.population_var.set(str(settings.population))
        self.generations_var.set(str(settings.generations))
        self.islands_var.set(str(settings.islands))
        self.crossover_var.set(f"{settings.crossover:g}")
        self.mutation_var.set(f"{settings.mutation:g}")
        self.elite_var.set(str(settings.elite_count))
        self.mutation_decay_var.set(f"{settings.mutation_decay:g}")
        self.mutation_floor_var.set(f"{settings.mutation_floor:g}")
        self.stagnation_var.set(str(settings.stagnation_limit))
        self.restarts_var.set(str(settings.restarts))
        self.migration_var.set(str(settings.migration_interval))
        self.migrants_ratio_var.set(f"{settings.migrants_ratio:g}")
        self.disaster_var.set(str(settings.disaster_patience))
        self.max_lane_var.set(str(settings.max_lane_length))
        self.fingerprint_var.set(f"{settings.fingerprint_distance:g}")
        self.ga_seed_var.set(str(settings.ga_seed))
        self.delay_var.set(str(settings.delay))
        self.ub_type_var.set(_label_of(UB_TYPE_LABELS, settings.ub_type))
        self.skill_type_var.set(_label_of(SKILL_TYPE_LABELS, settings.skill_type))
        self.sequence_type_var.set(_label_of(SEQUENCE_TYPE_LABELS, settings.sequence_type))
        self.legacy_ub_var.set(bool(settings.legacy_ub))
        self.own_skill_var.set(bool(settings.own_skill))
        self.other_skill_var.set(bool(settings.other_skill))
        self.verify_var.set(bool(settings.verify))

    def _apply_settings(self, settings: JobSettings, search: SearchSettings) -> None:
        self.battle_time_var.set(str(settings.battle_time))
        self.seed_var.set(str(settings.seed))
        self.passes_var.set(str(settings.passes))
        self.samples_var.set(str(settings.samples))
        self.batch_size_var.set(str(settings.batch_size))
        self.timeout_var.set(f"{settings.timeout:g}")
        self.calc_stats_var.set(str(settings.calc_stats))
        self.include_boss_var.set(settings.include_boss)
        self.verify_var.set(settings.verify)
        self.step_var.set(str(search.step))
        self.max_offset_var.set(str(search.max_offset))
        self.outer_probes_var.set(str(search.outer_probes))
        self.threshold_var.set(f"{search.threshold_value:g}")
        self.threshold_mode_var.set(search.threshold_mode.label)
        self.peak_tolerance_var.set(f"{search.peak_tolerance:g}")
        self.chunk_var.set(str(search.chunk))
        self.only_checked_var.set(bool(settings.items))
        self.rebuild_targets()
        if settings.items:  # one-based in JobSettings, zero-based in the tree
            self.selection = {f"{row - 1}:{index - 1}" for row, index in settings.items}
            self.refresh_items()

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running or self.save is None else "normal"
        self.start_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self.open_dir_button.configure(state="disabled" if running else "normal")
        self.open_report_button.configure(state="disabled" if running else "normal")


def _fit_window(root: tk.Tk) -> None:
    """Open at the size the layout actually asks for, clamped to the screen.

    The requested height depends on the font and the DPI scaling, so a hardcoded
    geometry either clips the status bar or wastes space.
    """
    root.update_idletasks()
    width = max(1040, min(root.winfo_reqwidth() + 24, max(880, root.winfo_screenwidth() - 80)))
    height = max(GEOMETRY_HINT[1], min(root.winfo_reqheight() + 24, max(GEOMETRY_HINT[1], root.winfo_screenheight() - 90)))
    root.geometry(f"{width}x{height}")


def _set_window_icon(root: tk.Tk) -> None:
    path = Path(__file__).with_name("app-icon.png")
    image = tk.PhotoImage(master=root, file=str(path))
    root.iconphoto(True, image)
    root._app_icon = image
    if sys.platform == "win32":
        # Tk 可让系统自行补小图标；显式给两个 Windows 图标槽同一张原图。
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        create = user32.CreateIconFromResourceEx
        create.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
        create.restype = wintypes.HICON
        png = path.read_bytes()
        icon = create(png, len(png), True, 0x30000, image.width(), image.height(), 0)
        if icon:
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DestroyIcon.argtypes = [wintypes.HICON]
            root.update_idletasks()
            hwnd = user32.GetAncestor(root.winfo_id(), 2)
            for slot in (0, 1):
                user32.SendMessageW(hwnd, 0x80, slot, icon)
            root.bind("<Destroy>", lambda e: user32.DestroyIcon(icon) if e.widget is root else None, add="+")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="PcrTimelineTester-gui", description="PcrTimelineTester（图形界面）")
    parser.add_argument("excel", nargs="?", type=Path, help="启动时直接打开的轴")
    args = parser.parse_args(argv)
    _attach_std_streams()
    log_path = _install_crash_handler()
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PcrTimelineTester.Desktop")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法打开图形界面：{exc}（日志：{log_path}）", file=sys.stderr)
        return 1
    root.title(TITLE)
    _set_window_icon(root)
    root.minsize(880, GEOMETRY_HINT[1])
    app = App(root, args.excel)
    globals()["_LAST_APP"] = app  # 崩溃处理器要用它把错误写进界面日志
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    _fit_window(root)
    app.apply_saved_geometry()
    try:
        root.mainloop()
    except Exception as exc:  # 主循环里漏出来的异常：记日志，别让窗口直接消失
        sys.excepthook(type(exc), exc, exc.__traceback__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
