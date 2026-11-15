"""Server discovery/launch helpers, settings persistence and the Chinese labels."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from PcrTimelineTester import settings as settings_mod
from PcrTimelineTester.search import SearchSettings, ThresholdMode
from PcrTimelineTester.server import DEFAULT_PORT, HostedServer, endpoint_for, find_server, port_open


def _fake_install(root: Path) -> Path:
    """A folder shaped like a PCRCalculator install (server 子目录 + exe)."""
    server = root / "server"
    server.mkdir(parents=True)
    exe = server / "PCRCalculatorServer.exe"
    exe.write_bytes(b"stub")
    return exe


def test_find_server_accepts_folder_subfolder_and_exe(tmp_path) -> None:
    exe = _fake_install(tmp_path / "2.11.0")
    assert find_server(tmp_path / "2.11.0") == exe
    assert find_server(tmp_path / "2.11.0" / "server") == exe
    assert find_server(exe) == exe
    assert find_server(str(tmp_path / "2.11.0") + '\\') == exe


def test_find_server_reports_nothing_when_missing(tmp_path) -> None:
    assert find_server(None) is None
    assert find_server("") is None
    assert find_server(tmp_path) is None
    assert find_server(tmp_path / "不存在") is None


def test_endpoint_for_builds_the_simulate_url() -> None:
    assert endpoint_for(8080) == "http://127.0.0.1:8080/simulate"
    assert endpoint_for(int(DEFAULT_PORT)).endswith("/simulate")


def test_port_open_detects_a_listening_socket() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port_open(port) is True
    assert port_open(port) is False


def test_existing_server_is_reused_not_killed(tmp_path) -> None:
    exe = _fake_install(tmp_path / "2.11.0")
    messages: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        server = HostedServer(exe, port=port, log=messages.append)
        assert server.ensure() is False          # 别人的摸轴器：只复用
        assert server.started is False
        server.stop()                            # 什么也不该发生
    assert any("已经有摸轴器在跑" in message for message in messages)


def test_missing_executable_raises(tmp_path) -> None:
    server = HostedServer(tmp_path / "nope.exe", port=1, log=lambda _m: None)
    with pytest.raises(OSError, match="找不到摸轴器程序"):
        server.ensure()


def test_settings_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings_mod.os, "environ", {"LOCALAPPDATA": str(tmp_path)})
    assert settings_mod.read_settings() == {}
    payload = {"calculator": "calc-dir", "port": "8090", "autostart": True,
               "search": {"step": "5", "threshold_mode": "绝对伤害"}}
    settings_mod.write_settings(payload)
    assert settings_mod.read_settings() == payload
    stored = json.loads((tmp_path / settings_mod.APP_NAME / settings_mod.FILE_NAME).read_text(encoding="utf-8"))
    assert stored["port"] == "8090"


def test_settings_survive_a_broken_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings_mod.os, "environ", {"LOCALAPPDATA": str(tmp_path)})
    path = tmp_path / settings_mod.APP_NAME / settings_mod.FILE_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert settings_mod.read_settings() == {}


def test_threshold_modes_speak_chinese() -> None:
    assert ThresholdMode.ABSOLUTE.label == "绝对伤害"
    assert ThresholdMode.PERCENT.label == "百分比"
    assert ThresholdMode.STRICT.label == "不允许掉伤害"
    for mode in ThresholdMode:
        assert ThresholdMode.parse(mode.label) is mode
        assert ThresholdMode.parse(mode.value) is mode
        assert ThresholdMode.parse(mode.name) is mode
    with pytest.raises(ValueError):
        ThresholdMode.parse("随便什么")


def test_no_battle_cap_by_default() -> None:
    settings = SearchSettings()
    assert settings.max_battles == 0
    assert "场次上限" not in settings.describe()
    assert "绝对伤害" in settings.describe()
    assert SearchSettings(max_battles=400).describe().endswith("场次上限 400")
    with pytest.raises(ValueError, match="场次上限不能是负数"):
        SearchSettings(max_battles=-1)


def test_search_settings_from_dict_reads_chinese_mode() -> None:
    parsed = SearchSettings.from_dict({"max_battles": 0, "threshold_mode": "百分比", "threshold_value": 0.02})
    assert parsed.threshold_mode is ThresholdMode.PERCENT
    assert parsed.threshold_value == pytest.approx(0.02)
    assert parsed.max_battles == 0
