"""FastAPI 入口：拓扑查询、阀门锁定、隔离方案计算。"""
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
from .models import Valve
from .schemas import (
    IsolationIn,
    IsolationOut,
    JointPlanIn,
    JointRegionAddIn,
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
    briefs = plans.active_plan_briefs(db)
    return TopologyOut(**isolation.topology_payload(db, briefs))


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


@app.post("/api/joint-plans")
def create_joint_plan(body: JointPlanIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """登记多个目标区域并联合求解；重复登记幂等返回同一计划。"""
    seed_database(db)
    regions_in = [r.model_dump() for r in body.regions]
    try:
        plan, hit = plans.create_plan(db, regions_in)
    except (isolation.TopologyError, plans.PlanError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return plans._serialize(db, plan, hit)


@app.get("/api/joint-plans")
def list_joint_plans(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [plans._serialize(db, p) for p in plans.list_plans(db)]


@app.get("/api/joint-plans/{plan_id}")
def get_joint_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        plan = plans.get_plan(db, plan_id)
    except plans.PlanError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return plans._serialize(db, plan)


@app.post("/api/joint-plans/{plan_id}/regions")
def add_joint_region(
    plan_id: str, body: JointRegionAddIn, db: Session = Depends(get_db)
) -> dict[str, object]:
    """向准备中的计划追加一个目标区域并重新联合求解；重复目标幂等。"""
    try:
        plan, hit = plans.add_region(db, plan_id, body.model_dump())
    except plans.PlanError as exc:
        # 计划不存在 -> 404；其余状态/校验问题 -> 400
        if db.get(plans.JointPlan, plan_id) is None:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return plans._serialize(db, plan, hit)


@app.post("/api/joint-plans/{plan_id}/execute")
def execute_joint_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str,object]:
    try:
        plan = plans.execute_plan(db, plan_id)
    except plans.PlanError as exc:
        status_code = 404 if "不存在" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return plans._serialize(db, plan)


@app.post("/api/joint-plans/{plan_id}/regions/{target_id}/release")
def release_joint_region(
    plan_id: str, target_id: str, db: Session = Depends(get_db)
) -> dict[str, object]:
    """释放一个区域；共享阀在其他区域仍依赖时保持关闭，最后一个区域释放才恢复。"""
    try:
        plan = plans.release_region(db, plan_id, target_id)
    except plans.PlanError as exc:
        status_code = 404 if "不存在" in str(exc) or "没有目标区域" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return plans._serialize(db, plan)


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
