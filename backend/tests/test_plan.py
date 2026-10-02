"""联合隔离计划验收测试。

验收 1 —— 两个可兼容区域（设备 U、W）生成去重后的共享关阀方案，
           且全部要求保供的供给点仍可达。
验收 2 —— 锁定关键阀 / 相互矛盾保供要求时整个计划无解，
           显示各自残余/锁阀见证，且未部分应用任何方案。
验收 3 —— 重复提交同一目标（幂等键重试 / 单请求内重复区域）
           不会增加第二套阀门或重复审计。
验收 4 —— 先释放一个区域后共享阀保持关闭；最后一个区域释放并
           刷新后才按计划恢复，且历史快照可查看。
"""
from __future__ import annotations

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.isolation import _load, build_graph, compute_joint_isolation
from app.main import app
from app.seed import reset_database, seed_database


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()
    with TestClient(app) as c:
        yield c


def _valve_states(client) -> dict[str, bool]:
    topo = client.get("/api/topology").json()
    return {v["id"]: v["is_open"] for v in topo["valves"]}


def _graph(db, closed):
    _, edges = _load(db)
    return build_graph(edges, set(closed))


# 两个兼容区域：设备 U、W 并联于支管，均要求 P1/P2/P3 保供
COMPATIBLE_ZONES = [
    {"target_id": "U", "essentials": ["P1", "P2", "P3"]},
    {"target_id": "W", "essentials": ["P1", "P2", "P3"]},
]


# ---------------- 验收 1：兼容区域 -> 去重共享方案，供给点全可达 ----------------


def test_joint_plan_dedup_shared_valves_and_supply(client):
    r = client.post(
        "/api/plans",
        json={"name": "U+W联合检修", "request_key": "acc-1", "zones": COMPATIBLE_ZONES},
    )
    assert r.status_code == 200, r.text
    plan = r.json()

    assert plan["feasible"] is True
    assert plan["status"] == "prepared"

    # 单独隔离 U 需 {V_UIN,V_UOUT}、W 需 {V_WIN,V_WOUT}（并集 4 阀）；
    # 联合方案去重后仅 3 阀：两只入口阀 + 共享回联阀 V_LK3
    assert plan["close_valves"] == ["V_LK3", "V_UIN", "V_WIN"]
    assert len(plan["close_valves"]) < 4
    assert "V_LK3" in plan["shared_valves"]

    solve = plan["solve"]
    # 共享阀门归因：V_LK3 同时被两个区域依赖
    assert set(solve["valve_zones"]["V_LK3"]) == {"U", "W"}
    # 每个区域都有隔离证据：边界阀 + 与来源断开确认
    evidence = {z["target_id"]: z for z in solve["zone_evidence"]}
    for t in ("U", "W"):
        assert evidence[t]["isolated"] is True
        assert "V_LK3" in evidence[t]["boundary_valves"]

    # 每个要求保供的供给点都有保供路径
    for p in ("P1", "P2", "P3"):
        path = solve["supply_paths"][p]
        assert path and path[0] == "SRC" and path[-1] == p

    # 在图上验证：联合集合关闭后目标断开、供给点全部可达
    db = SessionLocal()
    g = build_graph(_load(db)[1], set(plan["close_valves"]))
    for t in ("U", "W"):
        assert not nx.has_path(g, "SRC", t)
    for p in ("P1", "P2", "P3"):
        assert nx.has_path(g, "SRC", p)
    db.close()

    # 计划未执行前不得改动任何阀门状态
    assert all(_valve_states(client).values())


def test_joint_plan_smaller_than_sum_of_single_plans(client):
    """联合方案阀门数 < 两个单目标方案并集（去重收益）。"""
    single_u = client.post("/api/isolation", json={"target_id": "U"}).json()
    single_w = client.post("/api/isolation", json={"target_id": "W"}).json()
    union = set(single_u["best_solution"]) | set(single_w["best_solution"])
    assert len(union) == 4  # {V_UIN,V_UOUT} ∪ {V_WIN,V_WOUT}

    plan = client.post(
        "/api/plans", json={"request_key": "acc-1b", "zones": COMPATIBLE_ZONES}
    ).json()
    assert len(plan["close_valves"]) == 3 < len(union)


# ---------------- 验收 2：锁阀 / 矛盾需求 -> 整体无解 + 见证，不部分应用 ----------------


