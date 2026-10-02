import {
  AfterViewInit,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  ViewChild,
} from '@angular/core';
import cytoscape from 'cytoscape';
import { IsolationResult, JointPlan, Topology } from './models';

/**
 * Cytoscape.js 拓扑渲染：
 * - 节点：来源 / 目标设备 / 必要供给点 / 普通节点
 * - 边：管段（按保存的名义方向显示箭头），旁路使用虚线
 * - 单目标高亮：候选关闭阀（红）、残余/绕回供给路径（橙/绿）
 * - 联合计划高亮：目标区域（按区域配色边框）、共享关阀（红粗边+金色标记）、
 *   各区域保供路径（按区域配色）、不可行时逐区域残余/锁阀见证路径
 */
@Component({
  selector: 'app-network-graph',
  standalone: true,
  template: `<div #cy class="cy"></div>`,
  styles: [
    `
      .cy {
        width: 100%;
        height: 560px;
        background: #0f172a;
        border-radius: 10px;
      }
    `,
  ],
})
export class NetworkGraphComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() topology: Topology | null = null;
  @Input() result: IsolationResult | null = null;
  @Input() jointPlan: JointPlan | null = null;

  @ViewChild('cy') cyHost!: ElementRef<HTMLDivElement>;

  private cy: cytoscape.Core | null = null;

  // 区域索引 -> 配色（目标边框 / 保供路径 / 见证路径）
  private static readonly REGION_COLORS = ['#22d3ee', '#f472b6', '#a3e635', '#fbbf24'];

  ngAfterViewInit(): void {
    this.render();
  }

  ngOnChanges(): void {
    if (this.cy) {
      this.render();
    }
  }

  ngOnDestroy(): void {
    this.cy?.destroy();
    this.cy = null;
  }

  private colorFor(idx: number): string {
    return NetworkGraphComponent.REGION_COLORS[idx % NetworkGraphComponent.REGION_COLORS.length];
  }

  private render(): void {
    if (!this.topology) {
      return;
    }
    const topo = this.topology;

    const elements: cytoscape.ElementDefinition[] = [
      ...topo.nodes.map((n) => ({
        data: {
          id: n.id,
          label: `${n.name}${n.essential ? ' ★' : ''}`,
          kind: n.kind,
          essential: n.essential,
        },
        position: { x: n.x, y: n.y },
      })),
      ...topo.segments.map((s) => ({
        data: {
          id: s.id,
          source: s.source,
          target: s.target,
          label: s.valve_id ?? '',
          bypass: s.is_bypass,
        },
      })),
    ];

    if (this.cy) {
      this.cy.destroy();
    }
    this.cy = cytoscape({
      container: this.cyHost.nativeElement,
      elements,
      zoomingEnabled: true,
      userZoomingEnabled: true,
      panningEnabled: true,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': '#94a3b8',
            label: 'data(label)',
            color: '#e2e8f0',
            'font-size': '12px',
            'text-valign': 'bottom',
            'text-margin-y': 6,
            width: 26,
            height: 26,
            'border-width': 2,
            'border-color': '#cbd5e1',
          },
        },
        {
          selector: 'node[kind = "source"]',
          style: { 'background-color': '#16a34a', 'border-color': '#86efac' },
        },
        {
          selector: 'node[kind = "equipment"]',
          style: {
            'background-color': '#f59e0b',
            'border-color': '#fcd34d',
            width: 34,
            height: 34,
          },
        },
        {
          selector: 'node[essential = true]',
          style: { 'background-color': '#0284c7', 'border-color': '#7dd3fc' },
        },
        {
          selector: 'node.hl-target',
          style: { 'border-color': '#f8fafc', 'border-width': 4 },
        },
        {
          selector: 'edge',
          style: {
            width: 2.5,
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            label: 'data(label)',
            'font-size': '10px',
            color: '#cbd5e1',
            'text-background-color': '#0f172a',
            'text-background-opacity': 0.85,
            'text-background-padding': '2px',
          },
        },
        {
          selector: 'edge[bypass = true]',
          style: {
            'line-style': 'dashed',
            'line-color': '#a78bfa',
            'target-arrow-color': '#a78bfa',
          },
        },
        {
          selector: 'edge.close',
          style: {
            'line-color': '#ef4444',
            'target-arrow-color': '#ef4444',
            width: 5,
            color: '#fca5a5',
          },
        },
        {
          selector: 'edge.shared-close',
          style: {
            'line-color': '#ef4444',
            'target-arrow-color': '#ef4444',
            width: 7,
            color: '#fde68a',
            'line-style': 'solid',
          },
        },
        {
          selector: 'edge.planned-close',
          style: {
            'line-color': '#f59e0b',
            'target-arrow-color': '#f59e0b',
            width: 5,
            'line-style': 'dashed',
          },
        },
        {
          selector: 'edge.shared-planned',
          style: {
            'line-color': '#f59e0b',
            'target-arrow-color': '#f59e0b',
            width: 7,
            'line-style': 'dashed',
          },
        },
        {
          selector: 'edge.residual',
          style: {
            'line-color': '#fb923c',
            'target-arrow-color': '#fb923c',
            width: 4,
            'line-style': 'dotted',
          },
        },
        {
          selector: 'edge.witness',
          style: {
            'line-color': '#f87171',
            'target-arrow-color': '#f87171',
            width: 4,
          },
        },
        {
          selector: 'edge.supply',
          style: {
            'line-color': '#22d3ee',
            'target-arrow-color': '#22d3ee',
            width: 4,
          },
        },
        {
          selector: 'node.onpath',
          style: { 'border-color': '#67e8f9', 'border-width': 3 },
        },
        {
          selector: 'edge.rpath0',
          style: { 'line-color': this.colorFor(0), 'target-arrow-color': this.colorFor(0), width: 4 },
        },
        {
          selector: 'edge.rpath1',
          style: { 'line-color': this.colorFor(1), 'target-arrow-color': this.colorFor(1), width: 4 },
        },
        {
          selector: 'edge.rpath2',
          style: { 'line-color': this.colorFor(2), 'target-arrow-color': this.colorFor(2), width: 4 },
        },
        {
          selector: 'edge.rwitness0',
          style: { 'line-color': this.colorFor(0), 'target-arrow-color': this.colorFor(0), width: 3, 'line-style': 'dashed' },
        },
        {
          selector: 'edge.rwitness1',
          style: { 'line-color': this.colorFor(1), 'target-arrow-color': this.colorFor(1), width: 3, 'line-style': 'dashed' },
        },
        {
          selector: 'edge.rwitness2',
          style: { 'line-color': this.colorFor(2), 'target-arrow-color': this.colorFor(2), width: 3, 'line-style': 'dashed' },
        },
        {
          selector: 'node.rtarget0',
          style: { 'border-color': this.colorFor(0), 'border-width': 5 },
        },
        {
          selector: 'node.rtarget1',
          style: { 'border-color': this.colorFor(1), 'border-width': 5 },
        },
        {
          selector: 'node.rtarget2',
          style: { 'border-color': this.colorFor(2), 'border-width': 5 },
        },
        {
          selector: 'node.released-target',
          style: { 'border-color': '#475569', 'border-width': 3, 'border-style': 'dashed' },
        },
      ] as cytoscape.StylesheetJsonBlock[],
      layout: { name: 'preset' },
    });

    this.applyHighlights();
    this.cy!.fit(undefined, 40);
  }

  private markPath(nodePath: string[], cls: string): void {
    const cy = this.cy!;
    nodePath.forEach((n) => cy.$(`node#${n}`).addClass('onpath'));
    for (let i = 0; i < nodePath.length - 1; i++) {
      cy.edges(`[source = "${nodePath[i]}"][target = "${nodePath[i + 1]}"]`).addClass(cls);
      cy.edges(`[source = "${nodePath[i + 1]}"][target = "${nodePath[i]}"]`).addClass(cls);
    }
  }

  private applyHighlights(): void {
    const cy = this.cy;
    const topo = this.topology;
    if (!cy || !topo) {
      return;
    }

    const edgeByValve = new Map<string, string>();
    for (const s of topo.segments) {
      if (s.valve_id) {
        edgeByValve.set(s.valve_id, s.id);
      }
    }

    // 联合计划优先于单目标结果展示
    if (this.jointPlan) {
      this.applyJointHighlights(cy, edgeByValve);
      return;
    }

    const result = this.result;
    if (!result) {
      return;
    }

    // 目标设备
    cy.$(`node#${result.target_id}`).addClass('hl-target');

    if (result.feasible && result.solutions.length) {
      const best = result.solutions[0];
      for (const vid of best.close_valves) {
        const eid = edgeByValve.get(vid);
        if (eid) {
          cy.$(`edge#${eid}`).addClass('close');
        }
      }
      // 方案后必要供给点的来源路径
      for (const path of Object.values(best.supply_paths)) {
        if (!path) {
          continue;
        }
        this.markPath(path, 'supply');
      }
    } else if (!result.feasible) {
      if (result.residual_path) {
        this.markPath(result.residual_path.nodes, 'residual');
      }
      if (result.locked_witness_path) {
        this.markPath(result.locked_witness_path.nodes, 'witness');
      }
    }
  }

  private applyJointHighlights(
    cy: cytoscape.Core,
    edgeByValve: Map<string, string>,
  ): void {
    const plan = this.jointPlan!;
    const jr = plan.result;
    if (!jr) {
      return;
    }
    const regionIdx = new Map<string, number>();
    jr.regions.forEach((r, i) => regionIdx.set(r.target_id, i));
    const released = new Set(plan.regions.filter((r) => r.status === 'released').map((r) => r.target_id));

    // 区域目标节点：按区域配色；已释放区域改灰虚线边框
    for (const r of jr.regions) {
      const node = cy.$(`node#${r.target_id}`);
      if (released.has(r.target_id)) {
        node.addClass('released-target');
      } else {
        node.addClass(`rtarget${(regionIdx.get(r.target_id) ?? 0) % 3}`);
      }
    }

    const shared = new Set(jr.shared_valves);
    // prepared=计划中的关阀（琥珀虚线）；executing=已实际关断（红实线）；
    // released 后方案不再高亮关阀边（图随阀门状态恢复）
    const active = plan.status !== 'released';
    const executed = plan.status === 'executing';
    for (const vr of jr.valve_regions) {
      const eid = edgeByValve.get(vr.valve_id);
      if (!eid || !active) {
        continue;
      }
      const edge = cy.$(`edge#${eid}`);
      if (!executed) {
        edge.addClass(shared.has(vr.valve_id) ? 'shared-planned' : 'planned-close');
      } else {
        edge.addClass(shared.has(vr.valve_id) ? 'shared-close' : 'close');
      }
    }

    if (jr.feasible) {
      // 每个区域各自的保供路径：按区域配色
      jr.region_evidence.forEach((ev) => {
        const idx = (regionIdx.get(ev.target_id) ?? 0) % 3;
        for (const path of Object.values(ev.supply_paths)) {
          if (path) {
            this.markPath(path, `rpath${idx}`);
          }
        }
      });
    } else {
      // 不可行：逐区域残余路径（实色）与经锁阀见证路径（同色虚线）
      jr.residual_paths.forEach((p) => {
        this.markPath(p.nodes, `rpath${(regionIdx.get(p.target_id) ?? 0) % 3}`);
      });
      jr.locked_witness_paths.forEach((p) => {
        this.markPath(p.nodes, `rwitness${(regionIdx.get(p.target_id) ?? 0) % 3}`);
      });
    }
  }
}
