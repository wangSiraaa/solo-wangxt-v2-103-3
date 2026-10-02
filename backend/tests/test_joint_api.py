"""联合隔离计划的 FastAPI 端到端测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import reset_database, seed_database


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


REGIONS_TU = [
    {"target_id": "T", "supply_node_ids": None},
    {"target_id": "U", "supply_node_ids": None},
]


def test_joint_plan_full_lifecycle():
    _reset_seed()
    with TestClient(app) as client:
        # 登记
        r = client.post("/api/joint-plans", json={"regions": REGIONS_TU})
        assert r.status_code == 200, r.text
        plan = r.json()
        assert plan["feasible"] is True
        assert plan["status"] == "prepared"
        assert plan["idempotent_hit"] is False
        assert plan["result"]["best_solution"] == ["V_TIN", "V_TOUT"]
        assert plan["result"]["shared_valves"] == ["V_TIN", "V_TOUT"]
        pid = plan["id"]

        # 拓扑里带上活跃计划摘要
        topo = client.get("/api/topology").json()
        assert any(p["id"] == pid for p in topo["active_plans"])

        # 重复提交 -> 幂等
        r2 = client.post("/api/joint-plans", json={"regions": list(reversed(REGIONS_TU))})
        assert r2.status_code == 200
        plan2 = r2.json()
        assert plan2["id"] == pid
        assert plan2["idempotent_hit"] is True
        assert [e["kind"] for e in plan2["events"]] == ["created"]
        assert len(client.get("/api/joint-plans").json()) == 1

        # 执行
        ex = client.post(f"/api/joint-plans/{pid}/execute").json()
        assert ex["status"] == "executing"
        valves = {v["id"]: v["is_open"] for v in client.get("/api/topology").json()["valves"]}
        assert valves["V_TOUT"] is False
        assert valves["V_TIN"] is False
        assert valves["V_TU"] is True  # 被去重，不需要关

        # 释放 U，共享阀保持关闭
        client.post(f"/api/joint-plans/{pid}/regions/U/release")
        valves = {v["id"]: v["is_open"] for v in client.get("/api/topology").json()["valves"]}
        assert valves["V_TOUT"] is False
        assert valves["V_TIN"] is False

        # 释放 T，阀门恢复
        fin = client.post(f"/api/joint-plans/{pid}/regions/T/release").json()
        assert fin["status"] == "released"
        valves = {v["id"]: v["is_open"] for v in client.get("/api/topology").json()["valves"]}
        assert valves["V_TOUT"] is True
        assert valves["V_TIN"] is True
        # 历史快照仍可查看
        got = client.get(f"/api/joint-plans/{pid}").json()
        assert got["result"]["shared_valves"] == ["V_TIN", "V_TOUT"]
        kinds = [e["kind"] for e in got["events"]]
        assert kinds == ["created", "executed", "region_released", "region_released", "restored"]
        # 释放后拓扑不再列活跃计划
        assert client.get("/api/topology").json()["active_plans"] == []


def test_joint_plan_locked_valve_infeasible_end_to_end():
    _reset_seed()
    with TestClient(app) as client:
        client.post("/api/valves/V_TOUT/lock", json={"locked": True})
        r = client.post("/api/joint-plans", json={"regions": REGIONS_TU})
        assert r.status_code == 200  # 不可行也登记（保留见证），但不执行
        plan = r.json()
        assert plan["feasible"] is False
        assert {p["target_id"] for p in plan["result"]["residual_paths"]} == {"T", "U"}
        for w in plan["result"]["locked_witness_paths"]:
            assert "V_TOUT" in w["locked_valves_on_path"]

        ex = client.post(f"/api/joint-plans/{plan['id']}/execute")
        assert ex.status_code == 409
        # 无阀门被部分关闭
        valves = client.get("/api/topology").json()["valves"]
        assert all(v["is_open"] for v in valves)


def test_joint_plan_contradiction_rejected():
    _reset_seed()
    with TestClient(app) as client:
        r = client.post(
            "/api/joint-plans",
            json={
                "regions": [
                    {"target_id": "T", "supply_node_ids": ["U"]},
                    {"target_id": "U", "supply_node_ids": None},
                ]
            },
        )
        assert r.status_code == 200
        plan = r.json()
        assert plan["feasible"] is False
        assert plan["result"]["contradictions"][0]["node"] == "U"
        assert client.post(f"/api/joint-plans/{plan['id']}/execute").status_code == 409


def test_joint_plan_unknown_node_and_duplicate_target_validation():
    _reset_seed()
    with TestClient(app) as client:
        bad_target = client.post(
            "/api/joint-plans", json={"regions": [{"target_id": "NOPE"}]}
        )
        assert bad_target.status_code == 400
        dup = client.post(
            "/api/joint-plans",
            json={"regions": [{"target_id": "T"}, {"target_id": "T"}]},
        )
        assert dup.status_code == 400


def test_add_region_endpoint_and_404():
    _reset_seed()
    with TestClient(app) as client:
        p = client.post(
            "/api/joint-plans", json={"regions": [{"target_id": "T"}]}
        ).json()
        # 幂等追加已有区域
        again = client.post(f"/api/joint-plans/{p['id']}/regions", json={"target_id": "T"})
        assert again.json()["idempotent_hit"] is True
        assert len(again.json()["events"]) == 1
        # 追加新区域 -> 联合重算
        added = client.post(
            f"/api/joint-plans/{p['id']}/regions", json={"target_id": "U"}
        ).json()
        assert len(added["regions"]) == 2
        assert added["result"]["shared_valves"] == ["V_TIN", "V_TOUT"]
        assert [e["kind"] for e in added["events"]].count("region_added") == 1
        # 不存在的计划
        assert client.post("/api/joint-plans/NOPE/regions", json={"target_id": "U"}).status_code == 404
        assert client.get("/api/joint-plans/NOPE").status_code == 404
