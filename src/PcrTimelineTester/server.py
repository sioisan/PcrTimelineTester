"""Locate, launch and stop the PCRCalculator simulation server.

The tool no longer asks for a URL: it asks where PCRCalculator is installed and
which port the server should listen on, then starts
``PCRCalculatorServer.exe --server --host 127.0.0.1 --port N`` for the duration of
the tuning run.  A server that is already listening on that port is reused and
never killed -- we only stop what we started ourselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Callable

SERVER_EXE_NAMES = ("PCRCalculatorServer.exe", "PCRGuildCalculatorServer.exe")
DEFAULT_PORT = 8080
LAUNCH_TIMEOUT = 120.0
HOST = "127.0.0.1"


def find_server(target: str | Path | None) -> Path | None:
    """Accept the calculator folder, its ``server`` subfolder, or the exe itself."""
    if target is None:
        return None
    text = str(target).strip().strip('"')
    if not text:
        return None
    base = Path(text).expanduser()
    if base.is_file():
        return base if base.suffix.lower() == ".exe" else None
    candidates: list[Path] = []
    for name in SERVER_EXE_NAMES:
        candidates.append(base / name)
        candidates.append(base / "server" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def port_open(port: int, host: str = HOST, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def endpoint_for(port: int, host: str = HOST) -> str:
    return f"http://{host}:{int(port)}/simulate"


def _console_flags() -> int:
    """让摸轴器开自己的前台控制台窗口，别在当前进程里藏着跑。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def _bind_to_this_process(process: subprocess.Popen) -> object | None:
    """把摸轴器挂进一个「随本进程关闭而退出」的 Windows Job 对象。

    这样即使本程序被直接关掉（甚至被强杀），也不会留下一个还在吃资源的摸轴器。
    任何一步失败都只是放弃这层保险，不影响正常使用。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)  # 9 = ExtendedLimitInformation
        ):
            kernel32.CloseHandle(job)
            return None
        handle = wintypes.HANDLE(int(process._handle))  # noqa: SLF001 (Popen 只给了私有句柄)
        if not kernel32.AssignProcessToJobObject(job, handle):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:  # pragma: no cover - 纯保险，失败就算了
        return None


@dataclass
class HostedServer:
    """One server process owned by this run (or a reuse of somebody else's)."""

    executable: Path
    port: int = DEFAULT_PORT
    log: Callable[[str], None] = print
    process: subprocess.Popen | None = field(default=None, repr=False)
    started: bool = False
    _job: object | None = field(default=None, repr=False)
    _sink: object | None = field(default=None, repr=False)

    def ensure(self, *, timeout: float = LAUNCH_TIMEOUT) -> bool:
        """Make sure the port answers; start the server when it does not."""
        if port_open(self.port):
            self.log(f"端口 {self.port} 上已经有摸轴器在跑，直接使用（不会关掉它）")
            return False
        if not Path(self.executable).is_file():
            raise OSError(f"找不到摸轴器程序：{self.executable}")
        self.log(
            f"启动摸轴器（前台窗口）：{Path(self.executable).name} --server --host {HOST} --port {self.port}"
            f"（工作目录 {Path(self.executable).parent}）"
        )
        self.process = subprocess.Popen(
            [str(self.executable), "--server", "--host", HOST, "--port", str(int(self.port))],
            cwd=str(Path(self.executable).parent),
            stdin=subprocess.DEVNULL,
            stdout=self._log_sink(),
            stderr=subprocess.STDOUT,
            creationflags=_console_flags(),
        )
        self.started = True
        self._job = _bind_to_this_process(self.process)
        self.log(
            "摸轴器在自己的窗口里运行：关掉那个窗口，或者关掉本程序，它都会跟着停下来"
            f"{'' if self._job is not None else '（没挂上随本程序退出的保险，可能需要在任务管理器里结束）'}"
        )
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if port_open(self.port):
                self.log(f"摸轴器已就绪：端口 {self.port}（首次加载约 2~3 秒）")
                return True
            code = self.process.poll()
            if code is not None:
                self.started = False
                raise RuntimeError(f"摸轴器启动失败，进程已退出（退出码 {code}）")
            time.sleep(0.4)
        raise RuntimeError(f"摸轴器在 {timeout:g} 秒内没有打开端口 {self.port}")

    def _log_sink(self):
        """摸轴器自己的输出写文件（窗口只是给人看/关的），失败就让它继承当前终端。"""
        try:
            from .settings import settings_path

            path = settings_path().parent / "server.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._sink = open(path, "a", encoding="utf-8", errors="replace")
            self.log(f"摸轴器日志：{path}")
            return self._sink
        except OSError:
            return None

    def stop(self) -> None:
        """Kill only the process this object started."""
        process, self.process = self.process, None
        if self._sink is not None:
            try:
                self._sink.close()
            except OSError:
                pass
            self._sink = None
        if not self.started or process is None:
            return
        self.started = False
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
        except OSError:
            return
        self.log(f"已关闭本次启动的摸轴器（端口 {self.port}）")