def test_locked_key_valve_makes_plan_infeasible_with_witnesses(client):
    client.post("/api/valves/V_UIN/lock", json={"locked": True})
    r = client.post(
        "/api/plans", json={"request_key": "acc-2a", "zones": COMPATIBLE_ZONES}
    )
    assert r.status_code == 200, r.text
    plan = r.json()

    # 整个计划无解：不给出任何可执行的关阀集合
    assert plan["feasible"] is False
    assert plan["status"] == "infeasible"
    assert plan["close_valves"] == []

    solve = plan["solve"]
    # 各自见证：U 的残余路径直接经过被锁的 V_UIN；
    # W 的最短残余未过锁阀，但另有经 V_UIN 的见证路径
    residuals = {z["target_id"]: z for z in solve["zone_residuals"]}
    assert "V_UIN" in residuals["U"]["residual_path"]["locked_valves_on_path"]
    assert residuals["U"]["residual_path"]["nodes"][-1] == "U"
    witness = residuals["W"]["locked_witness_path"]
    assert witness is not None
    assert "V_UIN" in witness["locked_valves_on_path"]
    assert witness["nodes"][-1] == "W"

    # 断供诊断：隔离 U 只能断支管联络阀 V_BR，必然断供 P3
    ub = solve["unconstrained_best"]
    assert ub is not None
    assert "P3" in ub["unavoidable_essentials"]
    assert "P3" in solve["infeasible_reason"]
    assert "V_UIN" in solve["infeasible_reason"]

    # 未部分应用：除手动锁定外所有阀门仍打开
    states = _valve_states(client)
    assert all(states.values())
    # 无解计划不可执行
    assert client.post(f"/api/plans/{plan['id']}/execute").status_code == 409


def test_contradictory_supply_demands_make_plan_infeasible(client):
    """区域一检修管汇 N3（任何切法必然断供 P2），区域二却要求 P2 保供。"""
    zones = [
        {"target_id": "N3", "essentials": ["P1"]},
        {"target_id": "T", "essentials": ["P2"]},
    ]
    r = client.post("/api/plans", json={"request_key": "acc-2b", "zones": zones})
    assert r.status_code == 200, r.text
    plan = r.json()

    assert plan["feasible"] is False
    assert plan["close_valves"] == []
    solve = plan["solve"]
    # 矛盾见证：P2 在任何能同时隔离 N3、T 的切法下都不可避免断供
    ub = solve["unconstrained_best"]
    assert ub["unavoidable_essentials"] == ["P2"]
    assert "P2" in solve["infeasible_reason"]
    assert "矛盾" in solve["infeasible_reason"]
    # 每个区域都给出当前残余路径
    assert {z["target_id"] for z in solve["zone_residuals"]} == {"N3", "T"}
    # 未部分应用：阀门全部保持打开
    assert all(_valve_states(client).values())


def test_duplicate_target_rejected_without_partial_application(client):
    zones = [
        {"target_id": "U", "essentials": ["P1"]},
        {"target_id": "U", "essentials": ["P2"]},
    ]
    r = client.post("/api/plans", json={"request_key": "acc-2c", "zones": zones})
    assert r.status_code == 400
    assert "重复目标" in r.json()["detail"]
    # 未创建计划、未改动阀门
    assert client.get("/api/plans").json() == []
    assert all(_valve_states(client).values())


# ---------------- 验收 3：重复提交幂等，不增加第二套阀门或重复审计 ----------------


def test_repeated_submission_is_idempotent(client):
    body = {"name": "幂等计划", "request_key": "acc-3", "zones": COMPATIBLE_ZONES}
    first = client.post("/api/plans", json=body).json()
    second = client.post("/api/plans", json=body).json()

    # 同一计划，不新增第二套阀门
    assert first["id"] == second["id"]
    assert len(client.get("/api/plans").json()) == 1
    assert second["close_valves"] == first["close_valves"]

    # 不重复审计：只有一条 created 事件
    events = client.get(f"/api/plans/{first['id']}").json()["events"]
    assert [e["action"] for e in events] == ["created"]

    # 重复执行同样幂等
    client.post(f"/api/plans/{first['id']}/execute")
    client.post(f"/api/plans/{first['id']}/execute")
    events = client.get(f"/api/plans/{first['id']}").json()["events"]
    assert [e["action"] for e in events] == ["created", "executed"]
    closed = [v for v, open_ in _valve_states(client).items() if not open_]
    assert sorted(closed) == ["V_LK3", "V_UIN", "V_WIN"]


# ---------------- 验收 4：共享阀最后恢复，历史快照可查 ----------------


