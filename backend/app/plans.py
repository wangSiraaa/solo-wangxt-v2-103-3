"""联合隔离计划的生命周期服务：登记/联合求解/执行/逐区域释放/恢复。

关键规则：

- 登记即在同一物理连通图上做一次联合求解（isolation.compute_joint_isolation）。
  可行与不可行的计划都持久化并保留请求/结果快照；不可行计划不得执行，
  也不会退化为分别应用各区域的单目标方案。
- 重复提交幂等：同一组（目标, 各自保供点）登记只产生一个计划、一条审计；
  再次提交返回原计划并标记 idempotent_hit，不新增阀门、不新增审计事件。
- 执行：按去重后的联合集合一次性关阀；锁定阀/已被其它在执计划持有的阀
  导致无法执行时整体失败，阀门状态不做部分变更。
- 释放：逐区域释放；某只阀仍被其它未释放区域见证（依赖）时保持关闭，
  最后一个区域释放后，计划持有的阀门才按计划恢复打开。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation
from .models import JointPlan, PlanEvent, PlanRegion, Valve

PLAN_PREFIX = "JP"


class PlanError(RuntimeError):
    """计划生命周期错误（API 层映射为 4xx）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _fingerprint(regions: list[dict[str, Any]]) -> str:
    norm = sorted(
        (r["target_id"], tuple(sorted(r.get("supply_node_ids") or []))) for r in regions
    )
    raw = repr(norm)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _add_event(db: Session, plan: JointPlan, kind: str, detail: dict[str, Any]) -> None:
    db.add(PlanEvent(plan_id=plan.id, kind=kind, detail=detail))


def _store_regions(db: Session, plan: JointPlan, regions: list[dict[str, Any]]) -> None:
    """把求解结论中的逐区域归属写入 PlanRegion.required_valves。"""
    by_target = {e["target_id"]: e for e in plan.result_snapshot.get("region_evidence", [])}
    for idx, r in enumerate(regions):
        evidence = by_target.get(r["target_id"], {})
        db.add(
            PlanRegion(
                plan_id=plan.id,
                idx=idx,
                target_id=r["target_id"],
                supply_node_ids=r["supply_node_ids"],
                status="prepared",
                required_valves=evidence.get("boundary_valves", []),
            )
        )


def _active_plans_q(db: Session, exclude_id: str | None = None):
    q = select(JointPlan).where(JointPlan.status != "released")
    if exclude_id:
        q = q.where(JointPlan.id != exclude_id)
    return q


def _held_valves(db: Session, exclude_plan_id: str | None = None) -> set[str]:
    """其它在执（executing）计划当前实际保持关闭的阀门集合。"""
    held: set[str] = set()
    plans = db.scalars(_active_plans_q(db, exclude_plan_id)).all()
    for p in plans:
        if p.status == "executing":
            held.update(p.result_snapshot.get("best_solution", []))
    return held


def _serialize(db: Session, plan: JointPlan, idempotent_hit: bool = False) -> dict[str, Any]:
    regions = []
    for r in plan.regions:
        regions.append(
            {
                "idx": r.idx,
                "target_id": r.target_id,
                "supply_node_ids": list(r.supply_node_ids or []),
                "status": r.status,
                "required_valves": list(r.required_valves or []),
                "released_at": _iso(r.released_at),
            }
        )
    events = [
        {"id": e.id, "at": _iso(e.at), "kind": e.kind, "detail": e.detail}
        for e in plan.events
    ]
    return {
        "id": plan.id,
        "status": plan.status,
        "feasible": plan.feasible,
        "idempotent_hit": idempotent_hit,
        "created_at": _iso(plan.created_at),
        "executed_at": _iso(plan.executed_at),
        "released_at": _iso(plan.released_at),
        "regions": regions,
        "result": plan.result_snapshot,
        "infeasible_reason": plan.infeasible_reason,
        "request_snapshot": plan.request_snapshot,
        "events": events,
    }


def get_plan(db: Session, plan_id: str) -> JointPlan:
    plan = db.get(JointPlan, plan_id)
    if plan is None:
        raise PlanError(f"联合计划不存在: {plan_id}")
    return plan


