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
import { IsolationPlan, IsolationResult, Topology } from './models';

/**
 * Cytoscape.js 拓扑渲染：
 * - 节点：来源 / 目标设备 / 必要供给点 / 普通节点
 * - 边：管段（按保存的名义方向显示箭头），旁路使用虚线
 * - 单目标高亮：候选关闭阀（红）、残余/绕回供给路径（橙/青）
 * - 联合计划高亮：区域目标（紫框）、计划关阀（红）、共享阀（金色加粗）、
 *   保供路径（青）、无解时各区残余/锁阀见证路径（橙/红）
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
  @Input() plan: IsolationPlan | null = null;

  @ViewChild('cy') cyHost!: ElementRef<HTMLDivElement>;

  private cy: cytoscape.Core | null = null;

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
          selector: 'node.zone-target',
          style: { 'border-color': '#e879f9', 'border-width': 4 },
        },
        {
          selector: 'node.zone-released',
          style: { opacity: 0.45 },
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
          selector: 'edge.shared',
          style: {
            'line-color': '#fbbf24',
            'target-arrow-color': '#fbbf24',
            width: 7,
            color: '#fde68a',
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
      ] as cytoscape.StylesheetJsonBlock[],
      layout: { name: 'preset' },
    });

    this.applyHighlights();
    this.cy!.fit(undefined, 40);
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

    const markPath = (nodePath: string[], cls: string) => {
      nodePath.forEach((n) => cy.$(`node#${n}`).addClass('onpath'));
      for (let i = 0; i < nodePath.length - 1; i++) {
        cy.edges(`[source = "${nodePath[i]}"][target = "${nodePath[i + 1]}"]`).addClass(cls);
        cy.edges(`[source = "${nodePath[i + 1]}"][target = "${nodePath[i]}"]`).addClass(cls);
      }
    };

    if (this.plan) {
      this.applyPlanHighlights(cy, topo, edgeByValve, markPath);
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
        if (path) {
          markPath(path, 'supply');
        }
      }
    } else if (!result.feasible) {
      if (result.residual_path) {
        markPath(result.residual_path.nodes, 'residual');
      }
      if (result.locked_witness_path) {
        markPath(result.locked_witness_path.nodes, 'witness');
      }
    }
  }

  /** 联合计划高亮：与数据库阀门状态联动——只高亮当前仍关闭的计划阀门，
   *  区域释放并刷新拓扑后，对应高亮自动消退。 */
  private applyPlanHighlights(
    cy: cytoscape.Core,
    topo: Topology,
    edgeByValve: Map<string, string>,
    markPath: (path: string[], cls: string) => void,
  ): void {
    const plan = this.plan!;
    const closedNow = new Set(topo.valves.filter((v) => !v.is_open).map((v) => v.id));
    const shared = new Set(plan.shared_valves);

    // 区域目标节点（已释放区域淡化）
    for (const z of plan.zones) {
      cy.$(`node#${z.target_id}`).addClass('zone-target');
      if (z.status === 'released') {
        cy.$(`node#${z.target_id}`).addClass('zone-released');
      }
    }

    // 计划关阀：共享阀金色加粗、专有阀红色；仅高亮当前仍关闭的
    for (const vid of plan.close_valves) {
      if (!closedNow.has(vid)) {
        continue;
      }
      const eid = edgeByValve.get(vid);
      if (eid) {
        cy.$(`edge#${eid}`).addClass(shared.has(vid) ? 'shared' : 'close');
      }
    }

    if (plan.feasible) {
      // 各供给点保供路径（计划未完全释放时展示）
      if (plan.status !== 'released') {
        for (const path of Object.values(plan.solve.supply_paths ?? {})) {
          if (path) {
            markPath(path, 'supply');
          }
        }
      }
    } else {
      // 无解：各区域残余路径与经锁定阀见证路径
      for (const zr of plan.solve.zone_residuals ?? []) {
        if (zr.residual_path) {
          markPath(zr.residual_path.nodes, 'residual');
        }
        if (zr.locked_witness_path) {
          markPath(zr.locked_witness_path.nodes, 'witness');
        }
      }
    }
  }
}
