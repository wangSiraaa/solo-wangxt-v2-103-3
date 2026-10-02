import { Component, OnInit, inject, signal } from '@angular/core';
import { ApiService, PlanZoneSpec } from './api.service';
import { IsolationPlan, IsolationResult, PlanSummary, Topology } from './models';
import { NetworkGraphComponent } from './network-graph.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);

  topology = signal<Topology | null>(null);
  result = signal<IsolationResult | null>(null);
  targetId = signal<string>('T');
  loading = signal(false);
  error = signal<string | null>(null);

  // 阀门锁定状态（仅前端选择，计算时随请求提交并由后端持久化）
  locks = signal<Record<string, boolean>>({});

  // ---------------- 联合隔离计划 ----------------
  plans = signal<PlanSummary[]>([]);
  activePlan = signal<IsolationPlan | null>(null);
  // 区域草稿：待登记的目标区域及其必要供给点
  draftTarget = signal<string>('U');
  draftEssentials = signal<Record<string, boolean>>({});
  draftZones = signal<PlanZoneSpec[]>([]);
  // 每个计划草稿一个幂等键：重复提交/重试不会生成第二套阀门或重复审计
  private draftKey = crypto.randomUUID();

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.loading.set(true);
    this.api.topology().subscribe({
      next: (t) => {
        this.topology.set(t);
        const l: Record<string, boolean> = {};
        for (const v of t.valves) {
          l[v.id] = v.locked;
        }
        this.locks.set(l);
        if (!Object.keys(this.draftEssentials()).length) {
          const e: Record<string, boolean> = {};
          for (const n of t.nodes.filter((n) => n.essential)) {
            e[n.id] = true;
          }
          this.draftEssentials.set(e);
        }
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`无法加载拓扑：${e.message ?? e}`);
        this.loading.set(false);
      },
    });
    this.refreshPlans();
  }

  refreshPlans(): void {
    this.api.listPlans().subscribe((ps) => {
      this.plans.set(ps);
      const active = this.activePlan();
      if (active) {
        this.api.getPlan(active.id).subscribe((p) => this.activePlan.set(p));
      }
    });
  }

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.isolate(this.targetId(), this.locks()).subscribe({
      next: (r) => {
        this.result.set(r);
        // 同步锁定勾选状态（后端为权威来源）
        this.locks.update((l) => {
          const next = { ...l };
          for (const v of r.candidate_valves) {
            next[v.id] = v.locked;
          }
          return next;
        });
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`计算失败：${e.error?.detail ?? e.message ?? e}`);
        this.loading.set(false);
      },
    });
  }

  /** 快捷装载三个培训样例的锁定组合（样例 1 先重置）。 */
  loadSample(sample: 1 | 2 | 3): void {
    const applyLocksAndCompute = () => {
      const l: Record<string, boolean> = {};
      for (const v of this.topology()?.valves ?? []) {
        l[v.id] = false;
      }
      if (sample === 2) {
        l['V_TIN'] = true;
      } else if (sample === 3) {
        l['V_TOUT'] = true;
      }
      this.locks.set(l);
      this.compute();
    };

    if (sample === 1) {
      // 重置 -> 重新拉取拓扑 -> 计算
      this.api.reset().subscribe(() => {
        this.api.topology().subscribe((t) => {
          this.topology.set(t);
          const l: Record<string, boolean> = {};
          for (const v of t.valves) {
            l[v.id] = v.locked;
          }
          this.locks.set(l);
          applyLocksAndCompute();
        });
      });
    } else {
      applyLocksAndCompute();
    }
  }

  resetAll(): void {
    this.api.reset().subscribe(() => {
      this.result.set(null);
      this.activePlan.set(null);
      this.draftZones.set([]);
      this.draftKey = crypto.randomUUID();
      this.reload();
    });
  }

  // ---------------- 联合隔离计划操作 ----------------

  targetOptions(): string[] {
    return (this.topology()?.nodes ?? [])
      .filter((n) => n.kind !== 'source')
      .map((n) => n.id);
  }

  essentialOptions(): string[] {
    return (this.topology()?.nodes ?? []).filter((n) => n.essential).map((n) => n.id);
  }

  toggleDraftEssential(nodeId: string): void {
    this.draftEssentials.update((e) => ({ ...e, [nodeId]: !e[nodeId] }));
  }

  addDraftZone(): void {
    const target = this.draftTarget();
    if (!target || this.draftZones().some((z) => z.target_id === target)) {
      return;
    }
    const essentials = Object.entries(this.draftEssentials())
      .filter(([, on]) => on)
      .map(([id]) => id)
      .sort();
    this.draftZones.update((zs) => [...zs, { target_id: target, essentials }]);
  }

  removeDraftZone(targetId: string): void {
    this.draftZones.update((zs) => zs.filter((z) => z.target_id !== targetId));
  }

  createPlan(): void {
    if (!this.draftZones().length) {
      return;
    }
    this.error.set(null);
    this.api.createPlan('联合隔离计划', this.draftKey, this.draftZones()).subscribe({
      next: (p) => {
        this.activePlan.set(p);
        this.draftZones.set([]);
        this.draftKey = crypto.randomUUID(); // 下一份草稿用新幂等键
        this.refreshPlans();
      },
      error: (e) => this.error.set(`创建计划失败：${e.error?.detail ?? e.message ?? e}`),
    });
  }

  selectPlan(planId: string): void {
    this.api.getPlan(planId).subscribe((p) => this.activePlan.set(p));
  }

  executePlan(): void {
    const p = this.activePlan();
    if (!p) {
      return;
    }
    this.error.set(null);
    this.api.executePlan(p.id).subscribe({
      next: (plan) => {
        this.activePlan.set(plan);
        this.reload(); // 阀门状态已变化，刷新拓扑联动高亮
      },
      error: (e) => this.error.set(`执行失败：${e.error?.detail ?? e.message ?? e}`),
    });
  }

  releaseZone(zoneId: string): void {
    const p = this.activePlan();
    if (!p) {
      return;
    }
    this.error.set(null);
    this.api.releaseZone(p.id, zoneId).subscribe({
      next: (plan) => {
        this.activePlan.set(plan);
        this.reload(); // 释放后共享阀保持/专有阀恢复，刷新拓扑联动
      },
      error: (e) => this.error.set(`释放失败：${e.error?.detail ?? e.message ?? e}`),
    });
  }

  // ---------------- 展示辅助 ----------------

  valveName(id: string): string {
    return this.topology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  nodeName(id: string): string {
    return this.topology()?.nodes.find((n) => n.id === id)?.name ?? id;
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }

  zoneStatusLabel(status: string): string {
    return (
      { prepared: '准备', executing: '执行中', released: '已释放', infeasible: '无解' }[
        status
      ] ?? status
    );
  }

  eventLabel(action: string): string {
    return (
      {
        created: '创建计划',
        executed: '执行计划（应用关阀）',
        zone_released: '释放区域',
        plan_released: '计划完成（阀门恢复）',
      }[action] ?? action
    );
  }
}
