"""隔离候选集合计算（NetworkX）。

模型约定（仅适用于本演示的固定拓扑与阀门模型）：

1. 图为无向“物理连通图”——隔离边界阀门无论装在设备哪一侧，关闭后该物理
   连通即被切断。管段上保存的 up/down 方向用于前端箭头与来源/下游解释。
2. 阀门与其所在管段 1:1。阀门打开 => 该边连通；关闭 => 该边移除；
   锁定阀门不可被本算法选入关闭集合（保持其当前状态）。
3. 有效隔离集合必须同时满足：
   a. 目标设备与所有介质来源之间不存在连通路径；
   b. 每个必要供给点（essential 节点）仍与至少一个介质来源连通。
4. 在所有有效集合中取阀门数最少者；并列时按阀门 id 排序取字典序最小，
   并可返回若干等价方案。
5. 不存在有效集合时，返回当前仍可从来源到达目标设备的一条残余路径
   以及断供/无法隔离的诊断信息。

联合隔离（compute_joint_isolation）在同一物理连通图上对多个目标区域
联合求解：有效集合须同时让“所有目标与来源断开”且“所有区域登记的必要
供给点仍可达”。求解是全有或全无——锁定阀、重复目标或相互矛盾的保供
需求导致无解时不返回任何部分可用的关阀集合，只返回各区域的残余/锁阀
见证与不可避免断供诊断。联合集合中阀门的“区域归因”定义为：从集合中
移除该阀后某区域目标重新与来源连通，则该区域依赖该阀；被 ≥2 个区域
依赖的阀门即共享阀门。
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Node, Segment, Valve

MAX_ENUMERATE = 200_000
MAX_ALTERNATIVES = 5


class TopologyError(RuntimeError):
    pass


def _load(db: Session) -> tuple[list[Node], dict[str, dict[str, Any]]]:
    nodes = list(db.scalars(select(Node)).all())
    valves = {v.segment_id: v for v in db.scalars(select(Valve)).all()}
    segments = list(db.scalars(select(Segment)).all())

    edges: dict[str, dict[str, Any]] = {}
    for seg in segments:
        v = valves.get(seg.id)
        edges[seg.id] = {
            "id": seg.id,
            "u": seg.upstream_id,
            "v": seg.downstream_id,
            "direction": f"{seg.upstream_id}->{seg.downstream_id}",
            "kind": seg.kind,
            "is_bypass": seg.is_bypass,
            "valve_id": v.id if v else None,
            "valve_name": v.name if v else None,
            "is_open": v.is_open if v else True,
            "locked": v.locked if v else False,
            "operable": v.operable if v else True,
        }
    return nodes, edges


def build_graph(edges: dict[str, dict[str, Any]], closed: set[str]) -> nx.Graph:
    """构建在给定关闭集合下的物理连通无向图。

    初始即关闭、或被方案关闭的阀门对应边都不放入图；
    锁定阀门保持其当前状态、不可被选入 closed。
    """
    g = nx.Graph()
    for e in edges.values():
        g.add_node(e["u"])
        g.add_node(e["v"])
        if not e["is_open"]:
            continue  # 初始已关闭
        if e["valve_id"] in closed:
            continue  # 本方案关闭
        g.add_edge(e["u"], e["v"], edge_id=e["id"], valve_id=e["valve_id"])
    return g


def _reachable_sources(g: nx.Graph, node: str, sources: list[str]) -> list[str]:
    return [s for s in sources if s in g and node in g and nx.has_path(g, s, node)]


def _shortest_source_path(g: nx.Graph, target: str, sources: list[str]) -> list[str] | None:
    best: list[str] | None = None
    for s in sources:
        if s in g and target in g and nx.has_path(g, s, target):
            p = nx.shortest_path(g, s, target)
            if best is None or len(p) < len(best):
                best = p
    return best


def _preferred_source_path(
    g: nx.Graph, target: str, sources: list[str], prefer_edges: set[tuple[str, str]]
) -> list[str] | None:
    """返回来源到目标的供给路径；在最短长度 +2 跳内优先经过 prefer_edges（如开启的旁路）。

    两条供给路径都真实存在时，优先展示经旁路的那一条，便于培训页直观呈现
    “旁路绕回”。代价是允许比最短路多至多两跳；无旁路可用时退回最短路。
    """
    best: list[str] | None = None
    for s in sources:
        if s not in g or target not in g or not nx.has_path(g, s, target):
            continue
        min_len = nx.shortest_path_length(g, s, target)
        chosen: list[str] | None = None
        checked = 0
        for p in nx.shortest_simple_paths(g, s, target):
            if len(p) - 1 > min_len + 2 or checked > 200:
                break
            checked += 1
            if any((a, b) in prefer_edges or (b, a) in prefer_edges for a, b in zip(p, p[1:])):
                chosen = p
                break
        if chosen is None:
            chosen = nx.shortest_path(g, s, target)
        if best is None or len(chosen) < len(best):
            best = chosen
    return best


def _path_region_valves(
    edges: dict[str, dict[str, Any]],
    base_graph: nx.Graph,
    sources: list[str],
    targets: list[str],
) -> set[str]:
    """可能参与隔离的阀门集合：位于某条来源→目标简单路径上的边的阀门。

    最小有效集合中的每只阀门必然落在某条来源→目标路径上（否则移除它
    不会让任何目标更断开，集合就不是最小的），因此枚举只需考虑这些阀门；
    挂在死端支路上的阀门（如供给点支路阀）以及被关节点隔开的无关子网
    对隔离毫无帮助，被安全剪除。

    判定方法：加超级源/汇后求双连通分量（块），边在某条简单 s-t 路径上
    当且仅当它所在的块位于块-割点树上源块与汇块之间的路径上。
    """
    h = base_graph.copy()
    super_s, super_t = "\x00S", "\x00T"
    for s in sources:
        if s in h:
            h.add_edge(super_s, s)
    for t in targets:
        if t in h:
            h.add_edge(super_t, t)
    if super_s not in h or super_t not in h or not nx.has_path(h, super_s, super_t):
        return set()

    blocks = list(nx.biconnected_components(h))
    tree = nx.Graph()  # 块-割点树：块节点(int) 与 原图节点(articulation)
    for i, comp in enumerate(blocks):
        tree.add_node(("B", i))
        for n in comp:
            tree.add_edge(("B", i), n)
    s_block = next(i for i, comp in enumerate(blocks) if super_s in comp)
    t_block = next(i for i, comp in enumerate(blocks) if super_t in comp)
    on_path = set(nx.shortest_path(tree, ("B", s_block), ("B", t_block)))

    keep_pairs: set[frozenset[str]] = set()
    for i, comp in enumerate(blocks):
        if ("B", i) not in on_path:
            continue
        for a, b in h.subgraph(comp).edges():
            keep_pairs.add(frozenset((a, b)))

    useful: set[str] = set()
    for e in edges.values():
        if not e["is_open"] or not e["valve_id"]:
            continue
        if frozenset((e["u"], e["v"])) in keep_pairs:
            useful.add(e["valve_id"])
    return useful


def _path_valves(g_or_edges: nx.Graph | dict, path: list[str]) -> list[str]:
    """把节点路径翻译成经过的阀门 id 列表。"""
    if isinstance(g_or_edges, nx.Graph):
        g = g_or_edges
        out: list[str] = []
        for a, b in zip(path, path[1:]):
            data = g.get_edge_data(a, b)
            if data and data.get("valve_id"):
                out.append(data["valve_id"])
        return out
    # dict 模式：用于解析经过已移除边的残余路径
    edges = g_or_edges
    pair_to_valve = {(e["u"], e["v"]): e["valve_id"] for e in edges.values()}
    pair_to_valve.update({(e["v"], e["u"]): e["valve_id"] for e in edges.values()})
    return [pair_to_valve[(a, b)] for a, b in zip(path, path[1:]) if (a, b) in pair_to_valve]


def _valve_view(edges: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in edges.values():
        vid = e["valve_id"]
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append(
            {
                "id": vid,
                "name": e["valve_name"],
                "segment_id": e["id"],
                "endpoints": [e["u"], e["v"]],
                "is_open": e["is_open"],
                "locked": e["locked"],
                "operable": e["operable"],
                "is_bypass": e["is_bypass"],
            }
        )
    return sorted(out, key=lambda x: x["id"])


def _full_open_graph(edges: dict[str, dict[str, Any]], node_ids: set[str]) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(node_ids)
    for e in edges.values():
        if e["is_open"]:
            g.add_edge(e["u"], e["v"], edge_id=e["id"], valve_id=e["valve_id"])
    return g


def _describe_path(
    full_open: nx.Graph, edges: dict[str, dict[str, Any]], node_path: list[str]
) -> dict[str, Any]:
    """把一条节点路径描述为 节点序列 + 阀门序列 + 路径上的锁定阀。"""
    # 跨边拼接时 h1 的邻点 h2 会在两段中重复出现，压缩相邻重复
    cleaned: list[str] = []
    for n in node_path:
        if not cleaned or cleaned[-1] != n:
            cleaned.append(n)
    node_path = cleaned
    valve_seq: list[str | None] = []
    edge_seq: list[str] = []
    for a, b in zip(node_path, node_path[1:]):
        data = full_open.get_edge_data(a, b)
        edge_seq.append(data["edge_id"] if data else "")
        valve_seq.append(data["valve_id"] if data else None)
    return {
        "nodes": node_path,
        "valves": valve_seq,
        "locked_valves_on_path": sorted(
            {edges[eid]["valve_id"] for eid in edge_seq if eid and edges[eid]["locked"]}
        ),
        "uses_bypass": any(eid and edges[eid]["is_bypass"] for eid in edge_seq),
    }


def _residual_and_locked_witness(
    base_graph: nx.Graph,
    full_open: nx.Graph,
    edges: dict[str, dict[str, Any]],
    sources: list[str],
    target_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """当前（锁定）状态下仍连通来源与目标的残余路径，以及一条经锁定阀的见证路径。"""
    residual_nodes = _shortest_source_path(base_graph, target_id, sources)
    if not residual_nodes:
        return None, None
    residual = _describe_path(full_open, edges, residual_nodes)
    locked_witness: dict[str, Any] | None = None
    # 若最短残余路径没有经过锁定阀，则在较短简单路径中找一条经过锁定阀的
    # 见证路径，以直观展示“是哪只锁死的阀门让来源仍能回到目标”。
    if not residual["locked_valves_on_path"]:
        locked_pairs = {
            frozenset((e["u"], e["v"]))
            for e in edges.values()
            if e["locked"] and e["is_open"]
        }
        best: list[str] | None = None
        for src in sources:
            if src not in base_graph or target_id not in base_graph:
                continue
            checked = 0
            for path in nx.shortest_simple_paths(base_graph, src, target_id):
                if best is not None and len(path) >= len(best):
                    break
                checked += 1
                if checked > 500:
                    break
                if any(frozenset((a, b)) in locked_pairs for a, b in zip(path, path[1:])):
                    best = path
                    break
        if best is not None:
            locked_witness = _describe_path(full_open, edges, best)
    return residual, locked_witness


def _enumerate_valid(
    edges: dict[str, dict[str, Any]],
    candidates: list[str],
    valid,
) -> tuple[list[set[str]], int]:
    """按阀门数递增枚举有效集合；返回（最小尺寸的全部有效集合, 枚举计数）。"""
    solutions: list[set[str]] = []
    examined = 0
    for size in range(0, len(candidates) + 1):
        for combo in combinations(candidates, size):
            examined += 1
            if examined > MAX_ENUMERATE:
                break
            if valid(set(combo)):
                solutions.append(set(combo))
        if examined > MAX_ENUMERATE:
            break
        if solutions:
            break
    return solutions, examined


def _enumerate_all_cuts(
    edges: dict[str, dict[str, Any]],
    candidates: list[str],
    targets: list[str],
    sources: list[str],
) -> list[set[str]]:
    """枚举所有能同时切断全部目标来源的候选集合（供无解诊断）。"""
    all_cuts: list[set[str]] = []
    examined = 0
    for size in range(0, len(candidates) + 1):
        stop = False
        for combo in combinations(candidates, size):
            examined += 1
            if examined > MAX_ENUMERATE:
                stop = True
                break
            g = build_graph(edges, set(combo))
            if all(not _reachable_sources(g, t, sources) for t in targets):
                all_cuts.append(set(combo))
        if stop:
            break
    return all_cuts


def _unconstrained_witness(
    edges: dict[str, dict[str, Any]],
    all_cuts: list[set[str]],
    essentials: list[str],
    sources: list[str],
) -> dict[str, Any] | None:
    """在“能切断目标”的集合中诊断断供：哪些必要供给点在任何切法下都保不住，
    并给出一只断供供给点最少的见证切集。"""
    if not all_cuts:
        return None
    ever_supplied: set[str] = set()
    essential_set = set(essentials)
    cuts_with_lost: list[tuple[set[str], set[str]]] = []
    for cut in all_cuts:
        g = build_graph(edges, cut)
        cut_p = {p for p in essentials if not _reachable_sources(g, p, sources)}
        for p in essential_set - cut_p:
            ever_supplied.add(p)
        cuts_with_lost.append((cut, cut_p))
    unavoidable = sorted(essential_set - ever_supplied)
    unavoidable_set = set(unavoidable)

    # 见证切集：在“只断供不可避免点”的切集中选阀门最少、字典序最小者
    clean_witness = [(cut, lost) for cut, lost in cuts_with_lost if lost == unavoidable_set]
    if clean_witness:
        clean_witness.sort(key=lambda t: (len(t[0]), sorted(t[0])))
        witness_cut, witness_lost = clean_witness[0]
    else:
        cuts_with_lost.sort(key=lambda t: (len(t[1]), len(t[0]), sorted(t[0])))
        witness_cut, witness_lost = cuts_with_lost[0]

    return {
        "close_valves": sorted(witness_cut),
        "size": len(witness_cut),
        "disconnects_essentials": sorted(witness_lost),
        "unavoidable_essentials": unavoidable,
    }


def compute_isolation(db: Session, target_id: str) -> dict[str, Any]:
    nodes, edges = _load(db)
    node_ids = {n.id for n in nodes}
    if target_id not in node_ids:
        raise TopologyError(f"目标节点不存在: {target_id}")

    sources = sorted(n.id for n in nodes if n.kind == "source")
    essentials = sorted(n.id for n in nodes if n.essential)
    if not sources:
        raise TopologyError("拓扑中没有介质来源节点")

    full_open = _full_open_graph(edges, node_ids)
    base_graph = build_graph(edges, set())

    # 可被本算法关闭的阀门 = 工艺可操作 + 当前打开 + 未锁定，
    # 且位于来源与目标之间的路径区域（死端支路上的阀门对隔离无意义）
    region = _path_region_valves(edges, base_graph, sources, [target_id])
    candidates = sorted(
        e["valve_id"]
        for e in edges.values()
        if e["valve_id"]
        and e["operable"]
        and e["is_open"]
        and not e["locked"]
        and e["valve_id"] in region
    )

    # 初始状态下目标已无来源路径：无需操作
    if not _reachable_sources(base_graph, target_id, sources):
        return {
            "feasible": True,
            "target_id": target_id,
            "sources": sources,
            "essentials": essentials,
            "candidate_valves": _valve_view(edges),
            "solutions": [{"close_valves": [], "size": 0, "alternative_rank": 0}],
            "best_solution": [],
            "residual_path": None,
            "locked_witness_path": None,
            "infeasible_reason": None,
            "unconstrained_best": None,
        }

    def valid(closed: set[str]) -> bool:
        g = build_graph(edges, closed)
        if _reachable_sources(g, target_id, sources):
            return False
        for p in essentials:
            if not _reachable_sources(g, p, sources):
                return False
        return True

    solutions, examined = _enumerate_valid(edges, candidates, valid)

    def enrich(closed: set[str], rank: int) -> dict[str, Any]:
        g = build_graph(edges, closed)
        open_bypass_edges: set[tuple[str, str]] = {
            (e["u"], e["v"])
            for e in edges.values()
            if e["is_bypass"] and e["valve_id"] not in closed and e["is_open"]
        }
        supply_paths = {
            p: _preferred_source_path(g, p, sources, open_bypass_edges)
            for p in essentials
        }
        return {
            "close_valves": sorted(closed),
            "size": len(closed),
            "alternative_rank": rank,
            "closes_bypass_valves": sorted(v for v in closed if any(e["valve_id"] == v and e["is_bypass"] for e in edges.values())),
            "supply_paths": supply_paths,
        }

    result: dict[str, Any] = {
        "feasible": bool(solutions),
        "target_id": target_id,
        "sources": sources,
        "essentials": essentials,
        "candidate_valves": _valve_view(edges),
        "examined_combinations": examined,
        "solutions": [],
        "best_solution": [],
        "residual_path": None,
        "infeasible_reason": None,
        "unconstrained_best": None,
    }

    if solutions:
        solutions.sort(key=lambda s: sorted(s))
        alts = [enrich(s, i) for i, s in enumerate(solutions[:MAX_ALTERNATIVES])]
        result["solutions"] = alts
        result["best_solution"] = alts[0]["close_valves"]
        return result

    # ---------- 不可行：诊断 ----------
    # 1) 枚举所有能切断目标来源的切集，判断哪些必要供给点“在任何切法下
    #    都无法保住”，并给出一只断供供给点最少的见证切集。
    all_cuts = _enumerate_all_cuts(edges, candidates, [target_id], sources)
    unconstrained = _unconstrained_witness(edges, all_cuts, essentials, sources)

    # 2) 残余路径：当前状态（锁定下）仍连通来源与目标的路径
    residual, locked_witness = _residual_and_locked_witness(
        base_graph, full_open, edges, sources, target_id
    )
    result["residual_path"] = residual
    result["locked_witness_path"] = locked_witness

    unavoidable = unconstrained["unavoidable_essentials"] if unconstrained else []
    if unavoidable:
        reason = (
            "在当前阀门模型下，任何能切断目标设备来源的切法都不可避免地断供必要供给点 "
            f"{', '.join(unavoidable)}（其余必要供给点可同时保住）；"
            "目标仍与来源连通的残余路径见上。"
        )
    elif locked_witness is not None or (
        residual and residual["locked_valves_on_path"]
    ):
        reason = "目标设备仍经锁定阀门与来源连通，该阀门不可关闭，无法形成隔离边界。"
    else:
        reason = "在当前锁定约束下找不到满足条件的隔离集合。"

    result["unconstrained_best"] = unconstrained
    result["infeasible_reason"] = reason
    return result


def compute_joint_isolation(db: Session, zones: list[dict[str, Any]]) -> dict[str, Any]:
    """联合隔离求解：同一物理连通图上同时隔离多个目标区域。

    zones: [{"target_id": str, "essentials": [str, ...]}, ...]
    联合有效集合须同时满足：
      a. 每个区域目标与所有介质来源断开；
      b. 每个区域登记的每个必要供给点仍与至少一个来源连通。
    取阀门数最少的联合集合，并给出每只阀门的区域归因（共享阀门 =
    被 ≥2 个区域依赖）。无解时不返回任何部分方案，只返回各区域残余/
    锁阀见证与不可避免断供诊断。
    """
    nodes, edges = _load(db)
    node_ids = {n.id for n in nodes}
    sources = sorted(n.id for n in nodes if n.kind == "source")
    if not sources:
        raise TopologyError("拓扑中没有介质来源节点")
    essential_ids = {n.id for n in nodes if n.essential}

    if not zones:
        raise TopologyError("联合隔离计划至少需要一个目标区域")

    targets: list[str] = []
    for z in zones:
        t = z["target_id"]
        if t not in node_ids:
            raise TopologyError(f"目标节点不存在: {t}")
        if t in sources:
            raise TopologyError(f"目标区域不能是介质来源: {t}")
        targets.append(t)
    duplicated = sorted({t for t in targets if targets.count(t) > 1})
    if duplicated:
        raise TopologyError(f"重复目标区域: {', '.join(duplicated)}")
    for z in zones:
        for p in z["essentials"]:
            if p not in node_ids:
                raise TopologyError(f"必要供给点不存在: {p}")
            if p not in essential_ids:
                raise TopologyError(f"节点未标记为必要供给点: {p}")
            if p in targets:
                raise TopologyError(f"必要供给点与目标区域冲突: {p}")

    # 各必要供给点被哪些区域要求保供（用于矛盾诊断说明）
    required = sorted({p for z in zones for p in z["essentials"]})
    required_by = {
        p: [z["target_id"] for z in zones if p in z["essentials"]] for p in required
    }

    full_open = _full_open_graph(edges, node_ids)
    base_graph = build_graph(edges, set())

    region = _path_region_valves(edges, base_graph, sources, targets)
    candidates = sorted(
        e["valve_id"]
        for e in edges.values()
        if e["valve_id"]
        and e["operable"]
        and e["is_open"]
        and not e["locked"]
        and e["valve_id"] in region
    )

    zone_specs = [
        {"target_id": z["target_id"], "essentials": sorted(set(z["essentials"]))}
        for z in zones
    ]
    result: dict[str, Any] = {
        "feasible": False,
        "zones": zone_specs,
        "sources": sources,
        "required_essentials": required,
        "required_by": required_by,
        "candidate_valves": _valve_view(edges),
        "examined_combinations": 0,
        "close_valves": [],
        "size": 0,
        "shared_valves": [],
        "valve_zones": {},
        "zone_evidence": [],
        "supply_paths": {},
        "zone_residuals": [],
        "unconstrained_best": None,
        "infeasible_reason": None,
    }

    # 初始状态下全部目标已无来源路径：无需操作
    if all(not _reachable_sources(base_graph, t, sources) for t in targets):
        result["feasible"] = True
        result["zone_evidence"] = [
            {
                "target_id": t,
                "essentials": z["essentials"],
                "boundary_valves": [],
                "isolated": True,
                "note": "初始状态已与来源断开，无需关阀",
            }
            for t, z in zip(targets, zone_specs)
        ]
        g = build_graph(edges, set())
        result["supply_paths"] = {
            p: _preferred_source_path(g, p, sources, set()) for p in required
        }
        return result

    def valid(closed: set[str]) -> bool:
        g = build_graph(edges, closed)
        for t in targets:
            if _reachable_sources(g, t, sources):
                return False
        for p in required:
            if not _reachable_sources(g, p, sources):
                return False
        return True

    solutions, examined = _enumerate_valid(edges, candidates, valid)
    result["examined_combinations"] = examined

    if solutions:
        solutions.sort(key=lambda s: sorted(s))
        best = solutions[0]
        g = build_graph(edges, best)

        # 区域归因：移除阀门 v 后区域 t 重新与来源连通 => t 依赖 v
        valve_zones: dict[str, list[str]] = {}
        for v in sorted(best):
            dependents = []
            for t in targets:
                g_without = build_graph(edges, best - {v})
                if _reachable_sources(g_without, t, sources):
                    dependents.append(t)
            valve_zones[v] = dependents
        shared = sorted(v for v, ts in valve_zones.items() if len(ts) >= 2)

        open_bypass_edges: set[tuple[str, str]] = {
            (e["u"], e["v"])
            for e in edges.values()
            if e["is_bypass"] and e["valve_id"] not in best and e["is_open"]
        }
        result.update(
            {
                "feasible": True,
                "close_valves": sorted(best),
                "size": len(best),
                "shared_valves": shared,
                "valve_zones": valve_zones,
                "zone_evidence": [
                    {
                        "target_id": t,
                        "essentials": z["essentials"],
                        "boundary_valves": sorted(
                            v for v in best if t in valve_zones.get(v, [])
                        ),
                        "isolated": not _reachable_sources(g, t, sources),
                        "note": "与全部来源断开",
                    }
                    for t, z in zip(targets, zone_specs)
                ],
                "supply_paths": {
                    p: _preferred_source_path(g, p, sources, open_bypass_edges)
                    for p in required
                },
                "alternatives": [sorted(s) for s in solutions[:MAX_ALTERNATIVES]],
            }
        )
        return result

    # ---------- 联合无解：诊断（不返回任何部分可用的关阀集合） ----------
    # 1) 各区域当前的残余路径与经锁定阀的见证路径
    for t in targets:
        residual, locked_witness = _residual_and_locked_witness(
            base_graph, full_open, edges, sources, t
        )
        result["zone_residuals"].append(
            {
                "target_id": t,
                "residual_path": residual,
                "locked_witness_path": locked_witness,
            }
        )

    # 2) 能同时切断全部目标的切集中，哪些被要求保供的供给点不可避免断供
    all_cuts = _enumerate_all_cuts(edges, candidates, targets, sources)
    unconstrained = _unconstrained_witness(edges, all_cuts, required, sources)
    result["unconstrained_best"] = unconstrained

    unavoidable = unconstrained["unavoidable_essentials"] if unconstrained else []
    locked_blocking = sorted(
        {
            v
            for zr in result["zone_residuals"]
            for path in (zr["residual_path"], zr["locked_witness_path"])
            if path
            for v in path["locked_valves_on_path"]
        }
    )
    parts: list[str] = []
    if unavoidable:
        described = [
            f"{p}（区域 {'、'.join(required_by[p])} 要求保供）" for p in unavoidable
        ]
        parts.append(
            f"任何能同时隔离 {'、'.join(targets)} 的切法都不可避免断供必要供给点 "
            f"{'、'.join(described)}，各区域保供要求相互矛盾"
        )
    if locked_blocking:
        parts.append(
            f"目标区域仍经锁定阀门 {'、'.join(locked_blocking)} 与来源连通，"
            "这些阀门不可关闭，无法形成联合隔离边界"
        )
    if parts:
        reason = "；".join(parts) + "。联合计划无解，未应用任何部分方案。"
    else:
        reason = "在当前锁定约束下找不到同时满足全部区域隔离与保供要求的联合集合。"
    result["infeasible_reason"] = reason
    return result


def topology_payload(db: Session) -> dict[str, Any]:
    nodes, edges = _load(db)
    return {
        "nodes": [
            {"id": n.id, "name": n.name, "kind": n.kind, "x": n.x, "y": n.y, "essential": n.essential}
            for n in nodes
        ],
        "segments": [
            {
                "id": e["id"],
                "source": e["u"],
                "target": e["v"],
                "direction": e["direction"],
                "kind": e["kind"],
                "is_bypass": e["is_bypass"],
                "valve_id": e["valve_id"],
            }
            for e in edges.values()
        ],
        "valves": _valve_view(edges),
    }
