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

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
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

export interface PlanZone {
  id: string;
  target_id: string;
  essentials: string[];
  status: 'prepared' | 'executing' | 'released';
  boundary_valves: string[];
}

export interface ZoneEvidence {
  target_id: string;
  essentials: string[];
  boundary_valves: string[];
  isolated: boolean;
  note: string;
}

export interface ZoneResidual {
  target_id: string;
  residual_path: ResidualPath | null;
  locked_witness_path: ResidualPath | null;
}

export interface JointSolve {
  feasible: boolean;
  zones: { target_id: string; essentials: string[] }[];
  sources: string[];
  required_essentials: string[];
  required_by: Record<string, string[]>;
  examined_combinations: number;
  close_valves: string[];
  size: number;
  shared_valves: string[];
  valve_zones: Record<string, string[]>;
  zone_evidence: ZoneEvidence[];
  supply_paths: Record<string, string[] | null>;
  zone_residuals: ZoneResidual[];
  unconstrained_best: {
    close_valves: string[];
    size: number;
    disconnects_essentials: string[];
    unavoidable_essentials: string[];
  } | null;
  infeasible_reason: string | null;
}

export interface PlanEvent {
  seq: number;
  action: string;
  detail: Record<string, any> & {
    released_target?: string;
    restored_valves?: string[];
    still_closed_valves?: string[];
    applied_close_valves?: string[];
  };
  created_at: string;
}

export interface IsolationPlan {
  id: string;
  name: string;
  status: 'prepared' | 'executing' | 'released' | 'infeasible';
  feasible: boolean;
  close_valves: string[];
  shared_valves: string[];
  zones: PlanZone[];
  solve: JointSolve;
  created_at: string;
  updated_at: string;
  events: PlanEvent[];
}

export interface PlanSummary {
  id: string;
  name: string;
  status: string;
  feasible: boolean;
  close_valves: string[];
  shared_valves: string[];
  zone_count: number;
  created_at: string;
}
