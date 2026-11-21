// The Energy Mapper sidebar panel (registered by the integration with
// panel_custom). It runs inside the Home Assistant frontend: hass, narrow,
// route and panel are set by it.

import { mdiArrowExpandHorizontal, mdiChevronLeft, mdiChevronRight, mdiMenu, mdiRefresh } from "@mdi/js";
import { html, LitElement, nothing, type PropertyValues, type TemplateResult } from "lit";
import { html as staticHtml, unsafeStatic } from "lit/static-html.js";

import "./chart.ts";
import type { ChartModel, EnergyMapperChart } from "./chart.ts";
import {
  assignColors,
  dayEntries,
  type DayEntry,
  dayPresets,
  type DayStats,
  hourProfile,
  neighbourDay,
  peakWindow,
  registryIdOf,
  type StackSeries,
  stackSeries,
  statsDays,
  summary,
  unexplainedShare,
} from "./data.ts";
import {
  dayOf,
  dayRange,
  formatDay,
  formatDuration,
  formatEnergy,
  formatNumber,
  formatPercent,
  formatPower,
  formatTime,
  type LocaleInfo,
  localeInfo,
  previousDay,
} from "./format.ts";
import { adaptCalendar, definedPicker, type HaPickerTag, loadHaDatePicker } from "./ha-date-picker.ts";
import { type StringKey, type Translate, translator } from "./i18n.ts";
import {
  bestSuggestion,
  entityChoices,
  exampleWindow,
  formatHours,
  hasReview,
  initialForm,
  type LinkMode,
  linkDomains,
  linkable,
  reviewGroups,
  usualHours,
  validateMessage,
} from "./review.ts";
import { panelStyles } from "./styles.ts";
import { BASE_INDEX, chartTheme, type ChartTheme, SERIES_INDEXES, SUBMETERED_INDEX } from "./theme.ts";
import type {
  Days,
  Finding,
  HomeAssistant,
  Peak,
  PeakPart,
  Registry,
  RegistrySignature,
  Report,
  ReviewListing,
  ReviewSignature,
  SignatureExample,
  Status,
} from "./types.ts";

const REGISTRY_ROWS = 10;

interface WsError {
  code?: string;
  message?: string;
}

const defined = (tag: string) => customElements.get(tag) !== undefined;

export class EnergyMapperPanel extends LitElement {
  static override properties = {
    hass: { attribute: false },
    narrow: { type: Boolean },
    route: { attribute: false },
    panel: { attribute: false },
    _status: { state: true },
    _days: { state: true },
    _day: { state: true },
    _report: { state: true },
    _registry: { state: true },
    _loading: { state: true },
    _analyzing: { state: true },
    _error: { state: true },
    _notice: { state: true },
    _showAllSignatures: { state: true },
    _themeKey: { state: true },
    _single: { state: true },
    _picker: { state: true },
    _stats: { state: true },
    _review: { state: true },
    _reviewForm: { state: true },
    _reviewBusy: { state: true },
    _reviewError: { state: true },
    _showIgnored: { state: true },
  };

  static override styles = panelStyles;

  declare hass: HomeAssistant | undefined;
  declare narrow: boolean;
  declare route: unknown;
  declare panel: unknown;
  declare _status: Status | null;
  declare _days: Days | null;
  declare _day: string | null;
  declare _report: Report | null;
  declare _registry: Registry | null;
  declare _loading: boolean;
  declare _analyzing: boolean;
  declare _error: string | null;
  declare _notice: string | null;
  declare _showAllSignatures: boolean;
  declare _themeKey: string;
  declare _single: boolean;
  /** HA's date picker element, once loaded (null: native date input) */
  declare _picker: HaPickerTag | null;
  declare _stats: Record<string, DayStats>;
  /** the review of the signatures (administrators; null: not available) */
  declare _review: ReviewListing | null;
  /** the validation form open in the review */
  declare _reviewForm: { id: string; mode: LinkMode; name: string; entity: string; missing?: string } | null;
  declare _reviewBusy: string | null;
  declare _reviewError: string | null;
  declare _showIgnored: boolean;

