import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { IsolationPlan, IsolationResult, PlanSummary, Topology } from './models';

export interface PlanZoneSpec {
  target_id: string;
  essentials: string[];
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(): Observable<Topology> {
    return this.http.get<Topology>('/api/topology');
  }

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  reset(): Observable<unknown> {
    return this.http.post('/api/reset', {});
  }

  isolate(targetId: string, locks?: Record<string, boolean>): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
    });
  }

  // ---------------- 联合隔离计划 ----------------

  createPlan(name: string, requestKey: string, zones: PlanZoneSpec[]): Observable<IsolationPlan> {
    return this.http.post<IsolationPlan>('/api/plans', {
      name,
      request_key: requestKey,
      zones,
    });
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>('/api/plans');
  }

  getPlan(planId: string): Observable<IsolationPlan> {
    return this.http.get<IsolationPlan>(`/api/plans/${planId}`);
  }

  executePlan(planId: string): Observable<IsolationPlan> {
    return this.http.post<IsolationPlan>(`/api/plans/${planId}/execute`, {});
  }

  releaseZone(planId: string, zoneId: string): Observable<IsolationPlan> {
    return this.http.post<IsolationPlan>(`/api/plans/${planId}/zones/${zoneId}/release`, {});
  }
}
