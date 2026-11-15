"""Wire-level checks of the ``/simulate`` exchange, without a live server.

``httpx.MockTransport`` replaces the socket, so these tests assert exactly what
leaves our process (method, URL, JSON body) and how a real-shaped reply is
parsed.  That keeps the request contract testable on a machine where the
reference server cannot boot.
"""

from __future__ import annotations

import json

import httpx
import pytest

from PcrTimelineTester import client as client_mod
from PcrTimelineTester.client import FULL_CALC_STATS, SimulatorClient, TuneError

CONTEXT = {
    "PlayerList": [{"unitId": 1001 + index, "level": 200} for index in range(5)],
    "EnemyList": [],
    "GuildEnemyList": [800100],
    "Settings": {"RandomSeed": 666, "EnableMiss": False},
    "TalentSkillLevels": {},
    "TalentSkillNodeLevels": {},
    "MasterSkillNodeLevels": {},
    "RoleMastery": {},
    "TalentFullInfo": True,
    "IsTalentQuest": False,
    "ToMaxUnitAttr": False,
    "IsGuildBattle": True,
    "BattleTime": 90,
    "WaveGroupId": 1,
    "SetToggles": [],
    "NeedOnceResult": False,
}
# Every key the tool may put on the wire; anything else is a contract break.
CONTRACT = set(CONTEXT) | {
    "CalcStats", "IsSkipping", "UBExecTimeList", "SemanUBExecTimeList", "SemanUBSequenceData",
    "UseSequenceUB", "IsSemanMode", "IsAutoMode", "IsSetMode",
}
AXIS = [[470.0, 1000.0], [500.0, 1100.0], [600.0, 1200.0], [700.0, 1300.0], [800.0, 1400.0]]


def frame_of(body: dict) -> int:
    """Read back the tuned frame from the wire body (first row, first cell)."""
    return int(body["UBExecTimeList"][0][0])


def reply(body: dict, damage: float, *, seed: int | None = None, stats: bool = True) -> dict:
    """A response item shaped like PCRCalculatorServer's."""
    return {
        "Success": True,
        "Message": None,
        "CurrentSeed": body["Settings"]["RandomSeed"] if seed is None else seed,
        "BattleFrames": 5400,
        "Result": 0,
        "SemanUBNum": 0,
        "Stats": {"TotalDamage": damage, "PlayerDamageList": [damage / 5] * 5} if stats else None,
    }


