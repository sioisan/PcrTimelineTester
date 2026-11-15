"""The /simulate wire adapter."""

from __future__ import annotations

import pytest

from PcrTimelineTester.client import (
    DEFAULT_CALC_STATS,
    FULL_CALC_STATS,
    SimulatorClient,
    TuneError,
    simulation_url,
)


def context() -> dict:
    return {
        "PlayerList": [{"unitId": 1001, "level": 250}],
        "EnemyList": [],
        "GuildEnemyList": [800100],
        "Settings": {"RandomSeed": 0},
        "BattleTime": 30,
        "IsGuildBattle": True,
    }


def test_simulation_url_defaults_to_the_simulate_path() -> None:
    assert simulation_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080/simulate"
    assert simulation_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/simulate"
    assert simulation_url("https://host:9000/simulate/") == "https://host:9000/simulate"
    for bad in ("ftp://host", "http://user:pw@host/simulate", "http://host/simulate?x=1", "http://host#frag"):
        with pytest.raises(ValueError):
            simulation_url(bad)


def test_request_carries_the_frame_axis_and_the_seed() -> None:
    client = SimulatorClient(context=context(), seeds=(666,))
    try:
        request = client.request_for([[470.0, 1000.0]], 1234)
    finally:
        client.close()
    assert request["UBExecTimeList"] == [[470.0, 1000.0]]
    assert request["SemanUBExecTimeList"] == []
    assert request["UseSequenceUB"] is False
    assert request["IsSemanMode"] is False
    assert request["IsSkipping"] is True
    assert request["CalcStats"] == DEFAULT_CALC_STATS
    assert request["Settings"]["RandomSeed"] == 1234
    assert request["Settings"]["UseFixedRandomSeed"] is True
    assert request["IsGuildBattle"] is True


def test_evaluate_averages_the_sample_seeds() -> None:
    client = SimulatorClient(context=context(), seeds=(666, 667), batch_size=1)
    batches: list[int] = []

    def fake_post(requests: list[dict]) -> list[dict]:
        batches.append(len(requests))
        out = []
        for request in requests:
            seed = request["Settings"]["RandomSeed"]
            out.append({
                "Success": True,
                "CurrentSeed": seed,
                "Stats": {"TotalDamage": 1000 if seed == 666 else 1200},
                "BattleFrames": 5400,
                "Result": 0,
            })
        return out

    client._post = fake_post  # type: ignore[method-assign]
    try:
        outcomes = client.evaluate([[[470.0]], [[480.0]]])
    finally:
        client.close()
    assert [outcome.metric for outcome in outcomes] == [1100.0, 1100.0]
    assert [outcome.damage for outcome in outcomes] == [1000, 1000]
    assert batches == [4]  # _post 被替换，桩函数一次收齐 4 个请求


def test_battle_failure_is_fatal_for_the_group() -> None:
    client = SimulatorClient(context=context())
    client._post = lambda requests: [
        {"Success": False, "Message": "角色不合法"} for _ in requests
    ]  # type: ignore[method-assign]
    try:
        with pytest.raises(TuneError, match="战斗失败（第 1 项）：角色不合法"):
            client.evaluate([[[470.0]]])
    finally:
        client.close()


def test_seed_mismatch_is_rejected() -> None:
    client = SimulatorClient(context=context(), seeds=(666,))
    client._post = lambda requests: [
        {"Success": True, "CurrentSeed": 5, "Stats": {"TotalDamage": 1}, "BattleFrames": 1, "Result": 0}
        for _ in requests
    ]  # type: ignore[method-assign]
    try:
        with pytest.raises(TuneError, match="CurrentSeed"):
            client.evaluate([[[470.0]]])
    finally:
        client.close()


def test_missing_stats_retries_once_with_full_calc_stats() -> None:
    client = SimulatorClient(context=context(), seeds=(666,), log=lambda _: None)
    state = {"first": True}
    seen: list[int] = []

    def fake_post(requests: list[dict]) -> list[dict]:
        out = []
        for request in requests:
            seen.append(request["CalcStats"])
            if state["first"]:
                out.append({"Success": True, "CurrentSeed": 666})
            else:
                assert request["CalcStats"] == FULL_CALC_STATS
                out.append({
                    "Success": True, "CurrentSeed": 666,
                    "Stats": {"TotalDamage": 900}, "BattleFrames": 5400, "Result": 0,
                })
        state["first"] = False
        return out

    client._post = fake_post  # type: ignore[method-assign]
    try:
        outcome = client.evaluate_one([[470.0]])
    finally:
        client.close()
    assert outcome.metric == 900.0
    assert client.calc_stats == FULL_CALC_STATS
    assert seen == [DEFAULT_CALC_STATS, FULL_CALC_STATS]
