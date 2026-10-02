"""联合隔离计划的生命周期管理：创建（联合求解）、执行（应用关阀）、
按区域释放（共享阀门最后恢复），全程幂等并保留审计快照。

状态机：
  计划  prepared --execute--> executing --全部区域释放--> released
        （求解无解时为 infeasible，不可执行）
  区域  prepared --计划执行--> executing --释放--> released

阀门应用/恢复规则：
  - 执行时把联合关阀集合整体应用到阀门表（is_open=False），并快照事前状态；
  - 释放某区域时，仅恢复“不再被任何未释放区域依赖”的计划阀门——共享阀门
    （被 ≥2 个区域依赖）在最后一个依赖它的区域释放前保持关闭；
  - 重复创建（同一幂等键）、重复执行、重复释放均为幂等操作：
    返回当前计划状态，不新增阀门、不产生重复审计事件。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation
from .models import IsolationPlan, PlanEvent, PlanZone, Valve


class PlanError(RuntimeError):
    """计划生命周期中的可预期错误（映射为 HTTP 409/400）。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _add_event(db: Session, plan: IsolationPlan, action: str, detail: dict[str, Any]) -> None:
    seq = len(plan.events) + 1
    db.add(
        PlanEvent(
            plan_id=plan.id,
            seq=seq,
            action=action,
            detail=detail,
            created_at=_now(),
        )
    )


def _plan_snapshot(plan: IsolationPlan) -> dict[str, Any]:
    """写入审计事件的计划状态快照。"""
    return {
        "plan_id": plan.id,
        "status": plan.status,
        "close_valves": list(plan.close_valves),
        "shared_valves": list(plan.shared_valves),
        "zones": [
            {
                "id": z.id,
                "target_id": z.target_id,
                "essentials": list(z.essentials),
                "status": z.status,
                "boundary_valves": list(z.boundary_valves),
            }
            for z in plan.zones
        ],
    }


def plan_view(plan: IsolationPlan, with_events: bool = True) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": plan.id,
        "name": plan.name,
        "status": plan.status,
        "feasible": plan.feasible,
        "close_valves": list(plan.close_valves),
        "shared_valves": list(plan.shared_valves),
        "zones": [
            {
                "id": z.id,
                "target_id": z.target_id,
                "essentials": list(z.essentials),
                "status": z.status,
                "boundary_valves": list(z.boundary_valves),
            }
            for z in plan.zones
        ],
        "solve": dict(plan.solve_snapshot),
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }
    if with_events:
        view["events"] = [
            {
                "seq": e.seq,
                "action": e.action,
                "detail": dict(e.detail),
                "created_at": e.created_at,
            }
            for e in plan.events
        ]
    return view


def get_plan(db: Session, plan_id: str) -> IsolationPlan:
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise PlanError(f"计划不存在: {plan_id}", status_code=404)
    return plan


