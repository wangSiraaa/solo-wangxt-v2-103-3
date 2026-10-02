import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { IsolationResult, JointPlan, JointRegionInput, Topology } from './models';

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

  createJointPlan(regions: JointRegionInput[]): Observable<JointPlan> {
    return this.http.post<JointPlan>('/api/joint-plans', { regions });
  }

  listJointPlans(): Observable<JointPlan[]> {
    return this.http.get<JointPlan[]>('/api/joint-plans');
  }

  getJointPlan(id: string): Observable<JointPlan> {
    return this.http.get<JointPlan>(`/api/joint-plans/${id}`);
  }

  addJointRegion(id: string, region: JointRegionInput): Observable<JointPlan> {
    return this.http.post<JointPlan>(`/api/joint-plans/${id}/regions`, region);
  }

  executeJointPlan(id: string): Observable<JointPlan> {
    return this.http.post<JointPlan>(`/api/joint-plans/${id}/execute`, {});
  }

  releaseJointRegion(id: string, targetId: string): Observable<JointPlan> {
    return this.http.post<JointPlan>(
      `/api/joint-plans/${id}/regions/${targetId}/release`,
      {},
    );
  }
}
