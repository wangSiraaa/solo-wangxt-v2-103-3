import { Component, EventEmitter, Input, OnInit, Output, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import { JointPlan, Topology } from './models';

interface DraftRegion {
  target_id: string;
  supplies: string; // 逗号分隔输入，空 => 默认必要供给点
}

@Component({
  selector: 'app-joint-plan',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './joint-plan.component.html',
})
export class JointPlanComponent implements OnInit {
  private api = inject(ApiService);

  @Input() topology: Topology | null = null;
  @Output() selectedPlanChange = new EventEmitter<JointPlan | null>();
  @Output() planChanged = new EventEmitter<void>();

  plans = signal<JointPlan[]>([]);
  selectedId = signal<string | null>(null);
  selected = signal<JointPlan | null>(null);
  busy = signal(false);
  error = signal<string | null>(null);
  notice = signal<string | null>(null);

  drafts = signal<DraftRegion[]>([{ target_id: 'T', supplies: '' }]);

  ngOnInit(): void {
    this.loadPlans();
  }

  loadPlans(selectId?: string): void {
    this.api.listJointPlans().subscribe({
      next: (list) => {
        this.plans.set(list);
        const want = selectId ?? this.selectedId();
        const found = want ? list.find((p) => p.id === want) ?? null : null;
        if (found || want === null) {
          this.setSelected(found);
        }
      },
      error: (e) => this.error.set(`加载计划失败：${e.message ?? e}`),
    });
  }

  select(id: string): void {
    this.api.getJointPlan(id).subscribe({
      next: (p) => {
        this.setSelected(p);
        this.error.set(null);
      },
      error: (e) => this.error.set(e.error?.detail ?? e.message),
    });
  }

  private setSelected(p: JointPlan | null): void {
    this.selected.set(p);
    this.selectedId.set(p?.id ?? null);
    this.selectedPlanChange.emit(p);
  }

  get equipmentNodes() {
    return (this.topology?.nodes ?? []).filter((n) => n.kind === 'equipment');
  }

  get consumerNodes() {
    return (this.topology?.nodes ?? []).filter((n) => n.kind === 'consumer');
  }

  addDraft(): void {
    this.drafts.update((d) => [...d, { target_id: 'U', supplies: '' }]);
  }

  removeDraft(i: number): void {
    this.drafts.update((d) => d.filter((_, idx) => idx !== i));
  }

  private parseSupplies(raw: string): string[] | null {
    const ids = raw
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    return ids.length ? ids : null;
  }

  submit(): void {
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    const regions = this.drafts().map((d) => ({
      target_id: d.target_id,
      supply_node_ids: this.parseSupplies(d.supplies),
    }));
    this.api.createJointPlan(regions).subscribe({
      next: (p) => {
        this.busy.set(false);
        this.notice.set(
          p.idempotent_hit
            ? `幂等命中：登记已存在（${p.id}），未新增阀门或审计。`
            : `已登记并联合求解（${p.id}）。`,
        );
        this.loadPlans(p.id);
        this.planChanged.emit();
      },
      error: (e) => {
        this.busy.set(false);
        this.error.set(`登记失败：${e.error?.detail ?? e.message ?? e}`);
      },
    });
  }

  addRegion(target: string, supplies: string): void {
    if (!this.selected()) {
      return;
    }
    this.busy.set(true);
    this.api
      .addJointRegion(this.selected()!.id, {
        target_id: target,
        supply_node_ids: this.parseSupplies(supplies),
      })
      .subscribe({
        next: (p) => {
          this.busy.set(false);
          this.notice.set(
            p.idempotent_hit ? '该区域已在计划中（幂等），未重复创建。' : '已追加区域并重新联合求解。',
          );
          this.loadPlans(p.id);
          this.planChanged.emit();
        },
        error: (e) => {
          this.busy.set(false);
          this.error.set(e.error?.detail ?? e.message);
        },
      });
  }

  execute(): void {
    const p = this.selected();
    if (!p) {
      return;
    }
    this.busy.set(true);
    this.api.executeJointPlan(p.id).subscribe({
      next: (updated) => {
        this.busy.set(false);
        this.loadPlans(updated.id);
        this.planChanged.emit();
      },
      error: (e) => {
        this.busy.set(false);
        this.error.set(e.error?.detail ?? e.message);
      },
    });
  }

  release(targetId: string): void {
    const p = this.selected();
    if (!p) {
      return;
    }
    this.busy.set(true);
    this.api.releaseJointRegion(p.id, targetId).subscribe({
      next: (updated) => {
        this.busy.set(false);
        this.notice.set(
          updated.status === 'released'
            ? '最后一个区域已释放，阀门按计划恢复。'
            : `区域 ${targetId} 已释放；共享阀仍保持关闭。`,
        );
        this.loadPlans(updated.id);
        this.planChanged.emit();
      },
      error: (e) => {
        this.busy.set(false);
        this.error.set(e.error?.detail ?? e.message);
      },
    });
  }

  valveName(id: string): string {
    return this.topology?.valves.find((v) => v.id === id)?.name ?? id;
  }

  statusLabel(s: string): string {
    return { prepared: '准备中', executing: '执行中', released: '已释放（历史）' }[s] ?? s;
  }

  formatSupplies(ids: string[]): string {
    return ids.length ? ids.join('、') : '（默认全部必要供给点）';
  }

  regionTargets(plan: JointPlan): string {
    return plan.regions.map((r) => r.target_id).join(' + ');
  }

  reopenedValves(detail: Record<string, unknown> | undefined): string {
    const v = (detail?.['reopened_valves'] as string[] | undefined) ?? [];
    return v.join('、');
  }

  eventTarget(detail: Record<string, unknown> | undefined): string {
    return (detail?.['target_id'] as string | undefined) ?? '';
  }

  eventTime(at: string): string {
    // ISO 时间只展示 HH:MM:SS（第 11..19 个字符）
    return at && at.length >= 19 ? at.slice(11, 19) : at;
  }

  eventLabel(kind: string): string {
    return (
      {
        created: '登记/联合求解',
        executed: '按联合方案关阀',
        region_added: '追加区域并重算',
        region_released: '区域释放',
        restored: '按计划恢复阀门',
      }[kind] ?? kind
    );
  }
}