  private started = false;
  private requestId = 0;
  private infoCache: { key: string; info: LocaleInfo } | null = null;
  private modelCache: { report: Report; registry: Registry | null; key: string; model: ChartModel } | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    super();
    this.hass = undefined;
    this.narrow = false;
    this._status = null;
    this._days = null;
    this._day = null;
    this._report = null;
    this._registry = null;
    this._loading = false;
    this._analyzing = false;
    this._error = null;
    this._notice = null;
    this._showAllSignatures = false;
    this._themeKey = "";
    this._single = false;
    this._picker = definedPicker();
    this._stats = {};
    this._review = null;
    this._reviewForm = null;
    this._reviewBusy = null;
    this._reviewError = null;
    this._showIgnored = false;
    // HA elements loaded later (lazily) replace the fallbacks
    for (const tag of ["ha-menu-button", "ha-icon-button", "ha-button", "ha-alert", "ha-entity-picker"]) {
      if (!defined(tag)) customElements.whenDefined(tag).then(() => this.requestUpdate());
    }
  }

  override connectedCallback(): void {
    super.connectedCallback();
    this.resizeObserver = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      const single = width > 0 && width < 1000;
      if (single !== this._single) this._single = single;
    });
    this.resizeObserver.observe(this);
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.resizeObserver?.disconnect();
  }

  // -- helpers ---------------------------------------------------------------
  private get t(): Translate {
    return translator(this.hass?.locale?.language || this.hass?.language);
  }

  private get info(): LocaleInfo {
    const h = this.hass;
    const key = JSON.stringify([h?.locale, h?.language, h?.config?.time_zone]);
    if (this.infoCache?.key !== key) {
      this.infoCache = { key, info: localeInfo(h?.locale, h?.language ?? "en", h?.config?.time_zone ?? "UTC") };
    }
    return this.infoCache.info;
  }

  private get isAdmin(): boolean {
    return Boolean(this.hass?.user?.is_admin);
  }

  private async ws<T>(msg: Record<string, unknown>): Promise<T> {
    if (!this.hass) throw { code: "no_hass", message: "not connected" } satisfies WsError;
    return this.hass.callWS<T>(msg);
  }

  private errorText(err: unknown): string {
    const e = (err ?? {}) as WsError;
    if (e.code === "not_loaded") return this.t("not_loaded");
    if (e.code === "addon_unavailable") return `${this.t("unavailable")}: ${e.message ?? ""}`;
    return e.message || String(err);
  }

  // -- loading ---------------------------------------------------------------
  /** The parts of hass the panel depends on: hass itself changes with every state change. */
  private hassKey(): string {
    const h = this.hass as (HomeAssistant & { dockedSidebar?: string }) | undefined;
    return JSON.stringify([h?.language, h?.locale, h?.config?.time_zone, h?.user?.is_admin, h?.themes, h?.dockedSidebar]);
  }

  private lastHassKey = "";

  protected override shouldUpdate(changed: PropertyValues): boolean {
    if (changed.size === 1 && changed.has("hass") && this.started) {
      const key = this.hassKey();
      if (key === this.lastHassKey) return false;
      this.lastHassKey = key;
    }
    return true;
  }

  protected override willUpdate(changed: PropertyValues): void {
    if (changed.has("hass") && this.hass) {
      const key = JSON.stringify([this.hass.themes?.theme, this.hass.themes?.darkMode]);
      if (key !== this._themeKey) {
        // let the theme variables apply before reading them
        requestAnimationFrame(() => (this._themeKey = key));
      }
      if (!this.started) {
        this.started = true;
        void this.load();
        if (!this._picker) void loadHaDatePicker().then((tag) => (this._picker = tag));
      }
    }
  }

  private async load(day?: string | null): Promise<void> {
    this._loading = true;
    this._error = null;
    try {
      this._status = await this.ws<Status>({ type: "energy_mapper/status" });
      if (!this._status.configured || !this._status.available) {
        this._days = null;
        this._report = null;
        return;
      }
      const [days, registry] = await Promise.all([
        this.ws<Days>({ type: "energy_mapper/days" }),
        this.ws<Registry>({ type: "energy_mapper/signatures" }),
        this.loadReview(),
      ]);
      this._days = days;
      this._registry = registry;
      const entries = dayEntries(days);
      void this.loadStats(entries);
      const wanted = day ?? this._day ?? new URLSearchParams(location.search).get("day");
      const chosen = wanted && /^\d{4}-\d{2}-\d{2}$/.test(wanted) ? wanted : (entries[0]?.day ?? null);
      if (chosen) await this.selectDay(chosen);
      else this._report = null;
    } catch (err) {
      this._error = this.errorText(err);
    } finally {
      this._loading = false;
    }
  }

  /** The review of the signatures (administrators only; an add-on before
   * 0.6.0 has none: the section is left out). */
  private async loadReview(): Promise<void> {
    if (!this.isAdmin) {
      this._review = null;
      return;
    }
    try {
      this._review = await this.ws<ReviewListing>({ type: "energy_mapper/review" });
    } catch {
      this._review = null;
    }
    if (this.reviewLinked && this._review) {
      // opened from the Repairs notice: show the section
      this.reviewLinked = false;
      void this.updateComplete.then(() =>
        this.renderRoot.querySelector("#review")?.scrollIntoView({ behavior: "smooth", block: "start" }),
      );
    }
  }

  private reviewLinked = new URLSearchParams(location.search).has("review");

  private async decide(sig: ReviewSignature, action: "wait" | "ignore" | "restore"): Promise<void> {
    this._reviewBusy = sig.id;
    this._reviewError = null;
    try {
      await this.ws({ type: `energy_mapper/${action}`, signature_id: sig.id });
      if (this._reviewForm?.id === sig.id) this._reviewForm = null;
      await this.loadReview();
    } catch (err) {
      this._reviewError = this.errorText(err);
    } finally {
      this._reviewBusy = null;
    }
  }

  private async validate(sig: ReviewSignature): Promise<void> {
    const form = this._reviewForm;
    if (!form || form.id !== sig.id) return;
    const built = validateMessage(sig, form.mode, form.name, form.entity, this.hass?.states?.[form.entity]?.attributes);
    if ("missing" in built) {
      this._reviewForm = { ...form, missing: built.missing };
      return;
    }
    this._reviewBusy = sig.id;
    this._reviewError = null;
    try {
      await this.ws(built.msg);
      this._reviewForm = null;
      this._notice = this.t("review_validated", { name: String(built.msg.name) });
      const [registry] = await Promise.all([
        this.ws<Registry>({ type: "energy_mapper/signatures" }),
        this.loadReview(),
      ]);
      this._registry = registry;
    } catch (err) {
      this._reviewError = this.errorText(err);
    } finally {
      this._reviewBusy = null;
    }
  }

  /** Show one use of a signature on the chart: its day, zoomed on it. */
  private async showExample(example: SignatureExample): Promise<void> {
    if (example.day !== this._day) await this.selectDay(example.day);
    await this.updateComplete;
    const report = this._report;
    if (!report || report.day !== example.day || !report.curve.length) return;
    // the chart is created by this update: let it render first
    requestAnimationFrame(() => {
      const chart = this.chartElement();
      if (!chart) return;
      const [a, b] = exampleWindow(example, report.start, report.end);
      chart.zoom(a, b);
      chart.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  /** Today in Home Assistant's time zone (the days of the reports). */
  private get today(): string {
    return dayOf(Date.now(), this.hass?.config?.time_zone ?? "UTC");
  }

  /** Totals of the recent days, for the presets of the day picker (optional). */
  private async loadStats(entries: DayEntry[]): Promise<void> {
    const days = statsDays(entries, this.today);
    if (!days.length) return;
    try {
      const result = await this.ws<{ days: Record<string, DayStats> }>({ type: "energy_mapper/day_stats", days });
      this._stats = result.days ?? {};
    } catch {
      this._stats = {}; // the presets that need them are left out
    }
  }

  private async selectDay(day: string): Promise<void> {
    const id = ++this.requestId;
    this._day = day;
    this._error = null;
    this._notice = null;
    try {
      const url = new URL(location.href);
      url.searchParams.set("day", day);
      history.replaceState(history.state, "", url);
    } catch {
      // not in a browser page that allows it
    }
    if (!dayEntries(this._days).some((e) => e.day === day)) {
      // no report: nothing to ask the add-on, the panel says so
      this._report = null;
      this._loading = false;
      return;
    }
    this._loading = true;
    try {
      const report = await this.ws<Report>({ type: "energy_mapper/report", day });
      if (id === this.requestId) this._report = report;
    } catch (err) {
      if (id === this.requestId) {
        this._report = null;
        this._error = this.errorText(err);
      }
    } finally {
      if (id === this.requestId) this._loading = false;
    }
  }

  private async analyze(day: string): Promise<void> {
    this._analyzing = true;
    this._error = null;
    this._notice = null;
    try {
      const result = await this.ws<{ ok: boolean; error?: string; message?: string }>({
        type: "energy_mapper/analyze",
        day,
      });
      if (result && result.ok === false) throw { code: result.error, message: result.message };
      await this.load(day);
      this._notice = this.t("analyze_done");
    } catch (err) {
      this._error = this.errorText(err);
    } finally {
      this._analyzing = false;
    }
  }

  // -- chart model -----------------------------------------------------------
  private chartModel(report: Report): ChartModel {
    const key = JSON.stringify([this._themeKey, this.infoCache?.key, this.hass?.language]);
    const c = this.modelCache;
    if (c && c.report === report && c.registry === this._registry && c.key === key) return c.model;
    const t = this.t;
    const theme: ChartTheme = chartTheme(this);
    const stack = stackSeries(report, this._registry);
    const indexes = assignColors(
      stack.filter((s) => s.kind === "known" || s.kind === "signature").map((s) => s.id),
      SERIES_INDEXES,
    );
    const colors = new Map<string, string>();
    const names = new Map<string, string>();
    for (const s of stack) {
      colors.set(
        s.id,
        s.kind === "base"
          ? theme.palette[BASE_INDEX]
          : s.kind === "submetered"
            ? theme.palette[SUBMETERED_INDEX]
            : s.kind === "other"
              ? theme.other
              : theme.palette[indexes.get(s.id) ?? 0],
      );
      names.set(s.id, this.seriesName(s, t));
    }
    const model: ChartModel = {
      report,
      stack,
      colors,
      names,
      info: this.info,
      theme,
      labels: { house: t("chart_house"), pv: t("chart_pv") },
    };
    this.modelCache = { report, registry: this._registry, key, model };
    return model;
  }

  private seriesName(s: StackSeries, t: Translate): string {
    if (s.kind === "base" || s.kind === "submetered" || s.kind === "other") return t(s.kind);
    if (s.kind === "signature" && s.name === "?") return t("unclassified");
    return s.name;
  }

  // -- rendering helpers -----------------------------------------------------
  private iconButton(path: string, label: string, onClick: () => void, disabled = false): TemplateResult {
    if (defined("ha-icon-button")) {
      return html`<ha-icon-button .path=${path} .label=${label} ?disabled=${disabled} @click=${onClick}></ha-icon-button>`;
    }
    return html`<button class="icon-button" title=${label} aria-label=${label} ?disabled=${disabled} @click=${onClick}>
      <svg viewBox="0 0 24 24"><path d=${path}></path></svg>
    </button>`;
  }

  private textButton(
    label: string,
    onClick: () => void,
    disabled = false,
    appearance: "plain" | "accent" = "plain",
    title = "",
  ): TemplateResult {
    if (defined("ha-button")) {
      return html`<ha-button
        appearance=${appearance}
        size="small"
        title=${title}
        ?disabled=${disabled}
        @click=${onClick}
        >${label}</ha-button
      >`;
    }
    return html`<button type="button" class="text-button ${appearance}" title=${title} ?disabled=${disabled} @click=${onClick}>
      ${label}
    </button>`;
  }

  private alert(type: "info" | "warning" | "error" | "success", text: string, title?: string): TemplateResult {
    if (defined("ha-alert")) {
      return html`<ha-alert alert-type=${type} .title=${title ?? ""}>${text}</ha-alert>`;
    }
    return html`<div class="alert ${type}" role="alert">
      <div>${title ? html`<div class="alert-title">${title}</div>` : nothing}${text}</div>
    </div>`;
  }

  private toggleMenu = () => {
    this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
  };

  // -- render ----------------------------------------------------------------
  protected override render(): TemplateResult {
    const t = this.t;
    return html`
      <div class="${this.narrow ? "narrow" : ""} ${this._single ? "single" : ""}">
        <div class="header">
          <div class="toolbar">
            ${defined("ha-menu-button")
              ? html`<ha-menu-button .hass=${this.hass} .narrow=${this.narrow}></ha-menu-button>`
              : this.narrow
                ? this.iconButton(mdiMenu, t("menu"), this.toggleMenu)
                : nothing}
            <div class="main-title">${t("title")}</div>
            ${this.iconButton(mdiRefresh, t("refresh"), () => void this.load(), this._loading || this._analyzing)}
          </div>
        </div>
        <div class="content">${this.renderContent(t)}</div>
      </div>
    `;
  }

  private renderContent(t: Translate): TemplateResult {
    const status = this._status;
    if (!status) {
      return this._error ? this.alert("error", this._error) : html`<div class="message">${t("loading")}</div>`;
    }
    if (!status.configured) return this.alert("info", t("not_configured"));
    if (!status.available) {
      return this.alert("error", status.error ?? "", status.state === "loaded" || status.state === "setup_retry" ? t("unavailable") : t("not_loaded"));
    }
    const entries = dayEntries(this._days);
    return html`
      ${this.renderDayBar(entries, t)}
      ${this._error ? this.alert("error", this._error, t("error")) : nothing}
      ${this._notice ? this.alert("success", this._notice) : nothing}
      ${entries.length === 0 ? this.renderNoReport(t) : nothing}
      ${entries.length > 0 && this._day && !entries.some((e) => e.day === this._day)
        ? this.renderNoReportDay(this._day, entries, t)
        : nothing}
      ${this.renderReview(t)}
      ${this._report && this._report.day === this._day ? this.renderReport(this._report, t) : nothing}
      ${this.renderRegistry(t)}
    `;
  }

  private renderNoReport(t: Translate): TemplateResult {
    const yesterday = previousDay(Date.now() / 1000, this.hass?.config?.time_zone ?? "UTC");
    return html`<div class="card">
      <div class="message">${t("no_report")}</div>
      ${this.isAdmin
        ? html`<div class="more">
            ${this.textButton(this._analyzing ? t("analyzing") : t("analyze_yesterday"), () => void this.analyze(yesterday), this._analyzing)}
          </div>`
        : nothing}
    </div>`;
  }

  private renderNoReportDay(day: string, entries: DayEntry[], t: Translate): TemplateResult {
    const info = this.info;
    const past = day < this.today;
    const days = entries.map((e) => e.day).sort();
    const outside = day < days[0] || day > days[days.length - 1];
    return html`<div class="card">
      <div class="message">
        <div class="no-report-day">${formatDay(day, info)}</div>
        ${!past ? t("day_not_over") : this.isAdmin ? t("no_report_day_admin") : t("no_report_day")}
        ${outside
          ? html`<div class="secondary">
              ${t("outside_range", { first: formatDay(days[0], info, "short"), last: formatDay(days[days.length - 1], info, "short") })}
            </div>`
          : nothing}
      </div>
      ${past && this.isAdmin
        ? html`<div class="more">
            ${this.textButton(this._analyzing ? t("analyzing") : t("analyze"), () => void this.analyze(day), this._analyzing)}
          </div>`
        : nothing}
    </div>`;
  }

  // the picker's dates, rebuilt only when their inputs change
  private pickerCache: { key: string; range: [Date, Date]; presets: Record<string, [Date, Date]> } | null = null;

  private pickerDates(day: string, t: Translate): { range: [Date, Date]; presets: Record<string, [Date, Date]> } {
    const tz = this.info.time_zone;
    const today = this.today;
    const key = JSON.stringify([day, tz, today, this.hass?.language, this._stats]);
    if (this.pickerCache?.key !== key) {
      const presets: Record<string, [Date, Date]> = {};
      for (const p of dayPresets(today, this._stats)) presets[t(`preset_${p.key}` as StringKey)] = dayRange(p.day, tz);
      this.pickerCache = { key, range: dayRange(day, tz), presets };
    }
    return this.pickerCache;
  }

  private pickerChanged = (ev: Event) => {
    const value = (ev as CustomEvent<{ value?: { startDate?: Date } }>).detail?.value;
    if (!value?.startDate) return;
    // one day: the first day of the selected range
    const day = dayOf(value.startDate, this.info.time_zone);
    if (day !== this._day) void this.selectDay(day);
  };

  private pickerOpened = (ev: Event) => {
    const days = dayEntries(this._days).map((e) => e.day).sort();
    if (days.length) adaptCalendar(ev.currentTarget as Element, days[0], days[days.length - 1]);
  };

  private renderDayPicker(entries: DayEntry[], t: Translate): TemplateResult {
    const day = this._day ?? entries[0].day;
    if (this._picker) {
      const { range, presets } = this.pickerDates(day, t);
      const tag = unsafeStatic(this._picker);
      return staticHtml`<${tag}
        class="ha-day-picker"
        .hass=${this.hass}
        .startDate=${range[0]}
        .endDate=${range[1]}
        .ranges=${presets}
        @value-changed=${this.pickerChanged}
        @click=${this.pickerOpened}
        @keydown=${this.pickerOpened}
      ></${tag}>`;
    }
    // outside Home Assistant (or the picker could not be loaded): a native
    // date input, and arrows that go to the previous/next day with a report
    const days = entries.map((e) => e.day).sort();
    const prev = neighbourDay(entries, day, -1);
    const next = neighbourDay(entries, day, 1);
    return html`${this.iconButton(mdiChevronLeft, t("previous_day"), () => prev && void this.selectDay(prev), !prev)}
      <input
        type="date"
        class="day-input"
        aria-label=${t("day")}
        .value=${day}
        min=${days[0]}
        max=${days[days.length - 1]}
        @change=${(ev: Event) => {
          const value = (ev.target as HTMLInputElement).value;
          if (value) void this.selectDay(value);
        }}
      />
      ${this.iconButton(mdiChevronRight, t("next_day"), () => next && void this.selectDay(next), !next)}`;
  }

  private renderDayBar(entries: DayEntry[], t: Translate): TemplateResult | typeof nothing {
    if (!entries.length) return nothing;
    const day = this._day;
    const hasReport = entries.some((e) => e.day === day);
    const summaryOnly = entries.find((e) => e.day === day)?.summary;
    return html`<div class="daybar">
      ${this.renderDayPicker(entries, t)}
      ${summaryOnly ? html`<span class="badge neutral">${t("summary_only")}</span>` : nothing}
      <div class="spacer"></div>
      ${this.isAdmin && day && hasReport
        ? this.textButton(
            this._analyzing ? t("analyzing") : t("analyze"),
            () => void this.analyze(day),
            this._analyzing || this._loading,
          )
        : nothing}
    </div>`;
  }

  private renderReport(report: Report, t: Translate): TemplateResult {
    const model = this._themeKey ? this.chartModel(report) : null;
    return html`
      ${this.renderFindings(report.findings ?? [], t)} ${this.renderSummary(report, model, t)}
      ${this.renderChart(report, model, t)}
      <div class="columns">${this.renderPeaks(report, model, t)} ${this.renderSwitchedLoads(t)}</div>
      ${this.renderQuality(report, t)}
    `;
  }

  private renderFindings(findings: Finding[], t: Translate): TemplateResult | typeof nothing {
    if (!findings.length) return nothing;
    return html`<div class="findings">
      ${findings.map((f) => {
        const key = `finding_${f.kind}` as StringKey;
        const title = t(key) === key ? t("finding_other") : t(key);
        const text = [f.detail, f.entity_id && !f.detail.includes(f.entity_id) ? `(${f.entity_id})` : ""]
          .filter(Boolean)
          .join(" ");
        return this.alert("warning", text, title);
      })}
    </div>`;
  }

  private renderSummary(report: Report, model: ChartModel | null, t: Translate): TemplateResult {
    const s = summary(report);
    const info = this.info;
    const share = (v: number) => (s.house > 0 ? t("of_house", { share: formatPercent(v / s.house, info) }) : "");
    const color = (id: string) => model?.colors.get(id);
    const tile = (label: string, value: number, sub: string, dot?: string, main = false) =>
      html`<div class="card tile ${main ? "main" : ""}">
        <div class="label">${dot ? html`<span class="dot" style="background:${dot}"></span>` : nothing}${label}</div>
        <div class="value">${formatEnergy(value, info)}</div>
        <div class="sub">${sub}</div>
      </div>`;
    const firstKnown = model?.stack.find((x) => x.kind === "known")?.id;
    const firstSig = model?.stack.find((x) => x.kind === "signature")?.id;
    return html`<div class="tiles">
      ${tile(t("house"), s.house, report.max_house_w != null ? t("max_power", { power: formatPower(report.max_house_w, info) }) : "", undefined, true)}
      ${tile(t("grid_import"), s.grid_import, share(s.grid_import))}
      ${s.has_pv ? tile(t("grid_export"), s.grid_export, "") : nothing}
      ${s.has_pv ? tile(t("pv"), s.pv, "", model?.theme.pv) : nothing}
      ${tile(t("base"), s.base, s.base_w != null ? t("always_on", { power: formatPower(s.base_w, info) }) : share(s.base), color("base"))}
      ${s.submetered > 0 ? tile(t("submetered"), s.submetered, share(s.submetered), color("submetered")) : nothing}
      ${tile(t("known"), s.known, share(s.known), firstKnown ? color(firstKnown) : undefined)}
      ${tile(t("signatures"), s.signatures, share(s.signatures), firstSig ? color(firstSig) : undefined)}
      ${tile(t("other"), s.other, share(s.other), color("other"))}
    </div>`;
  }

  private chartElement(): EnergyMapperChart | null {
    return this.renderRoot.querySelector("energy-mapper-chart");
  }

  private renderChart(report: Report, model: ChartModel | null, t: Translate): TemplateResult {
    const hasCurve = report.curve.length > 0;
    return html`<div class="card chart-card">
      <div class="card-header">
        ${t("chart_title")}
        <div class="spacer"></div>
        ${hasCurve ? this.iconButton(mdiArrowExpandHorizontal, t("reset_zoom"), () => this.chartElement()?.resetZoom()) : nothing}
      </div>
      <div class="card-content">
        ${hasCurve
          ? html`<energy-mapper-chart .model=${model} .narrow=${this.narrow}></energy-mapper-chart>`
          : html`<div class="message">${report.curve_pruned ? t("curve_pruned") : t("no_curve")}</div>`}
      </div>
    </div>`;
  }

  private partLabel(part: PeakPart, t: Translate): string {
    if (part.kind === "submetered") return t("submetered");
    if (part.kind === "signature") {
      const rid = part.registry_id;
      const label = rid ? this._registry?.signatures.find((s) => s.id === rid)?.label : null;
      const name = label || rid || part.name;
      return name === "?" ? t("unclassified") : name;
    }
    return part.name;
  }

  private partColor(part: PeakPart, model: ChartModel | null, report: Report): string | undefined {
    if (!model) return undefined;
    if (part.kind === "submetered") return model.colors.get("submetered");
    if (part.kind === "known") return model.colors.get(`known:${part.name}`);
    if (part.kind === "signature") return model.colors.get(`sig:${part.registry_id || registryIdOf(report, part.name)}`);
    return undefined;
  }

  private zoomOnPeak(report: Report, peak: Peak): void {
    const chart = this.chartElement();
    if (!chart) return;
    const [a, b] = peakWindow(peak, report.start, report.end);
    chart.zoom(a, b);
    chart.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  private renderPeaks(report: Report, model: ChartModel | null, t: Translate): TemplateResult {
    const info = this.info;
    const peaks = report.peaks ?? [];
    const canZoom = report.curve.length > 0;
    return html`<div class="card">
      <div class="card-header">${t("peaks")}</div>
      ${report.peak_threshold_w != null
        ? html`<div class="card-hint">${t("peak_threshold", { power: formatPower(report.peak_threshold_w, info) })}</div>`
        : nothing}
      ${peaks.length === 0
        ? html`<div class="message">${t("peaks_none")}</div>`
        : html`<ul class="peaks">
            ${peaks.map((p) => {
              const share = unexplainedShare(p);
              const [from, to] = [formatTime(p.start, info), formatTime(p.end, info)];
              const range = from === to ? from : `${from} – ${to}`;
              return html`<li>
                <button
                  class="peak"
                  title=${canZoom ? t("show_on_chart") : ""}
                  @click=${() => canZoom && this.zoomOnPeak(report, p)}
                >
                  <div class="peak-head">
                    <span class="peak-power">${formatPower(p.house_w, info)}</span>
                    <span class="peak-time">${range}</span>
                    ${p.notable ? html`<span class="badge">${t("notable")}</span>` : nothing}
                  </div>
                  <div class="chips">
                    ${p.parts.length === 0
                      ? html`<span class="chip">${t("nothing_identified")}</span>`
                      : p.parts.map((part) => {
                          const c = this.partColor(part, model, report);
                          return html`<span class="chip">
                            ${c ? html`<span class="dot" style="background:${c}"></span>` : nothing}
                            ${this.partLabel(part, t)}
                            <span class="chip-value">${formatPower(part.power_w, info)}</span>
                          </span>`;
                        })}
                    ${p.unexplained_w > 0
                      ? html`<span class="chip unexplained">
                          ${t("unexplained_part", {
                            power: formatPower(p.unexplained_w, info),
                            share: formatPercent(share, info),
                          })}
                        </span>`
                      : nothing}
                  </div>
                  <div class="share" aria-hidden="true"><div style="width:${(share * 100).toFixed(1)}%"></div></div>
                </button>
              </li>`;
            })}
          </ul>`}
    </div>`;
  }

  private renderSwitchedLoads(t: Translate): TemplateResult | typeof nothing {
    const loads = this._registry?.switched_loads ?? [];
    const info = this.info;
    const today = new Map((this._report?.known_loads ?? []).map((k) => [k.entity_id, k]));
    if (!loads.length && !today.size) return nothing;
    const rows = loads.length
      ? loads.map((l) => ({ name: l.name, entity_id: l.entity_id, power_w: l.power_w, inverted: l.inverted, days: l.days_seen }))
      : [...today.values()].map((k) => ({ name: k.name, entity_id: k.entity_id, power_w: k.power_w, inverted: k.inverted, days: null }));
    return html`<div class="card">
      <div class="card-header">${t("switched_loads")}</div>
      <div class="card-hint">${t("switched_hint")}</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>${t("col_load")}</th>
              <th class="num">${t("col_learned")}</th>
              <th class="num">${t("col_days")}</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(
              (r) => html`<tr>
                <td>
                  <div class="sig-name">
                    ${r.name}
                    ${r.inverted ? html`<span class="badge neutral" title=${t("inverted_hint")}>${t("inverted")}</span>` : nothing}
                  </div>
                  <div class="secondary">${r.entity_id}</div>
                </td>
                <td class="num">${r.power_w ? formatPower(r.power_w, info) : "—"}</td>
                <td class="num">${r.days != null ? formatNumber(r.days, info) : "—"}</td>
              </tr>`,
            )}
          </tbody>
        </table>
      </div>
    </div>`;
  }

  private renderRegistry(t: Translate): TemplateResult | typeof nothing {
    const registry = this._registry;
    if (!registry || !this._days || dayEntries(this._days).length === 0) return nothing;
    const all = registry.signatures ?? [];
    const shown = this._showAllSignatures ? all : all.slice(0, REGISTRY_ROWS);
    const model = this._report && this._themeKey ? this.chartModel(this._report) : null;
    const seen = new Set<string>();
    for (const s of this._report?.signatures ?? []) if (s.registry_id) seen.add(s.registry_id);
    return html`<div class="card">
      <div class="card-header">${t("registry")}</div>
      <div class="card-hint">${t("registry_hint")}</div>
      ${all.length === 0
        ? html`<div class="message">${t("registry_none")}</div>`
        : html`${this.narrow ? this.registryList(shown, model, seen, t) : this.registryTable(shown, model, seen, t)}
            ${all.length > REGISTRY_ROWS
              ? html`<div class="more">
                  ${this.textButton(
                    this._showAllSignatures ? t("show_less") : t("show_all", { count: String(all.length) }),
                    () => (this._showAllSignatures = !this._showAllSignatures),
                  )}
                </div>`
              : nothing}`}
    </div>`;
  }

  private signatureCells(s: RegistrySignature, model: ChartModel | null, seen: Set<string>, t: Translate) {
    const info = this.info;
    const c = model?.colors.get(`sig:${s.id}`);
    const profile = hourProfile(s.hours);
    const cycle =
      s.kind === "cycling" && s.on_s && s.period_s
        ? t("cycle", { on: formatDuration(s.on_s, info), period: formatDuration(s.period_s, info) })
        : "";
    const hoursTitle = s.hours
      .map((v, h) => (v > 0 ? `${h}h` : ""))
      .filter(Boolean)
      .join(" ");
    return {
      name: html`<div class="sig-name">
          ${c ? html`<span class="dot" style="background:${c}"></span>` : nothing} ${s.label || s.id}
          ${seen.has(s.id) ? html`<span class="badge neutral">${t("seen_this_day")}</span>` : nothing}
        </div>
        ${s.label ? html`<div class="secondary">${s.id}</div>` : nothing}`,
      kind: s.kind === "cycling" ? t("cycling") : t("steady"),
      cycle,
      power: formatPower(s.power_w, info),
      duration: formatDuration(s.duration_s, info),
      perWeek: formatNumber(s.per_week, info, 1),
      lastSeen: s.last_seen ? formatDay(s.last_seen, info, "short") : "—",
      hours: html`<div class="hours" title=${hoursTitle}>
        ${profile.map((v) => html`<div class=${v > 0 ? "" : "zero"} style="height:${Math.max(v * 100, 4)}%"></div>`)}
      </div>`,
    };
  }

  private registryTable(
    shown: RegistrySignature[],
    model: ChartModel | null,
    seen: Set<string>,
    t: Translate,
  ): TemplateResult {
    return html`<div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>${t("col_signature")}</th>
            <th>${t("col_kind")}</th>
            <th class="num">${t("col_power")}</th>
            <th class="num">${t("col_duration")}</th>
            <th class="num">${t("col_per_week")}</th>
            <th>${t("col_last_seen")}</th>
            <th>${t("col_hours")}</th>
          </tr>
        </thead>
        <tbody>
          ${shown.map((s) => {
            const x = this.signatureCells(s, model, seen, t);
            return html`<tr>
              <td>${x.name}</td>
              <td>${x.kind} ${x.cycle ? html`<div class="secondary">${x.cycle}</div>` : nothing}</td>
              <td class="num">${x.power}</td>
              <td class="num">${x.duration}</td>
              <td class="num">${x.perWeek}</td>
              <td>${x.lastSeen}</td>
              <td>${x.hours}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
  }

  /** Narrow screens: one block per signature instead of a wide table. */
  private registryList(
    shown: RegistrySignature[],
    model: ChartModel | null,
    seen: Set<string>,
    t: Translate,
  ): TemplateResult {
    return html`<ul class="sig-list">
      ${shown.map((s) => {
        const x = this.signatureCells(s, model, seen, t);
        const facts = [x.kind, x.power, x.duration, `${x.perWeek} / ${t("week")}`, x.lastSeen];
        return html`<li>
          <div class="sig-main">
            ${x.name}
            <div class="secondary">${facts.join(" · ")}</div>
            ${x.cycle ? html`<div class="secondary">${x.cycle}</div>` : nothing}
          </div>
          ${x.hours}
        </li>`;
      })}
    </ul>`;
  }

  // -- review of the signatures (#10) ---------------------------------------
  private renderReview(t: Translate): TemplateResult | typeof nothing {
    const listing = this._review;
    if (!listing || !this.isAdmin) return nothing;
    const groups = reviewGroups(listing);
    if (!hasReview(groups) && !this._reviewError) return nothing;
    const info = this.info;
    const c = listing.criteria;
    return html`<div class="card review" id="review">
      <div class="card-header">${t("review")}${groups.proposed.length ? html`<span class="count">${groups.proposed.length}</span>` : nothing}</div>
      <div class="card-hint">${t("review_hint")}</div>
      ${listing.suggest?.unrecorded_domains?.length
        ? html`<div class="card-hint">${t("review_unrecorded", { domains: listing.suggest.unrecorded_domains.join(", ") })}</div>`
        : nothing}
      ${this._reviewError ? html`<div class="card-content">${this.alert("error", this._reviewError)}</div>` : nothing}
      ${groups.proposed.length
        ? html`<ul class="review-list">${groups.proposed.map((s) => this.renderCandidate(s, t))}</ul>`
        : html`<div class="message">
            ${t("review_none", { uses: formatNumber(c.min_uses, info), days: formatNumber(c.min_days, info) })}
          </div>`}
      ${groups.waiting.length
        ? html`<div class="review-more">
            <div class="secondary">${t("review_waiting", { count: formatNumber(groups.waiting.length, info) })}</div>
            <ul class="review-compact">
              ${groups.waiting.map((s) => this.renderCompact(s, t, this.waitText(s, t)))}
            </ul>
          </div>`
        : nothing}
      ${groups.ignored.length
        ? html`<div class="review-more">
            ${this.textButton(
              t("review_ignored", { count: formatNumber(groups.ignored.length, info) }),
              () => (this._showIgnored = !this._showIgnored),
            )}
            ${this._showIgnored
              ? html`<ul class="review-compact">
                  ${groups.ignored.map((s) =>
                    this.renderCompact(s, t, s.review?.state === "validated" ? t("review_ignored_appliance") : ""),
                  )}
                </ul>`
              : nothing}
          </div>`
        : nothing}
    </div>`;
  }

  private waitText(s: ReviewSignature, t: Translate): string {
    const r = s.review;
    if (!r?.until_count || !r.until) return "";
    const day = dayOf(r.until * 1000, this.info.time_zone);
    return t("review_back", { uses: formatNumber(r.until_count, this.info), day: formatDay(day, this.info, "short") });
  }

  /** The facts of a signature: power, duration or rhythm, uses, energy, hours. */
  private reviewFacts(s: ReviewSignature, t: Translate): string[] {
    const info = this.info;
    const facts = [formatPower(s.power_w, info)];
    if (s.kind === "cycling" && s.on_s && s.period_s) {
      facts.push(t("cycle", { on: formatDuration(s.on_s, info), period: formatDuration(s.period_s, info) }));
      if (s.duration_s) facts.push(formatDuration(s.duration_s, info));
    } else {
      facts.push(formatDuration(s.duration_s, info));
    }
    facts.push(t("review_uses", { count: formatNumber(s.count, info), days: formatNumber(s.days_seen, info) }));
    if (s.per_week != null) facts.push(t("review_per_week", { count: formatNumber(s.per_week, info, 1) }));
    facts.push(t("review_energy", { energy: formatEnergy(s.energy_kwh, info) }));
    const hours = formatHours(usualHours(s.hours));
    if (hours) facts.push(t("review_hours", { hours }));
    return facts;
  }

  private renderCandidate(s: ReviewSignature, t: Translate): TemplateResult {
    const info = this.info;
    const busy = this._reviewBusy === s.id;
    const form = this._reviewForm?.id === s.id ? this._reviewForm : null;
    const profile = hourProfile(s.hours);
    const days = new Set(dayEntries(this._days).map((e) => e.day));
    return html`<li>
      <div class="review-head">
        <div class="review-main">
          <div class="sig-name">${s.id}</div>
          <div class="secondary">${this.reviewFacts(s, t).join(" · ")}</div>
          ${this.renderProbably(s, t)}
        </div>
        <div class="hours" title=${formatHours(usualHours(s.hours, 0.01, 24))}>
          ${profile.map((v) => html`<div class=${v > 0 ? "" : "zero"} style="height:${Math.max(v * 100, 4)}%"></div>`)}
        </div>
      </div>
      ${s.examples?.length
        ? html`<div class="chips">
            <span class="secondary">${t("review_examples")}</span>
            ${s.examples.map((ex) => {
              const label = `${formatDay(ex.day, info, "short")} ${formatTime(ex.start, info)}`;
              return days.has(ex.day)
                ? html`<button class="chip link" title=${t("show_on_chart")} @click=${() => void this.showExample(ex)}>
                    ${label}<span class="chip-value">${formatDuration(ex.end - ex.start, info)}</span>
                  </button>`
                : html`<span class="chip">${label}</span>`;
            })}
          </div>`
        : nothing}
      ${form
        ? this.renderValidateForm(s, form, t)
        : html`<div class="review-actions">
            ${this.textButton(t("review_validate"), () => (this._reviewForm = initialForm(s, (e) => this.friendlyName(e))), busy, "accent")}
            ${this.textButton(t("review_wait"), () => void this.decide(s, "wait"), busy, "plain", t("review_wait_hint"))}
            ${this.textButton(t("review_ignore"), () => void this.decide(s, "ignore"), busy, "plain", t("review_ignore_hint"))}
          </div>`}
    </li>`;
  }

  /** The friendly name of an entity, if Home Assistant knows it. */
  private friendlyName(entityId: string): string | undefined {
    const name = this.hass?.states?.[entityId]?.attributes?.friendly_name;
    return typeof name === "string" ? name : undefined;
  }

  /** "Probably: <entity> (92 %)": the best entity suggested (#41). */
  private renderProbably(s: ReviewSignature, t: Translate): TemplateResult | typeof nothing {
    const best = bestSuggestion(s);
    if (!best) return nothing;
    const info = this.info;
    const hint = t("review_probably_hint", {
      matches: formatNumber(best.matches, info),
      steps: formatNumber(best.steps, info),
      days: formatNumber(best.days, info),
    });
    return html`<div class="probably" title=${`${best.entity_id} · ${hint}`}>
      ${t("review_probably", {
        name: this.friendlyName(best.entity_id) ?? best.entity_id,
        score: formatPercent(best.score, info),
      })}
    </div>`;
  }

  /** The suggested entities in the validation form: one click links one. */
  private renderSuggested(s: ReviewSignature, form: NonNullable<EnergyMapperPanel["_reviewForm"]>, t: Translate): TemplateResult | typeof nothing {
    if (!s.suggestions?.length) return nothing;
    const info = this.info;
    return html`<div class="chips">
      <span class="secondary">${t("review_suggested")}</span>
      ${s.suggestions.map((c) => {
        const selected = form.mode === "switch" && form.entity === c.entity_id;
        return html`<button
          type="button"
          class="chip link ${selected ? "selected" : ""}"
          aria-pressed=${selected ? "true" : "false"}
          title=${c.entity_id}
          @click=${() => {
            this.setForm({ mode: "switch" });
            this.pickEntity(c.entity_id);
          }}
        >
          ${this.friendlyName(c.entity_id) ?? c.entity_id}<span class="chip-value">${formatPercent(c.score, info)}</span>
        </button>`;
      })}
    </div>`;
  }

  private renderCompact(s: ReviewSignature, t: Translate, note: string): TemplateResult {
    const facts = this.reviewFacts(s, t).slice(0, 3);
    if (note) facts.push(note);
    return html`<li>
      <div class="review-main">
        <span class="sig-name">${s.id}</span>
        <span class="secondary">${facts.join(" · ")}</span>
      </div>
      ${this.textButton(t("review_restore"), () => void this.decide(s, "restore"), this._reviewBusy === s.id)}
    </li>`;
  }

  private setForm(patch: Partial<NonNullable<EnergyMapperPanel["_reviewForm"]>>): void {
    if (this._reviewForm) this._reviewForm = { ...this._reviewForm, ...patch, missing: undefined };
  }

  private pickEntity(entity: string): void {
    const form = this._reviewForm;
    if (!form) return;
    const name = this.friendlyName(entity);
    // the name follows the entity until the user types one
    const typed = form.name.trim() && form.name !== this.friendlyName(form.entity);
    this.setForm({ entity, name: typed || name === undefined ? form.name : name });
  }

  private renderEntityPicker(form: NonNullable<EnergyMapperPanel["_reviewForm"]>, t: Translate): TemplateResult {
    if (defined("ha-entity-picker")) {
      return html`<ha-entity-picker
        .hass=${this.hass}
        .value=${form.entity}
        .label=${t("review_entity")}
        .includeDomains=${linkDomains(form.mode)}
        .includeDeviceClasses=${form.mode === "sensor" ? ["power"] : undefined}
        .entityFilter=${(st: { entity_id: string; attributes?: Record<string, unknown> }) =>
          linkable(form.mode, st.entity_id, st.attributes)}
        @value-changed=${(ev: CustomEvent<{ value?: string }>) => this.pickEntity(ev.detail?.value ?? "")}
      ></ha-entity-picker>`;
    }
    const listId = `em-entities-${form.mode}`;
    return html`<label class="field">
      <span>${t("review_entity")}</span>
      <input
        class="text-input"
        list=${listId}
        .value=${form.entity}
        @change=${(ev: Event) => this.pickEntity((ev.target as HTMLInputElement).value.trim())}
      />
      <datalist id=${listId}>
        ${entityChoices(this.hass?.states, form.mode).map((e) => html`<option value=${e.id}>${e.name}</option>`)}
      </datalist>
    </label>`;
  }

  private renderValidateForm(
    s: ReviewSignature,
    form: NonNullable<EnergyMapperPanel["_reviewForm"]>,
    t: Translate,
  ): TemplateResult {
    const busy = this._reviewBusy === s.id;
    const modes: LinkMode[] = ["new", "switch", "sensor"];
    return html`<form
      class="review-form"
      @submit=${(ev: Event) => {
        ev.preventDefault();
        void this.validate(s);
      }}
    >
      <fieldset>
        <legend>${t("review_mode")}</legend>
        ${modes.map(
          (m) => html`<label class="radio">
            <input
              type="radio"
              name="mode-${s.id}"
              .checked=${form.mode === m}
              @change=${() => this.setForm({ mode: m, entity: "" })}
            />
            ${t(`review_mode_${m}` as StringKey)}
          </label>`,
        )}
      </fieldset>
      ${this.renderSuggested(s, form, t)}
      <div class="secondary">${t(`review_mode_${form.mode}_hint` as StringKey)}</div>
      ${form.mode !== "new" ? this.renderEntityPicker(form, t) : nothing}
      <label class="field">
        <span>${t("review_name")}</span>
        <input
          class="text-input"
          maxlength="100"
          .value=${form.name}
          @input=${(ev: Event) => this.setForm({ name: (ev.target as HTMLInputElement).value })}
          @keydown=${(ev: KeyboardEvent) => {
            if (ev.key === "Enter") {
              ev.preventDefault();
              void this.validate(s);
            }
          }}
        />
      </label>
      ${form.missing
        ? html`<div class="field-error">${form.missing === "name" ? t("review_missing_name") : t("review_missing_entity")}</div>`
        : nothing}
      <div class="review-actions">
        ${this.textButton(t("review_cancel"), () => (this._reviewForm = null), busy)}
        ${this.textButton(t("review_confirm"), () => void this.validate(s), busy, "accent")}
      </div>
    </form>`;
  }

  private renderQuality(report: Report, t: Translate): TemplateResult | typeof nothing {
    const q = report.quality;
    if (!q) return nothing;
    const info = this.info;
    return html`<div class="footer">
      ${t("quality", {
        samples: formatNumber(q.samples, info),
        gap: q.max_gap_s != null ? formatDuration(q.max_gap_s, info) : "—",
        time: formatDuration(q.compute_s ?? 0, info),
      })}
    </div>`;
  }
}

if (!customElements.get("energy-mapper-panel")) {
  customElements.define("energy-mapper-panel", EnergyMapperPanel);
}

declare global {
  interface HTMLElementTagNameMap {
    "energy-mapper-panel": EnergyMapperPanel;
    "energy-mapper-chart": EnergyMapperChart;
  }
}
