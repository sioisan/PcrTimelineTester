"""datas.py：从摸轴器 Datas 目录读角色名，图标缓存路径。"""

from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from PcrTimelineTester import datas


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _datas_dir(tmp_path: Path) -> Path:
    root = tmp_path / "PCRGuildCalculator_Data" / "StreamingAssets" / "Datas"
    _write(root / "chara_name.json", {
        "1000": ["未知角色", "unknown"],
        "1001": ["日和", "ヒヨリ"],
        "1150": ["似似花(新年)", "new year"],
    })
    _write(root / "UnitNickNameDic.json", {"100101": "猫拳", "115001": "春花"})
    _write(root / "nickname.json", {"100101": "日和莉", "136001": "涅妃"})
    _write(root / "GuildEnemyDatas.json", {"enemyIds": [[401010101, 401010102]]})
    return root


def test_unit_width_is_read_from_the_unit_info_table(monkeypatch) -> None:
    monkeypatch.setattr(
        datas,
        "_info_table",
        {
            134901: datas.UnitInfo(role=2, talent=1, width=196),
            136001: datas.UnitInfo(role=7, talent=4, width=732),
        },
    )
    assert datas.unit_width(134901) == 196  # 花音
    assert datas.unit_width(136001) == 732  # 蜘蛛（最靠后）
    assert datas.unit_width(0) is None


def test_locate_datas_accepts_install_dir_data_dir_and_datas_dir(tmp_path: Path) -> None:
    datas_dir = _datas_dir(tmp_path)
    install = tmp_path / "PCRGuildCalculator"
    install.mkdir()
    assert datas.locate_datas(tmp_path) == datas_dir
    assert datas.locate_datas(tmp_path / "PCRGuildCalculator_Data") == datas_dir
    assert datas.locate_datas(datas_dir) == datas_dir
    assert datas.locate_datas(install) is None


def test_names_prefer_nickname_and_fall_back_to_full_name(tmp_path: Path) -> None:
    _datas_dir(tmp_path)
    book = datas.open_book(tmp_path)
    assert book is not None
    assert book.name(115001) == "似似花(新年)"
    assert book.nickname(115001) == "春花"
    assert book.label(115001) == "春花"
    assert book.name(136001) == "角色1360"       # chara_name 里没有
    assert book.label(136001) == "涅妃"          # nickname.json 兜底
    assert book.label(100101) == "猫拳"          # UnitNickNameDic 优先
    assert book.label(999999) == "角色9999"


def test_characters_skip_the_ones_without_a_real_name(tmp_path: Path) -> None:
    _datas_dir(tmp_path)
    info = {
        100101: datas.UnitInfo(role=1, talent=1, width=200),
        136001: datas.UnitInfo(role=7, talent=4, width=732),
    }
    book = datas.open_book(tmp_path, info=info)
    assert book is not None
    ids = [character.unit_id for character in book.characters()]
    assert 100101 in ids and 136001 in ids
    assert 999999 not in ids
    assert book.characters()[0].base_id == 1001


def test_enemies_are_loaded(tmp_path: Path) -> None:
    _datas_dir(tmp_path)
    book = datas.open_book(tmp_path)
    assert book is not None
    assert book.enemies == [401010101, 401010102]


def test_icon_codes_only_use_the_11_31_61_suffixes_and_skip_big_ids() -> None:
    assert datas.icon_codes(115001) == ["115061", "115031", "115011"]  # 优先最大的那套
    assert datas.icon_codes(100101) == ["100161", "100131", "100111"]
    assert datas.icon_codes(0) == []
    assert datas.icon_codes(-5) == []
    assert datas.icon_codes(datas.UNIT_LIMIT) == []
    assert datas.icon_codes(200001) == []


def test_cached_icon_accepts_any_cached_suffix(tmp_path: Path, monkeypatch) -> None:
    icon_root = tmp_path / "icons"
    icon_root.mkdir()
    monkeypatch.setattr(datas, "icon_dir", lambda: icon_root)
    assert datas.cached_icon(115001) is None
    cached = icon_root / "115031.webp"
    cached.write_bytes(b"webp")
    assert datas.cached_icon(115001) == cached


class _FakeReply:
    def __init__(self, ok: bool) -> None:
        self.status_code = 200 if ok else 404
        self.content = b"webp" if ok else b""


class _FakeClient:
    """只认 ``…/115031.webp`` 这一条，其它一律 404。"""

    def __init__(self, answers: dict[str, bool]) -> None:
        self.answers = answers
        self.seen: list[str] = []

    def get(self, url: str) -> _FakeReply:
        self.seen.append(url)
        code = url.rsplit("/", 1)[-1].removesuffix(".webp")
        return _FakeReply(self.answers.get(code, False))


def test_download_icons_skips_cache_and_reports_progress(tmp_path: Path, monkeypatch) -> None:
    icon_root = tmp_path / "icons"
    icon_root.mkdir()
    monkeypatch.setattr(datas, "icon_dir", lambda: icon_root)
    (icon_root / "100111.webp").write_bytes(b"cached")
    client = _FakeClient({"115011": False, "115031": True})
    monkeypatch.setattr(datas, "_client_pool", type("Pool", (), {"get": lambda _self: client})())
    events: list[tuple[int, int, int]] = []
    downloaded, skipped, failed = datas.download_icons(
        [100101, 115001, 999999999],
        workers=2,
        progress=lambda done, total, unit: events.append((done, total, unit)),
        log=lambda *_args: None,
    )
    assert (downloaded, skipped, failed) == (1, 1, 0)
    assert (icon_root / "115031.webp").read_bytes() == b"webp"
    assert events[-1][:2] == (1, 1)
    assert not any("999999999" in url for url in client.seen)


def test_import_icon_pack_unpacks_webp_only(tmp_path: Path, monkeypatch) -> None:
    icon_root = tmp_path / "icons"
    monkeypatch.setattr(datas, "icon_dir", lambda: icon_root)
    pack = tmp_path / "icon.zip"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("100111.webp", b"one")
        archive.writestr("folder/115011.webp", b"two")
        archive.writestr("readme.txt", b"skip")
    assert datas.import_icon_pack(pack, log=lambda *_args: None) == 2
    assert (icon_root / "100111.webp").read_bytes() == b"one"
    assert (icon_root / "115011.webp").read_bytes() == b"two"
    assert not (icon_root / "readme.txt").exists()


def test_missing_book_returns_none(tmp_path: Path) -> None:
    assert datas.open_book(None) is None
    assert datas.open_book(tmp_path / "nothing") is None


def test_broken_json_file_is_tolerated(tmp_path: Path) -> None:
    root = _datas_dir(tmp_path)
    (root / "nickname.json").write_text("{not json", encoding="utf-8")
    book = datas.open_book(tmp_path)
    assert book is not None
    assert book.label(100101) == "猫拳"
    assert book.label(136001) == "角色1360"
