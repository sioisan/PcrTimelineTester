"""Offline pytest stand-in: run the real test files without installing pytest.

It implements just enough of pytest -- ``approx``, ``raises``, fixtures from
conftest, ``monkeypatch`` and ``tmp_path`` -- and executes every ``test_*``
function found in ``tests/``.  Usage (from anywhere):

    python tools/run_tests.py            # 全部用例
    python tools/run_tests.py test_ga    # 只跑名字里带 test_ga 的文件
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import re
import shutil
import sys
import tempfile
import traceback
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


class _Skip(Exception):
    pass


class _Approx:
    def __init__(self, value, rel=1e-6, absolute=1e-12):
        self.value = value
        self.rel = rel
        self.abs = absolute

    def __eq__(self, other):
        try:
            span = max(self.abs, self.rel * max(abs(other), abs(self.value)))
            return abs(other - self.value) <= span
        except TypeError:
            return NotImplemented

    def __repr__(self):
        return f"approx({self.value!r})"


class _Raises:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            raise AssertionError(f"期望抛出 {self.expected.__name__}，但没有异常")
        if not issubclass(kind, self.expected):
            return False
        if self.match and not re.search(self.match, str(value)):
            raise AssertionError(f"异常文本 {str(value)!r} 不匹配 {self.match!r}")
        self.value = value
        return True


pytest = types.ModuleType("pytest")
pytest.approx = lambda value, rel=1e-6, abs=None: _Approx(value, rel, 1e-12 if abs is None else abs)
pytest.raises = lambda expected, match=None: _Raises(expected, match)
pytest.fixture = lambda *args, **kwargs: (args[0] if args and callable(args[0]) else (lambda fn: fn))
pytest.skip = lambda *args, **kwargs: (_ for _ in ()).throw(_Skip(reason=str(args[0] if args else "")))
pytest.fail = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(*args))


class _Mark:
    def __getattr__(self, name):
        return lambda *args, **kwargs: (lambda fn: fn)


pytest.mark = _Mark()
sys.modules["pytest"] = pytest


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None, raising=True):
        if isinstance(target, str):
            module_name, _, attr = target.rpartition(".")
            target = importlib.import_module(module_name)
            name, value = attr, name
        old = getattr(target, name, _MISSING)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def delattr(self, target, name, raising=True):
        old = getattr(target, name, _MISSING)
        self._undo.append((target, name, old))
        delattr(target, name)

    def setitem(self, mapping, key, value):
        old = mapping.get(key, _MISSING)
        self._undo.append((mapping, key, old))
        mapping[key] = value

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            if isinstance(target, dict):
                if old is _MISSING:
                    target.pop(name, None)
                else:
                    target[name] = old
            elif old is _MISSING:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
            else:
                setattr(target, name, old)


class _Missing:
    pass


_MISSING = _Missing()


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules，否则模块里的 @dataclass 找不到自己的模块命名空间。
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _fixtures(module) -> dict:
    found = {}
    for name, value in vars(module).items():
        if callable(value) and getattr(value, "_pytest_fixture", True) and not name.startswith("_"):
            try:
                params = list(inspect.signature(value).parameters)
            except (TypeError, ValueError):
                continue
            found[name] = (value, params)
    return found


def main() -> int:
    scratch = ROOT / "build" / ".tmp_tests"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    conftest = _load(ROOT / "tests" / "conftest.py", "conftest")
    registry = _fixtures(conftest)
    passed = failed = skipped = 0
    failures: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = _load(path, path.stem)
        local = dict(registry)
        local.update({k: v for k, v in _fixtures(module).items() if k not in local})
        tests = [(name, value) for name, value in vars(module).items()
                 if name.startswith("test_") and callable(value)]
        for name, function in tests:
            patch = MonkeyPatch()
            cache: dict = {}

            def resolve(param: str):
                if param in cache:
                    return cache[param]
                if param == "monkeypatch":
                    cache[param] = patch
                    return patch
                if param == "tmp_path":
                    value = Path(tempfile.mkdtemp(dir=scratch))
                    cache[param] = value
                    return value
                if param not in local:
                    raise AssertionError(f"{path.name}::{name} 需要未知 fixture {param!r}")
                factory, params = local[param]
                kwargs = {nested: resolve(nested) for nested in params if nested not in ("self",)}
                value = factory(**kwargs)
                cache[param] = value
                return value

            try:
                signature = inspect.signature(function)
                kwargs = {p: resolve(p) for p in signature.parameters}
                function(**kwargs)
                passed += 1
            except _Skip:
                skipped += 1
            except Exception as exc:  # noqa: BLE001 - a runner must report, not crash
                failed += 1
                failures.append(f"{path.name}::{name}\n{traceback.format_exc()}")
                print(f"FAIL {path.name}::{name}: {exc}")
            finally:
                patch.undo()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    for text in failures:
        print("\n" + text)
    shutil.rmtree(scratch, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
