"""数据库模型：节点（设备/供给点）、管段、阀门、联合隔离计划。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


class JointPlan(Base):
    """联合隔离计划：在同一张物理连通图上同时隔离多个目标区域。

    状态机：
      prepared —— 已登记并完成联合求解；阀门尚未动作，可重复提交（幂等）/追加区域
      executing —— 已按联合方案关阀；各区域依次释放，全部释放后自动转为 released
      released —— 最后一个区域释放后按计划恢复阀门；计划转为只读历史快照

    feasible 为求解结论（非状态）。不可行计划同样持久化（保留见证与请求快照），
    但不得执行；不存在“部分应用”另一区域单目标方案的路径。
    """

    __tablename__ = "joint_plans"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 联合求解结论（去重后的关阀集合、每区域证据、保供路径、不可行见证），作为计划快照保留
    result_snapshot: Mapped[dict] = mapped_column("result_json", JSON, nullable=False, default=dict)
    # 登记请求快照（区域、各自必要供给点、当时锁定），重复提交时按此幂等判定
    request_snapshot: Mapped[dict] = mapped_column("request_json", JSON, nullable=False, default=dict)
    infeasible_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    regions: Mapped[list["PlanRegion"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanRegion.idx"
    )
    events: Mapped[list["PlanEvent"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanEvent.id"
    )


class PlanRegion(Base):
    """联合计划内的一个目标区域及其仍要求保供的节点集合。

    status: prepared（未开始/已随计划关阀但本区域尚未完成）→ released（本区域检修完成）
    required_valves: 求解后联合集合中归属本区域隔离边界的阀门（见证去重后）；
    一个阀门被多个区域见证即为共享阀门，释放时须等所有依赖区域都完成。
    """

    __tablename__ = "plan_regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("joint_plans.id"), nullable=False)
    idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    supply_node_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    required_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    plan: Mapped["JointPlan"] = relationship(back_populates="regions")

    __table_args__ = (UniqueConstraint("plan_id", "target_id", name="uq_plan_target"),)


class PlanEvent(Base):
    """计划审计事件：创建/重复提交命中/追加/执行/释放/恢复，均保留时间线。"""

    __tablename__ = "plan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("joint_plans.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    plan: Mapped["JointPlan"] = relationship(back_populates="events")