def create_plan(db: Session, regions_in: list[dict[str, Any]]) -> tuple[JointPlan, bool]:
    """登记并联合求解。返回 (plan, idempotent_hit)。

    regions_in: [{"target_id": str, "supply_node_ids": [str...] | None}, ...]
    """
    if not regions_in:
        raise PlanError("至少需要一个目标区域")

    # 先做一次计算以完成节点校验 / 默认保供点归一化 / 重复目标检查（可能抛 TopologyError）
    probe = isolation.compute_joint_isolation(db, regions_in)
    norm_regions = probe["regions"]
    fingerprint = _fingerprint(norm_regions)
    targets = [r["target_id"] for r in norm_regions]

    # 幂等：完全相同的区域+保供登记已存在（无论计划处于何种状态，含 released 历史）。
    # 演示库计划数量很少，指纹比对放在 Python 侧，避免 JSON 路径的方言差异。
    for candidate in db.scalars(select(JointPlan)).all():
        if candidate.request_snapshot.get("fingerprint") == fingerprint:
            return candidate, True

    # 冲突：任一目标仍被另一个未释放计划占用（避免对同一设备重复隔离/互相复位）
    active = db.scalars(_active_plans_q(db)).all()
    for p in active:
        p_targets = {r.target_id for r in p.regions}
        clash = p_targets.intersection(targets)
        if clash:
            raise PlanError(
                f"目标 {', '.join(sorted(clash))} 已属于在执行/准备中的计划 {p.id}，"
                "请在该计划内追加区域或先完成释放。"
            )

    plan_id = f"{PLAN_PREFIX}_{fingerprint[:10]}"
    plan = JointPlan(
        id=plan_id,
        status="prepared",
        feasible=probe["feasible"],
        result_snapshot=probe,
        request_snapshot={"fingerprint": fingerprint, "regions": norm_regions},
        infeasible_reason=probe.get("infeasible_reason"),
    )
    db.add(plan)
    db.flush()
    _store_regions(db, plan, norm_regions)
    _add_event(
        db,
        plan,
        "created",
        {
            "regions": norm_regions,
            "feasible": probe["feasible"],
            "close_valves": probe.get("best_solution", []),
        },
    )
    db.commit()
    db.refresh(plan)
    return plan, False


def add_region(
    db: Session, plan_id: str, region_in: dict[str, Any]
) -> tuple[JointPlan, bool]:
    """向 prepared 计划追加一个区域并重新联合求解；重复目标幂等返回。"""
    plan = get_plan(db, plan_id)
    if plan.status == "released":
        raise PlanError(f"计划 {plan_id} 已释放并转为历史快照，不可追加区域。")
    if plan.status == "executing":
        raise PlanError(f"计划 {plan_id} 已执行关阀，不可追加区域（先释放全部区域）。")

    current = [
        {"target_id": r.target_id, "supply_node_ids": list(r.supply_node_ids or [])}
        for r in plan.regions
    ]
    target = region_in["target_id"]

    existing = next((r for r in current if r["target_id"] == target), None)
    if existing is not None:
        new_supplies = sorted(region_in.get("supply_node_ids") or [])
        if new_supplies and sorted(existing["supply_node_ids"]) != new_supplies:
            raise PlanError(
                f"区域 {target} 已存在且保供要求不同；重复登记不得改变计划快照。"
            )
        return plan, True  # 幂等命中：不重算、不新增阀门、不新增审计

    merged = current + [
        {"target_id": target, "supply_node_ids": region_in.get("supply_node_ids") or []}
    ]
    probe = isolation.compute_joint_isolation(db, merged)  # 校验/归一化
    norm_regions = probe["regions"]
    new_fingerprint = _fingerprint(norm_regions)

    # 另一活跃计划恰好已持有同一组登记：幂等归并到已有计划由调用方处理；此处直接拒绝避免歧义
    for candidate in db.scalars(_active_plans_q(db, plan.id)).all():
        if candidate.request_snapshot.get("fingerprint") == new_fingerprint:
            raise PlanError(f"追加后的登记与另一在执行计划 {candidate.id} 完全相同。")

    plan.result_snapshot = probe
    plan.request_snapshot = {"fingerprint": new_fingerprint, "regions": norm_regions}
    plan.feasible = probe["feasible"]
    plan.infeasible_reason = probe.get("infeasible_reason")
    # 重建区域归属
    for r in list(plan.regions):
        db.delete(r)
    db.flush()
    _store_regions(db, plan, norm_regions)
    _add_event(db, plan, "region_added", {"regions": norm_regions, "added": target})
    db.commit()
    db.refresh(plan)
    return plan, False


