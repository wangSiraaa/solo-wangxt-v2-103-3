"""联合隔离计划验收测试（NetworkX 约束层）。

覆盖：
1. 两个可兼容区域（T、U）生成去重后的共享关阀方案，全部保供点仍可达；
   联合集合小于两区域单目标方案的并集（V_TU 被去重）。
2a. 锁定关键阀（V_TOUT）时整个联合计划无解，逐区域给出残余/锁阀见证，
    不应用任何区域的单目标方案。
2b. 彼此矛盾的保供要求（一个区域要隔离的节点恰是另一区域的保供点）无解，
    返回矛盾见证；单区域强制保供 P3 时给出不可避免断供见证。
3. 重复提交同一目标集合不产生第二套阀门、不重复审计。
4. 先释放一个区域后共享阀保持关闭；最后一个区域释放并重新读取后阀门才
   按计划恢复，历史快照仍可查看。
"""
from __future__ import annotations

import pytest

from app import plans
from app.database import Base, SessionLocal, engine
from app.isolation import build_graph
from app.models import Valve
from app.seed import reset_database, seed_database


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    yield db
    db.close()


def _edges(db):
    from app.isolation import _load

    return _load(db)[1]


def _graph(db, closed):
    return build_graph(_edges(db), set(closed))


def _set_locks(db, **locks: bool) -> None:
    for vid, locked in locks.items():
        valve = db.get(Valve, vid)
        assert valve is not None, vid
        valve.locked = locked
    db.commit()


# ---------- 验收 1：兼容区域、去重共享、保供可达 ----------