def test_release_keeps_shared_valves_until_last_zone(client):
    plan = client.post(
        "/api/plans", json={"request_key": "acc-4", "zones": COMPATIBLE_ZONES}
    ).json()
    client.post(f"/api/plans/{plan['id']}/execute")
    states = _valve_states(client)
    assert not states["V_UIN"] and not states["V_WIN"] and not states["V_LK3"]

    zones = {z["target_id"]: z for z in plan["zones"]}

    # 先释放区域 U：并联结构下每只计划阀门仍被 W 依赖（共享），全部保持关闭
    r1 = client.post(f"/api/plans/{plan['id']}/zones/{zones['U']['id']}/release")
    assert r1.status_code == 200
    plan1 = r1.json()
    assert plan1["status"] == "executing"
    assert {z["target_id"]: z["status"] for z in plan1["zones"]} == {
        "U": "released",
        "W": "executing",
    }
    states = _valve_states(client)
    assert not states["V_LK3"], "共享阀 V_LK3 必须保持关闭"
    assert not states["V_UIN"] and not states["V_WIN"]

    # 重复释放幂等：状态不变、不新增审计事件
    again = client.post(f"/api/plans/{plan['id']}/zones/{zones['U']['id']}/release")
    assert again.status_code == 200
    events = again.json()["events"]
    assert [e["action"] for e in events] == ["created", "executed", "zone_released"]

    # 释放最后一个区域 W 并刷新：全部计划阀门按计划恢复
    client.post(f"/api/plans/{plan['id']}/zones/{zones['W']['id']}/release")
    states = _valve_states(client)  # 重新拉取拓扑 = 刷新
    assert states["V_LK3"] and states["V_UIN"] and states["V_WIN"]
    final = client.get(f"/api/plans/{plan['id']}").json()
    assert final["status"] == "released"
    assert all(z["status"] == "released" for z in final["zones"])

    # 历史快照可查看：完整审计轨迹，且每步带计划快照
    actions = [e["action"] for e in final["events"]]
    assert actions == [
        "created",
        "executed",
        "zone_released",
        "zone_released",
        "plan_released",
    ]
    executed_evt = next(e for e in final["events"] if e["action"] == "executed")
    assert executed_evt["detail"]["applied_close_valves"] == ["V_LK3", "V_UIN", "V_WIN"]
    first_release = next(e for e in final["events"] if e["action"] == "zone_released")
    # 快照记录：第一次释放后共享阀仍全部关闭
    assert first_release["detail"]["still_closed_valves"] == ["V_LK3", "V_UIN", "V_WIN"]
    assert first_release["detail"]["restored_valves"] == []
    # 创建时的求解快照完整保留
    assert final["solve"]["close_valves"] == ["V_LK3", "V_UIN", "V_WIN"]
    assert final["solve"]["shared_valves"]


def test_private_valves_restore_early_when_not_shared(client):
    """三区域计划：T 的边界阀为专有（先释放先恢复），支管阀为 U/W 共享。"""
    zones = COMPATIBLE_ZONES + [{"target_id": "T", "essentials": ["P1", "P2", "P3"]}]
    plan = client.post("/api/plans", json={"request_key": "acc-4b", "zones": zones}).json()
    assert plan["close_valves"] == ["V_LK3", "V_TIN", "V_TOUT", "V_UIN", "V_WIN"]
    solve = plan["solve"]
    # T 的边界阀只被 T 依赖（专有），V_LK3 被 U/W 共享
    assert solve["valve_zones"]["V_TIN"] == ["T"]
    assert set(solve["valve_zones"]["V_LK3"]) == {"U", "W"}

    client.post(f"/api/plans/{plan['id']}/execute")
    zmap = {z["target_id"]: z for z in plan["zones"]}
    # 先释放 T：其专有阀 V_TIN/V_TOUT 立即恢复，支管共享阀保持关闭
    client.post(f"/api/plans/{plan['id']}/zones/{zmap['T']['id']}/release")
    states = _valve_states(client)
    assert states["V_TIN"] and states["V_TOUT"]
    assert not states["V_LK3"] and not states["V_UIN"] and not states["V_WIN"]
    # 释放 U：共享阀仍被 W 依赖，保持关闭
    client.post(f"/api/plans/{plan['id']}/zones/{zmap['U']['id']}/release")
    assert not _valve_states(client)["V_LK3"]
    # 释放最后的 W：全部恢复
    client.post(f"/api/plans/{plan['id']}/zones/{zmap['W']['id']}/release")
    assert all(_valve_states(client).values())


# ---------------- 求解器单元级校验 ----------------


def test_solver_rejects_unknown_and_conflicting_nodes(client):
    db = SessionLocal()
    seed_database(db)
    with pytest.raises(Exception, match="目标节点不存在"):
        compute_joint_isolation(db, [{"target_id": "NOPE", "essentials": []}])
    with pytest.raises(Exception, match="介质来源"):
        compute_joint_isolation(db, [{"target_id": "SRC", "essentials": []}])
    with pytest.raises(Exception, match="未标记为必要供给点"):
        compute_joint_isolation(db, [{"target_id": "U", "essentials": ["N2"]}])
    with pytest.raises(Exception, match="冲突"):
        compute_joint_isolation(db, [{"target_id": "P1", "essentials": ["P1"]}])
    db.close()


def test_reset_clears_plans_and_restores_valves(client):
    plan = client.post(
        "/api/plans", json={"request_key": "acc-reset", "zones": COMPATIBLE_ZONES}
    ).json()
    client.post(f"/api/plans/{plan['id']}/execute")
    client.post("/api/reset")
    assert client.get("/api/plans").json() == []
    assert all(_valve_states(client).values())