def execute_plan(db: Session, plan_id: str) -> JointPlan:
    """按联合方案关阀。整体校验、整体应用；任何一只阀不可关则不做部分关阀。"""
    plan = get_plan(db, plan_id)
    if plan.status == "released":
        raise PlanError(f"计划 {plan_id} 已释放，不能再次执行。")
    if plan.status == "executing":
        return plan  # 幂等：重复执行请求不产生第二次关阀/审计
    if not plan.feasible:
        raise PlanError(
            f"计划 {plan_id} 联合求解无可行解，禁止执行（请查看残余/锁阀/矛盾见证）。"
        )

    close_valves = list(plan.result_snapshot.get("best_solution", []))
    # 预检：锁定阀不应出现在方案中（求解已排除），并防止与其它在执计划对共享物理阀冲突
    blocked: list[str] = []
    for vid in close_valves:
        valve = db.get(Valve, vid)
        if valve is None or valve.locked:
            blocked.append(vid)
    held_elsewhere = _held_valves(db, exclude_plan_id=plan.id).intersection(close_valves)
    if blocked or held_elsewhere:
        raise PlanError(
            "联合方案无法整体执行："
            + (f"锁定/缺失阀 {', '.join(sorted(blocked))}；" if blocked else "")
            + (f"仍被其它在执计划持有 {', '.join(sorted(held_elsewhere))}" if held_elsewhere else "")
        )

    for vid in close_valves:
        valve = db.get(Valve, vid)
        if valve is not None:
            valve.is_open = False
    plan.status = "executing"
    plan.executed_at = _utcnow()
    for r in plan.regions:
        r.status = "prepared"  # 关阀完成，等待各区域分别报告释放
    _add_event(db, plan, "executed", {"close_valves": close_valves})
    db.commit()
    db.refresh(plan)
    return plan


def release_region(db: Session, plan_id: str, target_id: str) -> JointPlan:
    """释放一个区域；共享阀在其它区域仍依赖时保持关闭。

    最后一个区域释放后：计划持有的、且不被任何其它在执计划持有的阀门恢复打开，
    计划转为 released（历史快照仍可查看）。
    """
    plan = get_plan(db, plan_id)
    if plan.status == "released":
        # 幂等：已释放计划上对同一区域的重复释放直接返回，不产生重复审计
        return plan

    region = next((r for r in plan.regions if r.target_id == target_id), None)
    if region is None:
        raise PlanError(f"计划 {plan_id} 中没有目标区域 {target_id}")

    if plan.status != "executing":
        raise PlanError(f"计划 {plan_id} 尚未执行，不能释放区域 {target_id}")

    if region.status == "released":
        return plan  # 幂等：同一区域重复释放

    region.status = "released"
    region.released_at = _utcnow()
    _add_event(db, plan, "region_released", {"target_id": target_id})

    remaining = [r for r in plan.regions if r.status != "released"]
    if not remaining:
        # 全部区域完成：按计划恢复阀门，但仍被其它在执计划依赖的阀保持关闭
        plan_valves = set(plan.result_snapshot.get("best_solution", []))
        still_held = _held_valves(db, exclude_plan_id=plan.id)
        reopened = sorted(plan_valves - still_held)
        kept_closed = sorted(plan_valves & still_held)
        for vid in reopened:
            valve = db.get(Valve, vid)
            if valve is not None:
                valve.is_open = True
        plan.status = "released"
        plan.released_at = _utcnow()
        _add_event(
            db,
            plan,
            "restored",
            {"reopened_valves": reopened, "kept_closed_for_other_plans": kept_closed},
        )
    db.commit()
    db.refresh(plan)
    return plan


def list_plans(db: Session) -> list[JointPlan]:
    return list(db.scalars(select(JointPlan).order_by(JointPlan.created_at.desc())).all())


def active_plan_briefs(db: Session) -> list[dict[str, Any]]:
    """拓扑界面用：未释放计划的目标区域、关阀集合、共享阀、各区域状态。"""
    briefs = []
    for p in db.scalars(_active_plans_q(db)).all():
        briefs.append(
            {
                "id": p.id,
                "status": p.status,
                "feasible": p.feasible,
                "close_valves": list(p.result_snapshot.get("best_solution", [])),
                "shared_valves": list(p.result_snapshot.get("shared_valves", [])),
                "regions": [
                    {"target_id": r.target_id, "status": r.status, "required_valves": list(r.required_valves or [])}
                    for r in p.regions
                ],
            }
        )
    return briefs
