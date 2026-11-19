"""构建冻结版（PyInstaller）。

用法（在项目根目录）：

    python tools/build_frozen.py              # 单文件：dist/PcrTimelineTester.exe
    python tools/build_frozen.py --onedir     
    python tools/build_frozen.py --clean      # 先删 build/ 与 dist/

冻结版只打包图形界面；命令行入口（``PcrTimelineTester``）仍然用 pip 装。
名字可以用环境变量 ``PFT_FROZEN_NAME`` 覆盖（CI 里图省事）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8。

    GitHub Actions 的 Windows runner 默认 cp1252，这里任何一句中文 print 都会抛
    ``UnicodeEncodeError: 'charmap' codec can't encode characters``；换成 UTF-8 后
    日志在 CI 里也能正常显示。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 不是可写的文本流就算了
            pass


_utf8_console()

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "tools" / "launch.py"
ICON_PNG = ROOT / "src" / "PcrTimelineTester" / "app-icon.png"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def _prepare_icon() -> Path:
    """桌面程序资源使用单张 256 像素 ICO，窗口直接读取 512 像素原图。"""
    from PIL import Image

    BUILD.mkdir(parents=True, exist_ok=True)
    target = BUILD / "icon.ico"
    with Image.open(ICON_PNG) as source:
        source.resize((256, 256), Image.Resampling.LANCZOS).save(target, sizes=[(256, 256)])
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(*, clean: bool, name: str, console: bool = False) -> int:
    if clean:
        for folder in (DIST, BUILD):
            if folder.exists():
                shutil.rmtree(folder)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("没装 PyInstaller：先运行  python -m pip install pyinstaller", file=sys.stderr)
        return 1

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--console" if console else "--windowed",
        "--onefile",
        "--name", name,
        "--paths", str(ROOT / "src"),
        "--collect-submodules", "PcrTimelineTester",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        str(ENTRY),
    ]
    icon = _prepare_icon()
    args[3:3] = ["--icon", str(icon), "--add-data", f"{ICON_PNG}{os.pathsep}PcrTimelineTester"]
    print(" ".join(args))
    code = subprocess.call(args, cwd=str(ROOT))
    if code != 0:
        print(f"PyInstaller 失败（退出码 {code}）", file=sys.stderr)
        return code

    target = DIST / f"{name}.exe"
    if not target.exists():
        print(f"没找到构建产物：{target}", file=sys.stderr)
        return 1
    print(f"完成：{target}（{target.stat().st_size / (1 << 20):.1f} MB）")
    print(f"sha256：{_sha256(target)}")
    pack = _copy_icon_pack(name)
    if pack is not None:
        print(f"图标包：{pack}（{pack.stat().st_size / (1 << 20):.1f} MB）")
    return 0


ICON_PACK = ROOT / "icon.zip"


def _copy_icon_pack(name: str) -> Path | None:
    """发布版是「exe + 图标包」两个文件，这里顺手把 icon.zip 复制到 dist/。"""
    if not ICON_PACK.is_file():
        print("提示：项目根没有 icon.zip，这次的发布包里不会有图标包。")
        return None
    target = DIST / (f"{name}-icons.zip" if name == "PcrTimelineTester" else f"{name}-图标包.zip")
    shutil.copyfile(ICON_PACK, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 PcrTimelineTester 的冻结版")
    parser.add_argument("--clean", action="store_true", help="先删 build/ 与 dist/")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排查启动崩溃用）")
    parser.add_argument("--name", default=os.environ.get("PFT_FROZEN_NAME", "PcrTimelineTester"), help="产物名")
    args = parser.parse_args(argv)
    return build(clean=args.clean, name=args.name, console=args.console)


if __name__ == "__main__":
    raise SystemExit(main())