def test_joint_compatible_regions_dedup_and_supply(fresh_db):
    import networkx as nx

    from app.isolation import compute_isolation, compute_joint_isolation

    single_t = set(compute_isolation(fresh_db, "T")["best_solution"])
    single_u = set(compute_isolation(fresh_db, "U")["best_solution"])
    assert single_t == {"V_TIN", "V_TOUT"}
    assert single_u == {"V_TU"}

    result = compute_joint_isolation(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    assert result["feasible"] is True
    # 去重：联合最小集合 {V_TIN, V_TOUT} ⊊ 两个单目标方案的并集（V_TU 被剔除）
    assert result["best_solution"] == ["V_TIN", "V_TOUT"]
    assert set(result["best_solution"]) < (single_t | single_u)
    # 两只阀都被两个区域共同见证 => 共享阀门
    assert set(result["shared_valves"]) == {"V_TIN", "V_TOUT"}
    valve_regions = {v["valve_id"]: v["regions"] for v in result["valve_regions"]}
    assert valve_regions["V_TIN"] == ["T", "U"]
    assert valve_regions["V_TOUT"] == ["T", "U"]
    assert all(v["shared"] for v in result["valve_regions"])

    # NetworkX 联合约束：两个目标同时断源，全部（默认）必要供给点仍可达
    g = _graph(fresh_db, result["best_solution"])
    assert not nx.has_path(g, "SRC", "T")
    assert not nx.has_path(g, "SRC", "U")
    assert nx.has_path(g, "SRC", "P1")
    assert nx.has_path(g, "SRC", "P2")
    # 每区域隔离证据 + 每供给点保供路径齐全
    by_target = {e["target_id"]: e for e in result["region_evidence"]}
    for t in ("T", "U"):
        ev = by_target[t]
        assert ev["target_isolated"] is True
        assert set(ev["boundary_valves"]) == {"V_TIN", "V_TOUT"}
        assert ev["supply_paths"]["P1"] is not None
        assert "BP" in ev["supply_paths"]["P2"]


def test_joint_repeated_target_rejected(fresh_db):
    from app.isolation import compute_joint_isolation
    from app.isolation import TopologyError

    with pytest.raises(TopologyError):
        compute_joint_isolation(
            fresh_db,
            [
                {"target_id": "T", "supply_node_ids": None},
                {"target_id": "T", "supply_node_ids": None},
            ],
        )


# ---------- 验收 2a：锁定关键阀 => 整体无解，逐区域见证，不部分应用 ----------

def test_joint_locked_critical_valve_is_infeasible_with_witnesses(fresh_db):
    from app.isolation import compute_joint_isolation

    _set_locks(fresh_db, V_TOUT=True)
    result = compute_joint_isolation(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    assert result["feasible"] is False
    assert result["best_solution"] == []

    # 每个区域各自有残余路径与经锁定阀的见证路径
    residual_targets = {p["target_id"] for p in result["residual_paths"]}
    witness_targets = {p["target_id"] for p in result["locked_witness_paths"]}
    assert residual_targets == {"T", "U"}
    assert witness_targets == {"T", "U"}
    for p in result["locked_witness_paths"]:
        assert p["nodes"][0] == "SRC"
        assert p["nodes"][-1] == p["target_id"]
        assert "V_TOUT" in p["valves"]
        assert "V_TOUT" in p["locked_valves_on_path"]
    # 不可避免断供的逐区域见证：两个区域都保不住 P2
    ub = result["unconstrained_best"]
    assert ub is not None
    for row in ub["per_region"]:
        assert row["unavoidable_supplies"] == ["P2"]
    # 锁阀绝不出现在任何切集中
    assert "V_TOUT" not in ub["close_valves"]
    assert "不应用" in result["infeasible_reason"] or "未对任何区域" in result["infeasible_reason"]


def test_locked_plan_is_persisted_but_not_executed(fresh_db):
    """不可行计划持久化保留见证，但执行必须整体失败、阀门不被部分关闭。"""
    _set_locks(fresh_db, V_TOUT=True)
    plan, _ = plans.create_plan(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    assert plan.feasible is False
    assert plan.result_snapshot["residual_paths"]
    with pytest.raises(plans.PlanError):
        plans.execute_plan(fresh_db, plan.id)
    # 没有任何阀门被关闭（未部分应用任一区域方案）
    assert all(v.is_open for v in fresh_db.query(Valve).all())


# ---------- 验收 2b：矛盾保供需求 ----------

def test_joint_contradictory_supply_requirements(fresh_db):
    from app.isolation import compute_joint_isolation

    # 区域 T 要求对 U 保供，区域 U 又要隔离 U —— 结构性矛盾
    result = compute_joint_isolation(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": ["U"]},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    assert result["feasible"] is False
    assert len(result["contradictions"]) == 1
    c = result["contradictions"][0]
    assert c["node"] == "U"
    assert c["target_region"] == "U"
    assert c["supply_region"] == "T"
    assert result["best_solution"] == []
    assert "矛盾" in result["infeasible_reason"]


def test_joint_unavoidable_supply_loss_witness(fresh_db):
    """区域 U 强制保供 P3：P3 是 U 下游枝路，任何切法都保不住。"""
    from app.isolation import compute_joint_isolation

    result = compute_joint_isolation(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": ["P3"]},
        ],
    )
    assert result["feasible"] is False
    ub = result["unconstrained_best"]
    per = {r["target_id"]: r for r in ub["per_region"]}
    assert per["U"]["unavoidable_supplies"] == ["P3"]
    assert per["T"]["unavoidable_supplies"] == []


# ---------- 验收 3：重复提交幂等 ----------

def test_duplicate_submission_is_idempotent(fresh_db):
    plan1, hit1 = plans.create_plan(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    assert hit1 is False
    # 调整顺序/显式给出默认保供点，仍是同一登记指纹
    plan2, hit2 = plans.create_plan(
        fresh_db,
        [
            {"target_id": "U", "supply_node_ids": ["P1", "P2"]},
            {"target_id": "T", "supply_node_ids": ["P2", "P1"]},
        ],
    )
    assert hit2 is True
    assert plan2.id == plan1.id
    # 只有一条 created 审计，没有第二套阀门
    kinds = [e.kind for e in plan2.events]
    assert kinds == ["created"]
    assert plan2.result_snapshot["best_solution"] == ["V_TIN", "V_TOUT"]
    # 数据库里只有一个计划
    assert len(plans.list_plans(fresh_db)) == 1


def test_same_target_in_another_active_plan_conflicts(fresh_db):
    plans.create_plan(fresh_db, [{"target_id": "T", "supply_node_ids": None}])
    # 不同登记但目标 T 重叠，且计划 1 仍在 prepared/executing => 冲突拒绝
    with pytest.raises(plans.PlanError):
        plans.create_plan(
            fresh_db,
            [
                {"target_id": "T", "supply_node_ids": None},
                {"target_id": "U", "supply_node_ids": None},
            ],
        )
    # 计划 1 释放（成为历史）后，同一目标可在新计划登记
    plans.execute_plan(fresh_db, plans.list_plans(fresh_db)[0].id)
    plans.release_region(fresh_db, plans.list_plans(fresh_db)[0].id, "T")
    again, hit = plans.create_plan(fresh_db, [{"target_id": "T", "supply_node_ids": None}])
    assert hit is True  # 与历史快照登记相同 => 幂等命中历史计划


def test_add_duplicate_region_is_idempotent(fresh_db):
    plan, _ = plans.create_plan(fresh_db, [{"target_id": "T", "supply_node_ids": None}])
    before = len(plan.events)
    _, hit = plans.add_region(fresh_db, plan.id, {"target_id": "T", "supply_node_ids": None})
    assert hit is True
    fresh_db.refresh(plan)
    assert len(plan.events) == before
    assert len(plan.regions) == 1


# ---------- 验收 4：释放顺序与共享阀、快照 ----------

def test_shared_valve_stays_closed_until_last_region_released(fresh_db):
    plan, _ = plans.create_plan(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    plans.execute_plan(fresh_db, plan.id)
    assert fresh_db.get(Valve, "V_TOUT").is_open is False
    assert fresh_db.get(Valve, "V_TIN").is_open is False

    # 先释放 U：共享阀仍被 T 依赖，保持关闭
    plans.release_region(fresh_db, plan.id, "U")
    mid = plans.get_plan(fresh_db, plan.id)
    assert mid.status == "executing"
    assert fresh_db.get(Valve, "V_TOUT").is_open is False
    assert fresh_db.get(Valve, "V_TIN").is_open is False
    u_region = next(r for r in mid.regions if r.target_id == "U")
    t_region = next(r for r in mid.regions if r.target_id == "T")
    assert u_region.status == "released"
    assert t_region.status == "prepared"

    # 重复释放 U 幂等：不新增审计，阀不动
    events_before = len(mid.events)
    plans.release_region(fresh_db, plan.id, "U")
    assert len(plans.get_plan(fresh_db, plan.id).events) == events_before
    assert fresh_db.get(Valve, "V_TOUT").is_open is False

    # 最后一个区域 T 释放：按计划恢复
    plans.release_region(fresh_db, plan.id, "T")
    final = plans.get_plan(fresh_db, plan.id)
    assert final.status == "released"
    assert fresh_db.get(Valve, "V_TOUT").is_open is True
    assert fresh_db.get(Valve, "V_TIN").is_open is True
    # 重新读取后的历史快照仍完整
    assert final.result_snapshot["shared_valves"] == ["V_TIN", "V_TOUT"]
    kinds = [e.kind for e in final.events]
    assert kinds == ["created", "executed", "region_released", "region_released", "restored"]
    restore = next(e for e in final.events if e.kind == "restored")
    assert set(restore.detail["reopened_valves"]) == {"V_TIN", "V_TOUT"}
    assert all(r.status == "released" for r in final.regions)


def test_execute_and_release_are_idempotent(fresh_db):
    plan, _ = plans.create_plan(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    plans.execute_plan(fresh_db, plan.id)
    plans.execute_plan(fresh_db, plan.id)  # 重复执行
    p = plans.get_plan(fresh_db, plan.id)
    assert [e.kind for e in p.events].count("executed") == 1
    plans.release_region(fresh_db, plan.id, "T")
    plans.release_region(fresh_db, plan.id, "T")  # 重复释放
    p = plans.get_plan(fresh_db, plan.id)
    assert [e.kind for e in p.events].count("region_released") == 1
    # 只剩 U，计划仍在执行
    assert p.status == "executing"


def test_release_before_execute_rejected(fresh_db):
    plan, _ = plans.create_plan(fresh_db, [{"target_id": "T", "supply_node_ids": None}])
    with pytest.raises(plans.PlanError):
        plans.release_region(fresh_db, plan.id, "T")


def test_reset_clears_plans_and_opens_valves(fresh_db):
    plan, _ = plans.create_plan(
        fresh_db,
        [
            {"target_id": "T", "supply_node_ids": None},
            {"target_id": "U", "supply_node_ids": None},
        ],
    )
    plans.execute_plan(fresh_db, plan.id)
    reset_database(fresh_db)
    assert plans.list_plans(fresh_db) == []
    assert all(v.is_open and not v.locked for v in fresh_db.query(Valve).all())
