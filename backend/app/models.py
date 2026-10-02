"""数据库模型：节点（设备/供给点）、管段、阀门、联合隔离计划。"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source/equipment/consumer/junction
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Valve(Base):
    """阀门与其所在管段一一对应（demo 模型）。

    is_open:       阀门当前实际开闭状态；锁定后用户不可再改变其操作状态。
    locked:        用户锁定标记，锁定阀门不参与候选关闭集合。
                   初始状态全部为打开、未锁定；当前演示中可操作的动作是“关闭”，
                   因此锁定等价于“禁止关闭”（保持打开）。
    operable:      工艺上是否允许操作（个别阀门检修/铅封，恒不可用）。
    """

    __tablename__ = "valves"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    segment: Mapped["Segment"] = relationship(back_populates="valve")


class Segment(Base):
    """有向管段：upstream -> downstream 表示介质名义流向。

    隔离计算按“物理连通”使用无向图（介质可被两侧隔离边界切断，
    且检修隔离关注连通性而非流向）；direction 用于前端箭头标注、
    来源/下游识别与结果解释。旁路是与主管并联的一对管段。
    """

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    downstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="main")  # main/bypass/branch
    is_bypass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    valve: Mapped["Valve | None"] = relationship(back_populates="segment", uselist=False)

    __table_args__ = (UniqueConstraint("upstream_id", "downstream_id", name="uq_segment_endpoints"),)


class IsolationPlan(Base):
    """联合隔离计划：在同一物理连通图上对多个目标区域联合求解的关阀方案。

    status: prepared（已生成/准备）-> executing（执行中，关阀已应用）
            -> released（全部区域释放，阀门按计划恢复）；
            infeasible（联合求解无解，仅存见证，不可执行）。
    request_key: 客户端幂等键——同一键重复提交返回既有计划，
                 不新增关阀集合、不产生重复审计事件。
    solve_snapshot: 创建时联合求解结果（含共享阀门、各区隔离证据、
                    保供路径或残余/锁阀见证）的完整快照，历史可查。
    valve_snapshot: 执行时计划阀门的事前状态，释放时按计划恢复。
    """

    __tablename__ = "isolation_plans"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    close_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    shared_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    solve_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    valve_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    zones: Mapped[list["PlanZone"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanZone.seq"
    )
    events: Mapped[list["PlanEvent"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanEvent.seq"
    )


class PlanZone(Base):
    """计划内的一个目标区域：目标节点 + 该区域要求保供的必要供给点。

    status: prepared（准备）-> executing（执行）-> released（已释放）。
    boundary_valves: 联合关阀集合中该区域隔离所依赖的阀门（归因结果）；
                     被 ≥2 个区域依赖的即共享阀门，释放时须最后恢复。
    """

    __tablename__ = "plan_zones"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("isolation_plans.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    essentials: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    boundary_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    plan: Mapped[IsolationPlan] = relationship(back_populates="zones")


class PlanEvent(Base):
    """计划审计事件：创建/执行/区域释放/计划释放，detail 为该时刻计划快照。"""

    __tablename__ = "plan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("isolation_plans.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)

    plan: Mapped[IsolationPlan] = relationship(back_populates="events")