def create_plan(
    db: Session, name: str, request_key: str | None, zones: list[dict[str, Any]]
) -> tuple[IsolationPlan, bool]:
    """创建联合隔离计划并求解。返回 (计划, 是否新建)。

    幂等：request_key 已存在时直接返回既有计划，不重复求解、不重复审计。
    """
    if request_key:
        existing = db.scalar(
            select(IsolationPlan).where(IsolationPlan.request_key == request_key)
        )
        if existing is not None:
            return existing, False

    payload = isolation.compute_joint_isolation(db, zones)

    now = _now()
    plan = IsolationPlan(
        id=f"PLAN-{uuid.uuid4().hex[:10]}",
        request_key=request_key,
        name=name or "联合隔离计划",
        status="prepared" if payload["feasible"] else "infeasible",
        feasible=payload["feasible"],
        close_valves=payload["close_valves"],
        shared_valves=payload["shared_valves"],
        solve_snapshot=payload,
        valve_snapshot={},
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    boundary = {z["target_id"]: z["boundary_valves"] for z in payload["zone_evidence"]}
    for i, z in enumerate(zones, start=1):
        db.add(
            PlanZone(
                id=f"{plan.id}-Z{i}",
                plan_id=plan.id,
                seq=i,
                target_id=z["target_id"],
                essentials=sorted(set(z["essentials"])),
                status="prepared",
                boundary_valves=boundary.get(z["target_id"], []),
            )
        )
    db.flush()
    db.refresh(plan)
    _add_event(
        db,
        plan,
        "created",
        {
            **_plan_snapshot(plan),
            "solve_feasible": payload["feasible"],
            "infeasible_reason": payload["infeasible_reason"],
        },
    )
    db.commit()
    db.refresh(plan)
    return plan, True


def execute_plan(db: Session, plan_id: str) -> IsolationPlan:
    """执行计划：把联合关阀集合整体应用到阀门表。幂等。"""
    plan = get_plan(db, plan_id)
    if plan.status == "executing":
        return plan  # 幂等：已执行
    if plan.status == "released":
        return plan  # 幂等：已完结，不再变更
    if not plan.feasible:
        raise PlanError("联合计划无解，不存在可执行的关阀集合，已仅保留见证。")
    if plan.status != "prepared":
        raise PlanError(f"计划状态不允许执行: {plan.status}")

    running = db.scalar(
        select(IsolationPlan).where(IsolationPlan.status == "executing")
    )
    if running is not None and running.id != plan.id:
        raise PlanError(f"计划 {running.id} 正在执行中，请先完成其释放。")

    # 执行前复核：计划阀门仍须处于可关闭状态（创建后未被锁定/关闭）
    for vid in plan.close_valves:
        valve = db.get(Valve, vid)
        if valve is None:
            raise PlanError(f"计划阀门不存在: {vid}")
        if valve.locked:
            raise PlanError(f"计划阀门 {vid} 已被锁定，无法按计划关闭。")
        if not valve.is_open:
            raise PlanError(f"计划阀门 {vid} 已处于关闭状态，拓扑已变化。")

    snapshot = {vid: True for vid in plan.close_valves}
    for vid in plan.close_valves:
        db.get(Valve, vid).is_open = False
    plan.valve_snapshot = snapshot
    plan.status = "executing"
    plan.updated_at = _now()
    for z in plan.zones:
        z.status = "executing"
    db.flush()
    db.refresh(plan)
    _add_event(
        db,
        plan,
        "executed",
        {**_plan_snapshot(plan), "applied_close_valves": list(plan.close_valves)},
    )
    db.commit()
    db.refresh(plan)
    return plan


def release_zone(db: Session, plan_id: str, zone_id: str) -> IsolationPlan:
    """释放一个区域：恢复不再被其他未释放区域依赖的计划阀门。

    共享阀门（仍被其他区域依赖）保持关闭；最后一个区域释放后，
    计划阀门全部按执行前快照恢复。重复释放幂等。
    """
    plan = get_plan(db, plan_id)
    zone = db.get(PlanZone, zone_id)
    if zone is None or zone.plan_id != plan.id:
        raise PlanError(f"计划 {plan.id} 中不存在区域: {zone_id}", status_code=404)
    if zone.status == "released":
        return plan  # 幂等：该区域已释放，不重复审计
    if plan.status != "executing" or zone.status != "executing":
        raise PlanError(f"区域状态不允许释放: 计划={plan.status}, 区域={zone.status}")

    zone.status = "released"
    active = [z for z in plan.zones if z.status != "released"]

    # 仍被未释放区域依赖的阀门保持关闭（含共享阀门），其余按计划快照恢复
    still_needed: set[str] = set()
    for z in active:
        still_needed.update(z.boundary_valves)
    restored = [v for v in plan.close_valves if v not in still_needed]
    for vid in restored:
        valve = db.get(Valve, vid)
        if valve is not None:
            valve.is_open = bool(plan.valve_snapshot.get(vid, True))

    plan.updated_at = _now()
    db.flush()
    db.refresh(plan)
    _add_event(
        db,
        plan,
        "zone_released",
        {
            **_plan_snapshot(plan),
            "released_zone": zone.id,
            "released_target": zone.target_id,
            "restored_valves": restored,
            "still_closed_valves": sorted(still_needed & set(plan.close_valves)),
        },
    )
    if not active:
        plan.status = "released"
        plan.updated_at = _now()
        db.flush()
        db.refresh(plan)
        _add_event(
            db,
            plan,
            "plan_released",
            {**_plan_snapshot(plan), "restored_valves": list(plan.close_valves)},
        )
    db.commit()
    db.refresh(plan)
    return plan
