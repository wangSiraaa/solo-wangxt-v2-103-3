"""FastAPI 入口：拓扑查询、阀门锁定、隔离方案计算、联合隔离计划。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation, plans
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import IsolationPlan, Valve
from .schemas import (
    IsolationIn,
    IsolationOut,
    PlanCreateIn,
    PlanOut,
    PlanSummaryOut,
    TopologyOut,
    ValveLockIn,
)
from .seed import reset_database, seed_database

@asynccontextmanager
async def lifespan(app: FastAPI):  # pragma: no cover
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="管网隔离方案演示 API（培训用）",
    description="基于固定拓扑与阀门模型的检修隔离候选集合计算，不连接真实控制系统。",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/topology", response_model=TopologyOut)
def get_topology(db: Session = Depends(get_db)) -> TopologyOut:
    seed_database(db)
    return TopologyOut(**isolation.topology_payload(db))


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)) -> dict[str, object]:
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    valve.locked = body.locked
    db.commit()
    return {"id": valve.id, "locked": valve.locked}


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, str]:
    reset_database(db)
    return {"status": "reset"}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    seed_database(db)
    if body.locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in body.locks if vid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        for vid, locked in body.locks.items():
            valve = db.get(Valve, vid)
            if valve is not None:
                valve.locked = locked
        db.commit()

    try:
        payload = isolation.compute_isolation(db, body.target_id)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IsolationOut(**payload)


# ---------------- 联合隔离计划 ----------------


@app.post("/api/plans", response_model=PlanOut)
def create_plan(body: PlanCreateIn, db: Session = Depends(get_db)) -> PlanOut:
    """创建联合隔离计划：同一物理连通图上对多个目标区域联合求解。

    幂等：携带相同 request_key 重复提交时返回既有计划，
    不新增关阀集合、不产生重复审计事件。
    """
    seed_database(db)
    zones = [{"target_id": z.target_id, "essentials": z.essentials} for z in body.zones]
    try:
        plan, _created = plans.create_plan(db, body.name, body.request_key, zones)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlanOut(**plans.plan_view(plan))


@app.get("/api/plans", response_model=list[PlanSummaryOut])
def list_plans(db: Session = Depends(get_db)) -> list[PlanSummaryOut]:
    seed_database(db)
    all_plans = db.scalars(
        select(IsolationPlan).order_by(IsolationPlan.created_at.desc())
    ).all()
    return [
        PlanSummaryOut(
            id=p.id,
            name=p.name,
            status=p.status,
            feasible=p.feasible,
            close_valves=list(p.close_valves),
            shared_valves=list(p.shared_valves),
            zone_count=len(p.zones),
            created_at=p.created_at,
        )
        for p in all_plans
    ]


@app.get("/api/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanOut:
    """计划详情：区域状态、共享阀门、求解快照与全部审计事件（历史快照）。"""
    try:
        plan = plans.get_plan(db, plan_id)
    except plans.PlanError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return PlanOut(**plans.plan_view(plan))


@app.post("/api/plans/{plan_id}/execute", response_model=PlanOut)
def execute_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanOut:
    """执行计划：联合关阀集合整体应用到阀门表（幂等）。"""
    try:
        plan = plans.execute_plan(db, plan_id)
    except plans.PlanError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return PlanOut(**plans.plan_view(plan))


@app.post("/api/plans/{plan_id}/zones/{zone_id}/release", response_model=PlanOut)
def release_zone(plan_id: str, zone_id: str, db: Session = Depends(get_db)) -> PlanOut:
    """释放一个区域：共享阀门保持关闭，专有阀门按计划恢复（幂等）。"""
    try:
        plan = plans.release_zone(db, plan_id, zone_id)
    except plans.PlanError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return PlanOut(**plans.plan_view(plan))


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
