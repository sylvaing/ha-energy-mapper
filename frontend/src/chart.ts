// Stacked area chart of the per-minute breakdown (ECharts, tree-shaken).

import { LineChart } from "echarts/charts";
import {
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { type ECharts, init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { css, html, LitElement, type PropertyValues } from "lit";

import { seriesValue, type StackSeries } from "./data.ts";
import { formatPower, formatTime, type LocaleInfo } from "./format.ts";
import type { ChartTheme } from "./theme.ts";
import type { Report } from "./types.ts";

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  MarkAreaComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

export interface ChartModel {
  report: Report;
  stack: StackSeries[];
  /** series id -> colour */
  colors: Map<string, string>;
  /** series id -> display name */
  names: Map<string, string>;
  info: LocaleInfo;
  theme: ChartTheme;
  labels: { house: string; pv: string };
}

const escape = (text: string) =>
  text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

interface TooltipParam {
  seriesId: string;
  seriesName: string;
  color: string;
  value: [number, number];
}

export class EnergyMapperChart extends LitElement {
  static override properties = { model: { attribute: false }, narrow: { type: Boolean, reflect: true } };

  declare model: ChartModel | null;
  declare narrow: boolean;
  private chart: ECharts | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    super();
    this.model = null;
    this.narrow = false;
  }

  /** Zoom on [start, end] (ms). */
  zoom(start: number, end: number): void {
    this.chart?.dispatchAction({ type: "dataZoom", startValue: start, endValue: end });
  }

  resetZoom(): void {
    this.chart?.dispatchAction({ type: "dataZoom", start: 0, end: 100 });
  }

  override connectedCallback(): void {
    super.connectedCallback();
    if (this.hasUpdated) this.setup();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.chart?.dispose();
    this.chart = null;
  }

  protected override firstUpdated(): void {
    this.setup();
  }

  protected override updated(changed: PropertyValues): void {
    if (changed.has("model") || changed.has("narrow")) this.draw();
  }

  private setup(): void {
    const container = this.renderRoot.querySelector<HTMLDivElement>(".chart");
    if (!container || this.chart) return;
    this.chart = init(container, undefined, { renderer: "canvas" });
    this.resizeObserver = new ResizeObserver(() => this.chart?.resize());
    this.resizeObserver.observe(container);
    this.draw();
  }

  private draw(): void {
    if (!this.chart || !this.model) return;
    this.chart.setOption(this.option(this.model), { notMerge: true });
  }

  private option(m: ChartModel) {
    const { report, theme, info } = m;
    const curve = report.curve;
    const textStyle = { color: theme.secondaryText, fontFamily: theme.fontFamily };
    const stacked = m.stack.map((s) => {
      const color = m.colors.get(s.id) ?? theme.other;
      return {
        id: s.id,
        name: m.names.get(s.id) ?? s.name,
        type: "line" as const,
        stack: "house",
        symbol: "none",
        color,
        lineStyle: { width: 0.5, color },
        areaStyle: { color, opacity: 0.7 },
        emphasis: { disabled: true },
        data: curve.map((p) => [p.t * 1000, seriesValue(p, s)]),
      };
    });
    const peakAreas = report.peaks.map((p) => [
      {
        xAxis: p.start * 1000,
        itemStyle: { color: p.notable ? theme.error : theme.secondaryText, opacity: 0.12 },
      },
      { xAxis: (p.end + 60) * 1000 },
    ]);
    const house = {
      id: "house",
      name: m.labels.house,
      type: "line" as const,
      symbol: "none",
      color: theme.house,
      lineStyle: { width: 1, color: theme.house, opacity: 0.8 },
      emphasis: { disabled: true },
      z: 3,
      data: curve.map((p) => [p.t * 1000, p.house]),
      markArea: { silent: true, data: peakAreas },
      markLine:
        report.peak_threshold_w != null
          ? {
              silent: true,
              symbol: "none",
              lineStyle: { type: "dashed" as const, color: theme.error, width: 1 },
              label: {
                position: "insideEndTop" as const,
                color: theme.error,
                formatter: formatPower(report.peak_threshold_w, info),
              },
              data: [{ yAxis: report.peak_threshold_w }],
            }
          : undefined,
    };
    const hasPv = curve.some((p) => p.pv > 0);
    const pv = hasPv
      ? [
          {
            id: "pv",
            name: m.labels.pv,
            type: "line" as const,
            symbol: "none",
            color: theme.pv,
            lineStyle: { width: 1.5, color: theme.pv },
            emphasis: { disabled: true },
            z: 3,
            data: curve.map((p) => [p.t * 1000, p.pv]),
          },
        ]
      : [];
    const all = [...stacked, house, ...pv];
    return {
      animation: false,
      textStyle: { fontFamily: theme.fontFamily },
      grid: {
        left: 8,
        right: 12,
        top: 48,
        bottom: 56,
        outerBoundsMode: "same" as const,
        outerBoundsContain: "axisLabel" as const,
      },
      legend: {
        type: "scroll" as const,
        top: 0,
        left: 0,
        right: 0,
        icon: "circle",
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: theme.text, fontFamily: theme.fontFamily },
        pageIconColor: theme.text,
        pageIconInactiveColor: theme.divider,
        pageTextStyle: { color: theme.secondaryText },
        inactiveColor: theme.divider,
        data: all.map((s) => s.name),
      },
      tooltip: {
        trigger: "axis" as const,
        confine: true,
        triggerOn: "mousemove|click" as const,
        backgroundColor: theme.card,
        borderColor: theme.divider,
        textStyle: { color: theme.text, fontFamily: theme.fontFamily, fontSize: 12 },
        axisPointer: { type: "line" as const, lineStyle: { color: theme.secondaryText } },
        formatter: (params: TooltipParam[]) => this.tooltip(params, m),
      },
      xAxis: {
        type: "time" as const,
        min: report.start * 1000,
        max: report.end * 1000,
        axisLabel: { ...textStyle, hideOverlap: true, formatter: (v: number) => formatTime(v / 1000, info) },
        axisLine: { lineStyle: { color: theme.divider } },
        axisTick: { lineStyle: { color: theme.divider } },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value" as const,
        axisLabel: { ...textStyle, formatter: (v: number) => formatPower(v, info) },
        splitLine: { lineStyle: { color: theme.divider } },
      },
      dataZoom: [
        { type: "inside" as const, filterMode: "none" as const, zoomOnMouseWheel: "ctrl" as const, moveOnMouseWheel: false },
        {
          type: "slider" as const,
          filterMode: "none" as const,
          bottom: 8,
          height: 24,
          borderColor: theme.divider,
          fillerColor: "rgba(127, 127, 127, 0.15)",
          handleStyle: { color: theme.card, borderColor: theme.secondaryText },
          moveHandleStyle: { color: theme.divider },
          dataBackground: { lineStyle: { color: theme.divider }, areaStyle: { color: theme.divider } },
          selectedDataBackground: { lineStyle: { color: theme.primary }, areaStyle: { color: theme.primary, opacity: 0.2 } },
          textStyle: { color: theme.secondaryText },
          labelFormatter: (v: number) => formatTime(v / 1000, info),
          showDetail: !this.narrow,
        },
      ],
      series: all,
    };
  }

  private tooltip(params: TooltipParam[], m: ChartModel): string {
    if (!params.length) return "";
    const t = params[0].value[0] / 1000;
    const rows = params
      .filter((p) => p.seriesId === "house" || p.seriesId === "pv" || p.value[1] > 0)
      .sort((a, b) => {
        // lines first, then the stack from top to bottom
        const rank = (p: TooltipParam) => (p.seriesId === "house" ? -2 : p.seriesId === "pv" ? -1 : 0);
        return rank(a) - rank(b) || params.indexOf(b) - params.indexOf(a);
      })
      .map((p) => {
        const strong = p.seriesId === "house" ? "font-weight:500;" : "";
        return `<div style="display:flex;align-items:center;gap:8px;${strong}">
          <span style="width:8px;height:8px;border-radius:50%;background:${p.color};flex:none"></span>
          <span style="flex:1">${escape(p.seriesName)}</span>
          <span style="margin-left:16px">${formatPower(p.value[1], m.info)}</span></div>`;
      })
      .join("");
    return `<div style="margin-bottom:4px;font-weight:500">${formatTime(t, m.info)}</div>${rows}`;
  }

  protected override render() {
    return html`<div class="chart"></div>`;
  }

  static override styles = css`
    :host {
      display: block;
    }
    .chart {
      width: 100%;
      height: var(--energy-mapper-chart-height, 400px);
    }
    :host([narrow]) .chart {
      height: var(--energy-mapper-chart-height, 320px);
    }
  `;
}

if (!customElements.get("energy-mapper-chart")) {
  customElements.define("energy-mapper-chart", EnergyMapperChart);
}
