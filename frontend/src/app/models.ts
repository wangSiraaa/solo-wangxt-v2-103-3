export interface TopoNode {
  id: string;
  name: string;
  kind: 'source' | 'equipment' | 'consumer' | 'junction';
  x: number;
  y: number;
  essential: boolean;
}

export interface Segment {
  id: string;
  source: string;
  target: string;
  direction: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve_id: string | null;
}

export interface Valve {
  id: string;
  name: string;
  segment_id: string;
  endpoints: string[];
  is_open: boolean;
  locked: boolean;
  operable: boolean;
  is_bypass: boolean;
}

export interface Solution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
  examined_combinations: number;
  solutions: Solution[];
  best_solution: string[];
  residual_path: ResidualPath | null;
  locked_witness_path: ResidualPath | null;
  infeasible_reason: string | null;
  unconstrained_best: {
    close_valves: string[];
    size: number;
    disconnects_essentials: string[];
    unavoidable_essentials: string[];
  } | null;
}

// ---------------- 联合隔离计划 ----------------

export interface JointRegionInput {
  target_id: string;
  supply_node_ids?: string[] | null;
}

export interface ValveRegion {
  valve_id: string;
  regions: string[];
  shared: boolean;
}

export interface JointRegionEvidence {
  target_id: string;
  supply_node_ids: string[];
  target_isolated: boolean;
  boundary_valves: string[];
  supply_paths: Record<string, string[] | null>;
}

export interface JointSolution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  valve_regions: ValveRegion[];
  shared_valves: string[];
  region_evidence: JointRegionEvidence[];
}

export interface JointRegionUnconstrained {
  target_id: string;
  disconnects_supplies: string[];
  unavoidable_supplies: string[];
}

export interface JointResult {
  feasible: boolean;
  regions: { target_id: string; supply_node_ids: string[] }[];
  sources: string[];
  examined_combinations: number;
  solutions: JointSolution[];
  best_solution: string[];
  shared_valves: string[];
  valve_regions: ValveRegion[];
  region_evidence: JointRegionEvidence[];
  residual_paths: (ResidualPath & { target_id: string })[];
  locked_witness_paths: (ResidualPath & { target_id: string })[];
  infeasible_reason: string | null;
  unconstrained_best: {
    close_valves: string[];
    size: number;
    per_region: JointRegionUnconstrained[];
  } | null;
  contradictions: {
    target_region: string;
    supply_region: string;
    node: string;
    message: string;
  }[];
}

export interface PlanRegionState {
  idx: number;
  target_id: string;
  supply_node_ids: string[];
  status: 'prepared' | 'released';
  required_valves: string[];
  released_at: string | null;
}

export interface PlanEvent {
  id: number;
  at: string;
  kind: string;
  detail: Record<string, unknown>;
}

export interface ActivePlanBrief {
  id: string;
  status: 'prepared' | 'executing' | 'released';
  feasible: boolean;
  close_valves: string[];
  shared_valves: string[];
  regions: { target_id: string; status: string; required_valves: string[] }[];
}

export interface JointPlan {
  id: string;
  status: 'prepared' | 'executing' | 'released';
  feasible: boolean;
  idempotent_hit: boolean;
  created_at: string;
  executed_at: string | null;
  released_at: string | null;
  regions: PlanRegionState[];
  result: JointResult;
  infeasible_reason: string | null;
  request_snapshot: { fingerprint: string; regions: JointRegionInput[] };
  events: PlanEvent[];
}

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
  active_plans?: ActivePlanBrief[];
}