class Server:
    """Records every request and replies with ``damage_for(body)``."""

    def __init__(self, damage_for=lambda body: frame_of(body) * 10.0, *, stats=True, status=200):
        self.damage_for = damage_for
        self.stats = stats
        self.status = status
        self.calls: list[list[dict]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.calls.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        return httpx.Response(200, json=[reply(item, self.damage_for(item), stats=self.stats) for item in body])


REAL_CLIENT = httpx.Client  # captured before any patch, so installs never nest


def install(monkeypatch, handler) -> None:
    """Route the client's own httpx.Client through a mock transport."""

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", factory)


def make(server: Server, monkeypatch, **kwargs) -> SimulatorClient:
    install(monkeypatch, server)
    return SimulatorClient(context=CONTEXT, **kwargs)


def test_request_body_matches_server_contract(monkeypatch):
    server = Server()
    with make(server, monkeypatch) as client:
        client.evaluate_one(AXIS)

    assert server.requests[0].method == "POST"
    assert str(server.requests[0].url) == "http://127.0.0.1:8080/simulate"
    body = server.calls[0]
    # An array body, one object per battle.
    assert isinstance(body, list) and len(body) == 1
    item = body[0]
    assert set(item) <= CONTRACT, f"请求里出现契约外的字段：{sorted(set(item) - CONTRACT)}"
    assert CONTRACT - set(item) == set(), "请求缺少契约字段"
    assert item["UBExecTimeList"] == AXIS
    assert item["Settings"]["RandomSeed"] == 666
    assert item["Settings"]["UseFixedRandomSeed"] is True
    assert item["Settings"]["EnableMiss"] is False
    assert item["IsSkipping"] is True
    assert item["IsSemanMode"] is False and item["IsGuildBattle"] is True
    assert item["SemanUBExecTimeList"] == [] and item["SemanUBSequenceData"] == []
    assert item["BattleTime"] == 90 and item["WaveGroupId"] == 1
    assert len(item["PlayerList"]) == 5
    assert item["GuildEnemyList"] == [800100]
    assert item["CalcStats"] == 3
    assert item["NeedOnceResult"] is True   # 期望伤害要靠 OnceResult

def test_metric_prefers_expected_damage(monkeypatch):
    """期望伤害来自 OnceResult.exceptDamage；实战伤害仍然单独保留。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        items = []
        for item in body:
            reply_item = reply(item, 1000.0)
            reply_item["OnceResult"] = {
                "id": 0, "randomSeed": 666, "currentDamage": 1000, "criticalEX": 100,
                "exceptDamage": 2500, "backTime": 1, "backTimeText": "剩1s",
                "warnings": [], "BattleResult": 0,
            }
            items.append(reply_item)
        return httpx.Response(200, json=items)

    install(monkeypatch, handler)
    with SimulatorClient(context=CONTEXT) as client:
        outcome = client.evaluate_one(AXIS)
    assert outcome.metric == 2500.0
    assert outcome.expected == 2500.0 and outcome.is_expected is True
    assert outcome.damage == 1000


def test_metric_falls_back_to_sampled_damage(monkeypatch):
    """摸轴器没有期望伤害（DONT_USE_EX 或没返回 OnceResult）时用实战伤害。"""
    server = Server()
    with make(server, monkeypatch) as client:
        outcome = client.evaluate_one(AXIS)
    assert outcome.expected is None and outcome.is_expected is False
    assert outcome.metric == 4700.0 and outcome.damage == 4700

def test_batches_split_by_batch_size(monkeypatch):
    server = Server()
    candidates = [[[float(frame)], *[[10.0]] * 4] for frame in (400, 410, 420, 430, 440)]
    with make(server, monkeypatch, batch_size=2) as client:
        outcomes = client.evaluate(candidates)

    assert [len(call) for call in server.calls] == [2, 2, 1]
    assert client.battles == 5
    assert [item.damage for item in outcomes] == [4000, 4100, 4200, 4300, 4400]
    assert candidates[0][0][0] == 400.0  # caller's table stays untouched


def test_samples_average_over_seeds(monkeypatch):
    server = Server()
    with make(server, monkeypatch, seeds=(666, 777), batch_size=4) as client:
        outcome = client.evaluate_one(AXIS)

    assert [item["Settings"]["RandomSeed"] for call in server.calls for item in call] == [666, 777]
    assert outcome.damage == 4700
    assert outcome.metric == pytest.approx(4700.0)
    assert outcome.result == 0 and outcome.battle_frames == 5400


def test_missing_stats_retries_once_with_full_calc_stats(monkeypatch):
    server = Server(stats=False)
    with make(server, monkeypatch) as client:
        with pytest.raises(TuneError):
            client.evaluate_one(AXIS)
        assert client.calc_stats == FULL_CALC_STATS
    assert [call[0]["CalcStats"] for call in server.calls] == [3, FULL_CALC_STATS]

    server = Server(stats=False)
    with make(server, monkeypatch, calc_stats=FULL_CALC_STATS) as client:
        with pytest.raises(TuneError, match="CalcStats 已经是 511"):
            client.evaluate_one(AXIS)
        assert len(server.calls) == 1


def test_http_status_becomes_readable_error(monkeypatch):
    server = Server(status=400)
    with make(server, monkeypatch) as client:
        with pytest.raises(TuneError, match="接口返回 HTTP 400"):
            client.evaluate_one(AXIS)


def test_reply_must_be_array_of_request_length(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    install(monkeypatch, handler)
    with SimulatorClient(context=CONTEXT) as client:
        with pytest.raises(TuneError, match="接口返回数量与请求不匹配"):
            client.evaluate_one(AXIS)


def test_battle_failure_and_seed_mismatch_abort(monkeypatch):
    def failure(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        item = reply(body[0], 0.0)
        item.update(Success=False, Message="NoGameData")
        return httpx.Response(200, json=[item])

    install(monkeypatch, failure)
    with SimulatorClient(context=CONTEXT) as client:
        with pytest.raises(TuneError, match="战斗失败（第 1 项）：NoGameData"):
            client.evaluate_one(AXIS)

    def wrong_seed(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=[reply(body[0], 100.0, seed=1)])

    install(monkeypatch, wrong_seed)
    with SimulatorClient(context=CONTEXT) as client:
        with pytest.raises(TuneError, match="CurrentSeed"):
            client.evaluate_one(AXIS)
