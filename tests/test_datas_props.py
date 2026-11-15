"""角色数据：图标编码（< 190000）与 unit_info.php 的职能 / 属性 / 站位宽度。"""

from __future__ import annotations

import json

import httpx

from PcrTimelineTester import datas


def test_icon_codes_respect_the_190000_limit():
    assert datas.UNIT_LIMIT == 190_000
    assert datas.icon_codes(115001) == ["115061", "115031", "115011"]  # 优先最大的那套
    assert datas.icon_codes(190001) == []
    assert datas.icon_codes(200000) == []
    assert datas.icon_codes(0) == []


def test_unit_info_payload_is_parsed():
    payload = {
        "115001": {"talent": "3", "role": "3", "search_area_width": "434"},
        "134901": {"talent": 1, "role": 2, "search_area_width": 196},
        "190001": {"talent": "1", "role": "1", "search_area_width": "250"},  # 超过 190000，丢掉
        "999999": {"talent": "x", "role": "y"},  # 什么都没有
        "bad": {"talent": "1", "role": "1", "search_area_width": "1"},
    }
    table = datas._info_from_payload(payload)
    assert table[115001] == datas.UnitInfo(role=3, talent=3, width=434)
    assert table[134901] == datas.UnitInfo(role=2, talent=1, width=196)
    assert 190001 not in table and "bad" not in table and 999999 not in table
    assert datas.role_label(3) == "增益型"
    assert datas.talent_label(1) == "火"


def test_unit_info_page_is_parsed_with_or_without_a_js_wrapper():
    plain = '{"100101": {"talent": "1", "role": "1", "search_area_width": "200"}}'
    assert datas._parse_unit_info(plain)[100101] == datas.UnitInfo(role=1, talent=1, width=200)
    wrapped = (
        "var x = 1;\n"
        'const unit_info = `{"100201": {"talent": "2", "role": "3", "search_area_width": "300"}}`;\n'
    )
    assert datas._parse_unit_info(wrapped)[100201] == datas.UnitInfo(role=3, talent=2, width=300)
    assert datas._parse_unit_info("") == {}
    assert datas._parse_unit_info("没有这东西") == {}


def test_unit_info_is_cached_and_reused(tmp_path, monkeypatch):
    cache = tmp_path / "unit_info.json"
    cache.write_text(
        json.dumps({"100101": {"role": 1, "talent": 2, "search_area_width": 210}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(datas, "unit_info_path", lambda: cache)

    def boom(*_args, **_kwargs):
        raise AssertionError("有缓存时不该联网")

    monkeypatch.setattr(datas.httpx.Client, "get", boom)
    table = datas.load_unit_info(log=lambda *_: None)
    assert table[100101] == datas.UnitInfo(role=1, talent=2, width=210)


def test_unit_info_falls_back_to_cache_when_the_network_fails(tmp_path, monkeypatch):
    cache = tmp_path / "unit_info.json"
    cache.write_text(
        json.dumps({"100101": {"role": 2, "talent": 3, "search_area_width": 220}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(datas, "unit_info_path", lambda: cache)

    def offline(*_args, **_kwargs):
        raise httpx.HTTPError("断网")

    monkeypatch.setattr(datas.httpx.Client, "get", offline)
    messages: list[str] = []
    table = datas.load_unit_info(refresh=True, log=messages.append)
    assert table[100101] == datas.UnitInfo(role=2, talent=3, width=220)
    assert messages and "先用本地缓存" in messages[0]


def test_unit_info_is_fetched_once_and_written_to_the_cache(tmp_path, monkeypatch):
    cache = tmp_path / "sub" / "unit_info.json"  # 目录不存在也要能建
    monkeypatch.setattr(datas, "unit_info_path", lambda: cache)
    body = json.dumps({"100101": {"role": "1", "talent": "4", "search_area_width": "205"}})

    class _Reply:
        text = body

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(datas.httpx.Client, "get", lambda *_args, **_kwargs: _Reply())
    table = datas.load_unit_info(log=lambda *_: None)
    assert table[100101] == datas.UnitInfo(role=1, talent=4, width=205)
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["100101"] == {"role": 1, "talent": 4, "search_area_width": 205}
    # 缓存里回来也是同一张表
    assert datas._load_unit_info_file()[100101] == datas.UnitInfo(role=1, talent=4, width=205)


def test_unit_width_reads_from_the_unit_info_table(monkeypatch):
    monkeypatch.setattr(
        datas,
        "_info_table",
        {
            134901: datas.UnitInfo(role=2, talent=1, width=196),
            136001: datas.UnitInfo(role=7, talent=4, width=732),
            128701: datas.UnitInfo(role=1, talent=3, width=0),  # 没有宽度
            1362: datas.UnitInfo(role=3, talent=1, width=198),  # 4 位键也认
        },
    )
    assert datas.unit_width(134901) == 196  # 花音
    assert datas.unit_width(136001) == 732  # 蜘蛛（最靠后）
    assert datas.unit_width(128701) is None  # 宽度是 0，当成不知道
    assert datas.unit_width(136201) == 198  # 4 位键兜底
    assert datas.unit_width(0) is None
    assert datas.unit_width(100101) is None  # 表里没有


def test_databook_reports_the_characters_without_info(tmp_path):
    """Datas 里有的角色、资料表里却没有，要能被发现（界面据此重抓一次）。"""
    datas_dir = tmp_path / "PCRGuildCalculator_Data" / "StreamingAssets" / "Datas"
    datas_dir.mkdir(parents=True)
    (datas_dir / "chara_name.json").write_text(
        json.dumps({"1001": ["日和"], "1002": ["优衣"], "1003": ["怜"]}),
        encoding="utf-8",
    )
    (datas_dir / "UnitNickNameDic.json").write_text(
        json.dumps({"100101": "猫拳", "100201": "优衣", "100301": "怜"}),
        encoding="utf-8",
    )
    book = datas.DataBook(
        datas_dir,
        info={
            100101: datas.UnitInfo(role=1, talent=1, width=200),
            100301: datas.UnitInfo(role=2, talent=0, width=300),  # 只有职能、没有属性
        },
    )
    assert book.units_without_info() == [100201, 100301]
    assert [item.unit_id for item in book.characters()] == [100101]
    assert datas.DataBook(datas_dir, info={}).units_without_info() == [100101, 100201, 100301]


def test_databook_splits_by_role_and_talent(tmp_path):
    datas_dir = tmp_path / "PCRGuildCalculator_Data" / "StreamingAssets" / "Datas"
    datas_dir.mkdir(parents=True)
    (datas_dir / "chara_name.json").write_text(
        json.dumps({"1150": ["似似花(新年)", "春花"], "1349": ["花音"]}), encoding="utf-8"
    )
    (datas_dir / "UnitNickNameDic.json").write_text(
        json.dumps({"115001": "春花", "134901": "花音"}), encoding="utf-8"
    )
    book = datas.DataBook(
        datas_dir,
        info={
            115001: datas.UnitInfo(role=3, talent=3, width=434),
            134901: datas.UnitInfo(role=2, talent=1, width=196),
        },
    )
    characters = {item.unit_id: item for item in book.characters()}
    assert characters[115001].group_label == "增益型·风"
    assert characters[134901].group_label == "破防型·火"
    assert book.role_of(115001) == 3 and book.talent_of(115001) == 3
    assert book.width_of(115001) == 434
    assert book.role_of(999999) == 0
    assert book.width_of(999999) is None
