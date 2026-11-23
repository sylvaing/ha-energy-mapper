// The Energy Mapper sidebar panel (registered by the integration with
// panel_custom). It runs inside the Home Assistant frontend: hass, narrow,
// route and panel are set by it.

import {
  mdiArrowExpandHorizontal,
  mdiChevronLeft,
  mdiChevronRight,
  mdiCogOutline,
  mdiFormatListChecks,
  mdiLightbulbOutline,
  mdiMenu,
  mdiRefresh,
} from "@mdi/js";
import { html, LitElement, nothing, svg, type PropertyValues, type TemplateResult } from "lit";
import { html as staticHtml, unsafeStatic } from "lit/static-html.js";

import {
  type ApplianceCard,
  applianceCards,
  type ApplianceListing,
  type DailyEnergy,
  dailyEnergy,
  type Period,
  PERIOD_DAYS,
  PERIODS,
  type StatRow,
  statisticIds,
  statsRequest,
  type Tracking,
} from "./appliances.ts";
import { energyState, pickList, preselected, type TrackingListing } from "./takeover.ts";
import { exampleLines } from "./examples.ts";
import { sensorChoices, teachingLines, teachState, type TeachingListing } from "./teaching.ts";
import {
  canStart,
  deviceChoices,
  entitiesOf,
  flatPowerSensors,
  formFor,
  formNotes,
  stateOptions,
  teachMessage,
  toggleState,
  withState,
  type ConnectedEntity,
  type ConnectedForm,
  type ConnectedListing,
} from "./connected.ts";
import { selfTestHint, selfTestRows, showSelfTest, type SelfTestListing } from "./selftest.ts";
import "./chart.ts";
import "./live-view.ts";
import type { ChartModel, EnergyMapperChart } from "./chart.ts";
import {
  assignColors,
  categorySeries,
  maxPowerTime,
  type SplitKey,
  splitParts,
  dayEntries,
  type DayEntry,
  dayPresets,
  type DayStats,
  hourProfile,
  neighbourDay,
  registryIdOf,
  type StackSeries,
  stackSeries,
  statsDays,
  summary,
  namedSignatureIds,
  seriesCategory,
  timeSpan,
  usageDetail,
  usageRows,
  type UsageRow,
  type Namer,
  peakPartName,
  registryName,
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
import { findingView } from "./findings.ts";
import { adaptCalendar, definedPicker, type HaPickerTag, loadHaDatePicker } from "./ha-date-picker.ts";
import { type StringKey, type Translate, translator } from "./i18n.ts";
import { distinctNames, phasesSummary, provisionalName } from "./names.ts";
import {
  bestSuggestion,
  blankForm,
  currentOf,
  drivesLoad,
  entityChoices,
  exampleDrawing,
  exampleWindow,
  filterCounts,
  filterOf,
  formatHours,
  formForMerge,
  formForSuggestion,
  formForUse,
  type LinkMode,
  linkDomains,
  linkable,
  nextAfter,
  REVIEW_FILTERS,
  type ReviewFilter,
  type ReviewForm,
  reviewProgress,
  reviewQueue,
  shortcutFor,
  shortcutLetters,
  stepFrom,
  suggestionLabel,
  typingTarget,
  usualHours,
  validateMessage,
} from "./review.ts";
import {
  type BlockPos,
  firstBlock,
  moveBlock,
  type RasterBlock,
  rasterDayLabel,
  rasterLastDay,
  rasterRows,
  rasterTicks,
} from "./raster.ts";
import { panelStyles } from "./styles.ts";
import {
  axisTicks,
  bucketCount,
  houseBuckets,
  isWholeDay,
  notablePeaks,
  peakZoom,
  type TimelineRow,
  timelineRows,
  visibleBlocks,
} from "./timeline.ts";
import { availableTabs, searchWithTab, shownTab, type Tab, tabForKey, tabFromSearch, toReviewCount } from "./tabs.ts";
import {
  BASE_INDEX,
  chartTheme,
  type ChartTheme,
  ESTIMATED_INDEX,
  SERIES_INDEXES,
  splitColor,
  SUBMETERED_INDEX,
} from "./theme.ts";
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
/** Rows of the review queue shown before "Show the N others" (#51). */
const QUEUE_ROWS = 12;
/** Days whose house curve is kept for the examples of the review. */
const MAX_EXAMPLE_CURVES = 12;

/** The house curve of a day, for the examples of the review (#51). */
interface ExampleCurve {
  start: number;
  end: number;
  points: Array<{ t: number; house: number }>;
}

interface WsError {
  code?: string;
  message?: string;
}

const defined = (tag: string) => customElements.get(tag) !== undefined;

interface TimelineSelection {
  any: boolean;
  dim: boolean;
  on: (row: TimelineRow) => boolean;
}

/** One bar of the house strip (in the strip's viewBox units). */
/** The integration's page, where an appliance's subentry is reconfigured. */
const SETTINGS_PATH = "/config/integrations/integration/energy_mapper";

const svgBar = (x: number, y: number, h: number) =>
  svg`<rect x=${x + 0.1} y=${y} width="0.8" height=${h}></rect>`;

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
    _reviewFilter: { state: true },
    _reviewCurrent: { state: true },
    _reviewChecked: { state: true },
    _queueAll: { state: true },
    _exampleCurves: { state: true },
    _tab: { state: true },
    _dayView: { state: true },
    _window: { state: true },
    _category: { state: true },
    _appliance: { state: true },
    _appliances: { state: true },
    _period: { state: true },
    _applianceStats: { state: true },
    _tracking: { state: true },
    _takeover: { state: true },
    _teaching: { state: true },
    _teach: { state: true },
    _connected: { state: true },
    _connectedForm: { state: true },
    _selfTest: { state: true },
    _liveUp: { state: true },
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
  /** the validation form of the signature shown, once edited */
  declare _reviewForm: ReviewForm | null;
  declare _reviewBusy: string | null;
  declare _reviewError: string | null;
  /** the filter of the review queue (#51) */
  declare _reviewFilter: ReviewFilter;
  /** the signature shown in the review (null: the first of the queue) */
  declare _reviewCurrent: string | null;
  /** the signatures selected for a bulk action */
  declare _reviewChecked: string[];
  /** the whole queue shown, not only its first rows */
  declare _queueAll: boolean;
  /** the block of the day raster that takes the focus (roving tabindex) */
  private rasterPos: { sig: string; pos: BlockPos } | null = null;
  /** the house curves of the days of the examples (null: none) */
  declare _exampleCurves: Record<string, ExampleCurve | null>;
  /** the tab asked for (URL ``?tab=``); the one shown may differ (#47) */
  declare _tab: Tab;
  /** the category of the split bar selected on the Today tab (#48), null: all */
  declare _category: SplitKey | null;
  /** the appliance selected in "What used energy" (#50): a chart series id, null: none */
  declare _appliance: string | null;
  /** the view of the day card (#49): the timeline or the stacked chart */
  declare _dayView: "timeline" | "stacked";
  /** the window (s) shown by the timeline, null: the whole day */
  declare _window: [number, number] | null;
  /** the appliances (integration 0.7.0+; null: not available) */
  declare _appliances: ApplianceListing | null;
  /** the period of the Appliances tab (#54) */
  declare _period: Period;
  /** daily energy of the appliances, 30 days to the selected day (energy
   * null: the statistics could not be read) */
  declare _applianceStats: { key: string; energy: DailyEnergy | null } | null;
  /** take over tracking in the Energy dashboard (#13, administrators; null: not available) */
  declare _tracking: TrackingListing | null;
  /** the confirmation open on an appliance card: take over (with the device
   * picked) or undo; busy while the integration applies it */
  declare _takeover: { key: string; mode: "take" | "undo"; stat: string; busy: boolean; error: string | null } | null;
  /** learning from a measured device (#14; null: not available) */
  declare _teaching: TeachingListing | null;
  /** the nightly self-test (#15; null: not available) */
  declare _selfTest: SelfTestListing | null;
  /** the devices that can teach from their state (#79; read when the form opens) */
  declare _connected: ConnectedListing | null;
  /** the form "Learn from a connected appliance" (#79): on a card (key) or a new appliance (key null) */
  declare _connectedForm: ConnectedForm | null;
  /** the live view streams (#17): the pulsing dot of its tab */
  declare _liveUp: boolean;
  /** the confirmation open on an appliance card: start learning (with the
   * sensor picked), finish with the learned signature, or stop */
  declare _teach: {
    key: string;
    mode: "start" | "finish" | "stop";
    entity: string;
    busy: boolean;
    error: string | null;
  } | null;

  private started = false;
  private requestId = 0;
  private infoCache: { key: string; info: LocaleInfo } | null = null;
  private modelCache: { report: Report; registry: Registry | null; key: string; model: ChartModel } | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private highlightCache: { key: string; ids: string[] | null } | null = null;
  private queueCache: { listing: ReviewListing | null; filter: ReviewFilter; queue: ReviewSignature[] } | null = null;
  private curveRequests = new Set<string>();
  private curveTimer: ReturnType<typeof setTimeout> | undefined;

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
    this._reviewFilter = "energy";
    this._reviewCurrent = null;
    this._reviewChecked = [];
    this._queueAll = false;
    this._exampleCurves = {};
    this._tab = tabFromSearch(location.search);
    this._category = null;
    this._appliance = null;
    this._dayView = "timeline";
    this._window = null;
    this._appliances = null;
    this._period = "day";
    this._applianceStats = null;
    this._tracking = null;
    this._takeover = null;
    this._teaching = null;
    this._teach = null;
    this._connected = null;
    this._connectedForm = null;
    this._selfTest = null;
    this._liveUp = false;
    if (new URLSearchParams(location.search).has("review")) this.writeTab(this._tab);
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
  /** Provisional name of a signature without label (#52). */
  private get namer(): Namer {
    const t = this.t;
    const info = this.info;
    return (sig) => provisionalName(sig, t, info);
  }

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

  protected override updated(): void {
    this.loadExampleCurves();
    void this.loadApplianceStats();
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
        this.loadAppliances(),
        this.loadTracking(),
        this.loadTeaching(),
        this.loadSelfTest(),
      ]);
      this._days = days;
      this._registry = registry;
      // read the statistics again (a new analysis imports some)
      if (this._applianceStats) this._applianceStats = { ...this._applianceStats, key: "" };
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
  }

  /** The appliances (an integration before 0.7.0 has no such command: the
   * tab keeps the signatures only). */
  private async loadAppliances(): Promise<void> {
    try {
      this._appliances = await this.ws<ApplianceListing>({ type: "energy_mapper/appliances" });
    } catch {
      this._appliances = null;
    }
  }

  /** What the appliances can take over in the Energy dashboard (#13):
   * administrators, integration 0.8.0+; else nothing is offered. */
  private async loadTracking(): Promise<void> {
    if (!this.isAdmin) {
      this._tracking = null;
      return;
    }
    try {
      this._tracking = await this.ws<TrackingListing>({ type: "energy_mapper/tracking" });
    } catch {
      this._tracking = null;
    }
  }

  /** The learning status of the appliances taught by a sensor (#14, every
   * user; integration 0.9.0+, else nothing is shown). */
  private async loadTeaching(): Promise<void> {
    try {
      this._teaching = await this.ws<TeachingListing>({ type: "energy_mapper/teaching" });
    } catch {
      this._teaching = null;
    }
  }

  /** Open the form "Learn from a connected appliance" (#79) on an appliance
   * card (``key``) or for a new appliance (``key`` null), filled with the
   * entities the integration proposes for the best device. */
  private async openConnected(key: string | null): Promise<void> {
    this._teach = null;
    this._connectedForm = { ...formFor(undefined, key), busy: true };
    try {
      this._connected = await this.ws<ConnectedListing>({ type: "energy_mapper/connected_devices" });
    } catch (err) {
      this._connected = null;
      if (this._connectedForm) this._connectedForm = { ...formFor(undefined, key), error: this.errorText(err) };
      return;
    }
    const first = deviceChoices(this._connected, key)[0];
    const name = key === null ? "" : (this._appliances?.appliances.find((a) => a.key === key)?.name ?? "");
    this._connectedForm = formFor(first, key, key === null ? "" : name);
  }

  /** Start learning from the connected appliance of the open form. */
  private async applyConnected(): Promise<void> {
    const form = this._connectedForm;
    if (!form || !canStart(form)) return;
    this._connectedForm = { ...form, busy: true, error: null };
    try {
      await this.ws(teachMessage(form));
      const name = form.key === null ? form.name.trim() : (this._appliances?.appliances.find((a) => a.key === form.key)?.name ?? "");
      this._notice = this.t("connected_started", { name, state: this.sensorName(form.entity) });
      this._connectedForm = null;
    } catch (err) {
      if (this._connectedForm) this._connectedForm = { ...this._connectedForm, busy: false, error: this.errorText(err) };
    }
    await Promise.all([this.loadAppliances(), this.loadTeaching()]);
  }

  /** The nightly self-test (#15, every user; integration 0.9.0+, else
   * nothing is shown). */
  private async loadSelfTest(): Promise<void> {
    try {
      this._selfTest = await this.ws<SelfTestListing>({ type: "energy_mapper/self_test" });
    } catch {
      this._selfTest = null;
    }
  }

  /** Start, finish or stop learning, after the confirmation on the card. */
  private async applyTeach(name: string): Promise<void> {
    const form = this._teach;
    if (!form || form.busy || (form.mode === "start" && !form.entity)) return;
    const appliance = this._appliances?.appliances.find((a) => a.key === form.key);
    const entity = form.mode === "start" ? form.entity : (appliance?.teacher?.entity_id ?? "");
    const sensor = this.sensorName(entity);
    this._teach = { ...form, busy: true, error: null };
    try {
      if (form.mode === "start") {
        await this.ws({ type: "energy_mapper/teach", appliance: form.key, entity_id: form.entity });
        this._notice = this.t("teach_started", { name, sensor });
      } else {
        await this.ws({ type: "energy_mapper/stop_teaching", appliance: form.key, use: form.mode === "finish" });
        this._notice = form.mode === "finish" ? this.t("teach_finished", { name }) : this.t("teach_stopped", { name, sensor });
      }
      this._teach = null;
    } catch (err) {
      if (this._teach) this._teach = { ...this._teach, busy: false, error: this.errorText(err) };
    }
    await Promise.all([this.loadAppliances(), this.loadTeaching(), this.loadTracking()]);
  }

  private sensorName(entityId: string): string {
    return this.friendlyName(entityId) ?? entityId;
  }

  /** Take over, or undo, after the confirmation on the card. */
  private async applyTakeover(name: string): Promise<void> {
    const form = this._takeover;
    if (!form || form.busy || (form.mode === "take" && !form.stat)) return;
    const state = energyState(this._tracking, form.key);
    const device =
      form.mode === "undo"
        ? state.kind === "taken_over"
          ? state.device
          : ""
        : (this._tracking?.devices.find((d) => d.stat_consumption === form.stat)?.name ?? form.stat);
    this._takeover = { ...form, busy: true, error: null };
    try {
      if (form.mode === "take") {
        await this.ws({ type: "energy_mapper/take_over", appliance: form.key, stat_consumption: form.stat });
        this._notice = this.t("takeover_done", { name, device });
      } else {
        await this.ws({ type: "energy_mapper/undo_take_over", appliance: form.key });
        this._notice = this.t("takeover_undone", { device });
      }
      this._takeover = null;
    } catch (err) {
      if (this._takeover) this._takeover = { ...this._takeover, busy: false, error: this.errorText(err) };
    }
    await this.loadTracking();
  }

  /** The daily energy of the appliances (recorder statistics, one request
   * for the 30 days ending on the selected day), while the Appliances tab is shown. */
  private async loadApplianceStats(): Promise<void> {
    const day = this._day;
    if (this.shownTab !== "appliances" || !day || !this._appliances) return;
    const ids = statisticIds(this._appliances.appliances);
    const tz = this.hass?.config?.time_zone ?? "UTC";
    const key = JSON.stringify([day, tz, ids]);
    if (this._applianceStats?.key === key) return;
    this._applianceStats = { key, energy: ids.length ? (this._applianceStats?.energy ?? null) : {} };
    if (!ids.length) return;
    let energy: DailyEnergy | null = null;
    try {
      energy = dailyEnergy(await this.ws<Record<string, StatRow[]>>(statsRequest(ids, day, tz)), tz);
    } catch {
      energy = null; // the cards show the day of the report only
    }
    if (this._applianceStats?.key === key) this._applianceStats = { key, energy };
  }

  // -- tabs (#47) ------------------------------------------------------------
  /** The tabs of this user; To review needs an add-on with a review (0.6.0+). */
  private get tabs(): Tab[] {
    return availableTabs(this.isAdmin, this._review !== null);
  }

  private get shownTab(): Tab {
    return shownTab(this._tab, this.tabs);
  }

  private writeTab(tab: Tab): void {
    try {
      const url = new URL(location.href);
      url.search = searchWithTab(url.search, tab);
      history.replaceState(history.state, "", url);
    } catch {
      // not in a browser page that allows it
    }
  }

  /** Show a tab; ``focus``: move the keyboard focus to it, ``toTop``: when
   * scrolled down, show the new tab from its top. */
  private async selectTab(tab: Tab, focus = false, toTop = true): Promise<void> {
    const header = this.renderRoot.querySelector(".header");
    const content = this.renderRoot.querySelector(".content");
    const scrolled =
      toTop && header && content && content.getBoundingClientRect().top < header.getBoundingClientRect().bottom - 1;
    this._tab = tab;
    this.writeTab(tab);
    await this.updateComplete;
    if (scrolled) this.renderRoot.firstElementChild?.scrollIntoView({ block: "start" });
    if (focus) this.renderRoot.querySelector<HTMLElement>(`#tab-${tab}`)?.focus();
  }

  private tabKeydown = (ev: KeyboardEvent) => {
    const next = tabForKey(this.tabs, this.shownTab, ev.key);
    if (!next) return;
    ev.preventDefault();
    void this.selectTab(next, true);
  };

  /** Wait, ignore or restore signatures (one, or the selection of the
   * queue), then show the next one of the queue. */
  private async decide(ids: string[], action: "wait" | "ignore" | "restore"): Promise<void> {
    if (!ids.length || this._reviewBusy) return;
    const queue = this.queue;
    const current = this.currentSig?.id ?? null;
    const done: string[] = [];
    this._reviewBusy = ids.length === 1 ? ids[0] : "*";
    this._reviewError = null;
    try {
      for (const id of ids) {
        await this.ws({ type: `energy_mapper/${action}`, signature_id: id });
        done.push(id);
      }
    } catch (err) {
      this._reviewError = this.errorText(err);
    } finally {
      if (done.length) {
        this._reviewChecked = this._reviewChecked.filter((id) => !done.includes(id));
        this.selectSignature(nextAfter(queue, current, done));
        await this.loadReview();
      }
      this._reviewBusy = null;
    }
  }

  /** Validate the signature with its form ("Validate and next"). */
  private async validate(sig: ReviewSignature): Promise<void> {
    if (this._reviewBusy) return;
    const form = this.formOf(sig);
    const built = validateMessage(
      sig,
      form.mode,
      form.name,
      form.entity,
      this.hass?.states?.[form.entity]?.attributes,
      form.appliance,
    );
    if ("missing" in built) {
      this._reviewForm = { ...form, missing: built.missing };
      await this.updateComplete;
      this.renderRoot.querySelector<HTMLElement>(built.missing === "entity" ? ".rq-form ha-entity-picker, .rq-form input[list]" : "#rq-input")?.focus();
      return;
    }
    const queue = this.queue;
    this._reviewBusy = sig.id;
    this._reviewError = null;
    try {
      await this.ws(built.msg);
      this._reviewForm = null;
      this._reviewChecked = this._reviewChecked.filter((id) => id !== sig.id);
      this.selectSignature(nextAfter(queue, sig.id, [sig.id]));
      this._notice =
        form.mode === "merge"
          ? this.t("review_merged", { name: this.applianceName(form.appliance) })
          : this.t("review_validated", { name: String(built.msg.name) });
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

  /** The name of an appliance of the review listing. */
  private applianceName(key: string): string {
    const fromListing = this._review?.appliances?.find((a) => a.key === key)?.name;
    const fromSuggestion = this._review?.signatures
      .flatMap((s) => s.suggestions ?? [])
      .find((c) => c.appliance?.key === key)?.appliance?.name;
    return fromListing ?? fromSuggestion ?? key;
  }

  /** Show one use of a signature on the chart: its day, zoomed on it. */
  private async showExample(example: SignatureExample): Promise<void> {
    if (this.shownTab !== "today") await this.selectTab("today", false, false);
    if (example.day !== this._day) await this.selectDay(example.day);
    await this.updateComplete;
    const report = this._report;
    if (!report || report.day !== example.day || !report.curve.length) return;
    // the chart is created by this update: let it render first
    requestAnimationFrame(() => {
      const [a, b] = exampleWindow(example, report.start, report.end);
      this.zoomTo(a / 1000, b / 1000);
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
    if (day !== this._day) this._window = null;
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
    const stack = stackSeries(report, this._registry, this.namer);
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
            : s.kind === "estimated"
              ? theme.palette[ESTIMATED_INDEX]
            : s.kind === "other"
              ? theme.other
              : theme.palette[indexes.get(s.id) ?? 0],
      );
      names.set(s.id, this.seriesName(s, t));
    }
    // the legend toggles the series by name: two alike provisional names must differ
    const distinct = distinctNames(names);
    const model: ChartModel = {
      report,
      stack,
      colors,
      names: distinct,
      info: this.info,
      theme,
      labels: { house: t("chart_house"), pv: t("chart_pv") },
    };
    this.modelCache = { report, registry: this._registry, key, model };
    return model;
  }

  private seriesName(s: Pick<StackSeries, "kind" | "name">, t: Translate): string {
    if (s.kind === "base" || s.kind === "submetered" || s.kind === "estimated" || s.kind === "other") return t(s.kind);
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
          ${this.hasTabs ? this.renderTabs(t) : nothing}
        </div>
        ${this.hasTabs
          ? html`<div class="content" role="tabpanel" id="tabpanel" aria-labelledby="tab-${this.shownTab}">
              ${this.renderContent(t)}
            </div>`
          : html`<div class="content">${this.renderContent(t)}</div>`}
      </div>
    `;
  }

  /** The tabs are shown once the add-on answers. */
  private get hasTabs(): boolean {
    return Boolean(this._status?.configured && this._status.available);
  }

  private renderTabs(t: Translate): TemplateResult {
    const shown = this.shownTab;
    const count = toReviewCount(this._review);
    const labels: Record<Tab, string> = {
      today: t("tab_today"),
      appliances: t("tab_appliances"),
      review: t("tab_review"),
      live: t("tab_live"),
    };
    return html`<div class="tabs" role="tablist" aria-label=${t("tabs")}>
      ${this.tabs.map(
        (tab) => html`<button
          type="button"
          role="tab"
          class="tab"
          id="tab-${tab}"
          aria-selected=${tab === shown ? "true" : "false"}
          aria-controls="tabpanel"
          tabindex=${tab === shown ? "0" : "-1"}
          @click=${() => void this.selectTab(tab)}
          @keydown=${this.tabKeydown}
        >
          <span class="tab-label">${labels[tab]}</span>
          ${tab === "review" && count > 0 ? html`<span class="tab-badge">${formatNumber(count, this.info)}</span>` : nothing}
          ${tab === "live" && shown === "live" && this._liveUp
            ? html`<span class="tab-live" role="img" aria-label=${t("live_dot")}></span>`
            : nothing}
        </button>`,
      )}
    </div>`;
  }

  /** Admin banner of Today: the signatures waiting for a name (#47). */
  private renderReviewBanner(t: Translate): TemplateResult | typeof nothing {
    const count = toReviewCount(this._review);
    if (!this.isAdmin || count === 0 || !this.tabs.includes("review")) return nothing;
    return html`<button type="button" class="review-banner" @click=${() => void this.selectTab("review", true)}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d=${mdiFormatListChecks}></path></svg>
      <span class="review-banner-text">
        ${count === 1 ? t("review_banner_one") : t("review_banner", { count: formatNumber(count, this.info) })}
      </span>
      <span class="review-banner-action">${t("review_start")}</span>
    </button>`;
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
    const tab = this.shownTab;
    return html`
      ${tab === "today" || tab === "appliances" ? this.renderDayBar(entries, t, tab === "today") : nothing}
      ${this._error ? this.alert("error", this._error, t("error")) : nothing}
      ${this._notice ? this.alert("success", this._notice) : nothing}
      ${tab === "today"
        ? this.renderToday(entries, t)
        : tab === "appliances"
          ? this.renderAppliances(t)
          : tab === "live"
            ? this.renderLive()
            : this.renderReview(t)}
    `;
  }

  /** Live (#17): its own element, which streams while it is shown. */
  private renderLive(): TemplateResult {
    return html`<energy-mapper-live
      .hass=${this.hass}
      ?narrow=${this.narrow || this._single}
      .registry=${this._registry}
      .appliances=${this._appliances?.appliances ?? null}
      @live-state=${(ev: CustomEvent<{ up: boolean }>) => (this._liveUp = ev.detail.up)}
      @live-labelled=${() => void this.afterLiveLabel()}
    ></energy-mapper-live>`;
  }

  /** A live label changed the appliances, the registry or the review. */
  private async afterLiveLabel(): Promise<void> {
    try {
      const [registry] = await Promise.all([
        this.ws<Registry>({ type: "energy_mapper/signatures" }),
        this.loadReview(),
        this.loadAppliances(),
      ]);
      this._registry = registry;
    } catch {
      // shown again at the next refresh
    }
  }

  /** Today: the selected day, for the whole household. */
  private renderToday(entries: DayEntry[], t: Translate): TemplateResult {
    return html`
      ${this.renderReviewBanner(t)}
      ${entries.length === 0 ? this.renderNoReport(t) : nothing}
      ${entries.length > 0 && this._day && !entries.some((e) => e.day === this._day)
        ? this.renderNoReportDay(this._day, entries, t)
        : nothing}
      ${this._report && this._report.day === this._day ? this.renderReport(this._report, t) : nothing}
    `;
  }

  /** Appliances (#54): one card per appliance, then every signature of the
   * registry and the switched loads, collapsed under "All signatures". */
  private renderAppliances(t: Translate): TemplateResult {
    return html`${this.renderApplianceCards(t)} ${this.renderSelfTest(t)} ${this.renderAllSignatures(t)}`;
  }

  /** Self-test (#15, every user): how often the curve finds each hidden
   * measured device again. */
  private renderSelfTest(t: Translate): TemplateResult | typeof nothing {
    const listing = this._selfTest;
    if (!listing || !showSelfTest(listing)) return nothing;
    const info = this.info;
    const rows = selfTestRows(listing, t, info, (e) => this.friendlyName(e) ?? null);
    return html`<section class="card self-test" aria-labelledby="self-test-title">
      <div class="card-header"><h2 id="self-test-title">${t("selftest_title")}</h2></div>
      <div class="card-hint">${selfTestHint(listing, t, info)}</div>
      <ul class="self-test-list">
        ${rows.map(
          (r) => html`<li class="self-test-row">
            <div class="self-test-main">
              <span class="self-test-name">${r.name}</span>
              ${r.rate != null
                ? html`<span class="self-test-rate tone-${r.tone}">${formatPercent(r.rate, info)}</span>`
                : nothing}
            </div>
            <div class="self-test-headline">
              ${r.headline}${r.trendText
                ? html` · <span class="self-test-trend trend-${r.trend}"
                      ><span aria-hidden="true">${r.trend === "up" ? "↗" : r.trend === "down" ? "↘" : "→"}</span>
                      ${r.trendText}</span
                    >`
                : nothing}
            </div>
            ${r.detail ? html`<div class="self-test-detail">${r.detail}</div>` : nothing}
          </li>`,
        )}
      </ul>
    </section>`;
  }

  private get applianceCardList(): ApplianceCard[] {
    const report = this._report && this._report.day === this._day ? this._report : null;
    return applianceCards(this._appliances?.appliances ?? [], {
      day: this._day,
      period: this.periodShown,
      report,
      rows: report ? usageRows(report, this._registry, this.namer) : [],
      registry: this._registry,
      stats: this._applianceStats?.energy ?? null,
    });
  }

  /** Periods of several days need the statistics; without them: the day. */
  private get periodShown(): Period {
    return this._applianceStats?.energy ? this._period : "day";
  }

  private trackingText(tracking: Tracking[], estimated: boolean, t: Translate): string {
    const label = (k: Tracking) => t(`tracked_${k}` as StringKey);
    const how =
      tracking.length > 1
        ? t("tracked_merged", { list: tracking.map((k) => label(k).toLocaleLowerCase(this.info.language)).join(" + ") })
        : tracking.length
          ? label(tracking[0])
          : "";
    return [how, estimated ? t("tracked_estimated") : t("tracked_measured")].filter(Boolean).join(" · ");
  }

  /** Go to the appliance's settings: the integration page, where its
   * subentry is reconfigured (Home Assistant's own navigation). */
  private openSettings = (ev: MouseEvent) => {
    if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    history.pushState(null, "", SETTINGS_PATH);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  };

  /** Select the appliance on the Today tab (its timeline row and list row). */
  private async showApplianceDay(card: ApplianceCard): Promise<void> {
    if (!card.series) return;
    this._appliance = card.series;
    this._category = null;
    await this.selectTab("today", false, true);
  }

  private renderApplianceCards(t: Translate): TemplateResult | typeof nothing {
    const listing = this._appliances;
    if (!listing) return nothing;
    const info = this.info;
    const cards = this.applianceCardList;
    const period = this.periodShown;
    const stats = this._applianceStats;
    const model = this._report && this._report.day === this._day && this._themeKey ? this.chartModel(this._report) : null;
    const missing = stats !== null && stats.energy === null && cards.some((c) => c.estimated);
    const chips = stats?.energy && cards.some((c) => c.bars.length);
    return html`<section class="card appliances" aria-labelledby="appliances-title">
      <div class="card-header">
        <h2 id="appliances-title">${t("appliances_title")}</h2>
        <div class="spacer"></div>
        ${chips
          ? html`<div class="view-toggle" role="group" aria-label=${t("period_label")}>
              ${PERIODS.map(
                (p) => html`<button
                  class="chip link view ${period === p ? "selected" : ""}"
                  aria-pressed=${period === p ? "true" : "false"}
                  @click=${() => (this._period = p)}
                >
                  ${t(`period_${p}` as StringKey)}
                </button>`,
              )}
            </div>`
          : nothing}
      </div>
      ${cards.length ? html`<div class="card-hint">${t("appliances_hint")}</div>` : nothing}
      ${missing ? html`<div class="card-hint">${t("appliances_stats_missing")}</div>` : nothing}
      ${this.isAdmin && this._teaching?.supported
        ? this._connectedForm?.key === null
          ? html`<div class="connected-new">${this.renderConnectedForm(t)}</div>`
          : html`<div class="connected-action">
              ${this.textButton(t("connected_action"), () => void this.openConnected(null), false, "plain", t("connected_action_hint"))}
            </div>`
        : nothing}
      ${cards.length
        ? html`<ul class="appliance-grid">
            ${cards.map((c) => this.renderApplianceCard(c, model, period, t, info))}
          </ul>`
        : html`<div class="message">${this.isAdmin ? t("appliances_none") : t("appliances_none_user")}</div>`}
    </section>`;
  }

  private renderApplianceCard(
    c: ApplianceCard,
    model: ChartModel | null,
    period: Period,
    t: Translate,
    info: LocaleInfo,
  ): TemplateResult {
    const color = (c.series && model?.colors.get(c.series)) || "var(--disabled-color, #bdbdbd)";
    const selected = c.series !== null && c.series === this._appliance;
    const facts: string[] = [];
    if (period === "day") {
      if (c.runs != null && c.runs > 0) {
        facts.push(c.runs === 1 ? t("usage_runs_one") : t("usage_runs", { count: formatNumber(c.runs, info) }));
      }
    } else if (c.used_days) {
      facts.push(
        t("appliance_used_days", { count: formatNumber(c.used_days, info), total: formatNumber(PERIOD_DAYS[period], info) }),
      );
    }
    const energy =
      c.energy_kwh == null
        ? html`<span class="appliance-energy none">${c.estimated ? t("appliance_no_data") : t("appliance_sensor")}</span>`
        : c.energy_kwh < 0.0005
          ? html`<span class="appliance-energy none"
              >${period === "day"
                ? t("appliance_not_used")
                : t("appliance_not_used_period", { count: formatNumber(PERIOD_DAYS[period], info) })}</span
            >`
          : html`<span class="appliance-energy">${formatEnergy(c.energy_kwh, info)}</span>`;
    const body = html`
      <span class="appliance-head">
        <span class="dot" style="background:${color}"></span>
        <span class="appliance-name">${c.name}</span>
      </span>
      <span class="appliance-tracking">${this.trackingText(c.tracking, c.estimated, t)}</span>
      <span class="appliance-figures">${energy}${facts.length ? html`<span class="appliance-facts">${facts.join(" · ")}</span>` : nothing}</span>
      ${c.bars.length ? this.renderApplianceBars(c, period, t, info) : nothing}
      ${c.last_use ? html`<span class="appliance-last">${t("appliance_last_use", { day: formatDay(c.last_use, info, "brief") })}</span>` : nothing}
    `;
    return html`<li class="appliance-card ${selected ? "selected" : ""}">
      ${c.series
        ? html`<button
            class="appliance-main"
            title=${t("appliance_show_day", { name: c.name })}
            @click=${() => void this.showApplianceDay(c)}
          >
            ${body}
          </button>`
        : html`<div class="appliance-main">${body}</div>`}
      ${this.isAdmin ? this.renderTakeover(c, t) : nothing} ${this.renderTeaching(c, t, info)}
      ${this.renderExampleLines(c, t, info)}
      ${this.isAdmin
        ? html`<div class="appliance-actions">
            ${this.teachAction(c)}
            <a
              class="appliance-settings"
              href=${SETTINGS_PATH}
              title=${t("appliance_settings_label", { name: c.name })}
              aria-label=${t("appliance_settings_label", { name: c.name })}
              @click=${this.openSettings}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d=${mdiCogOutline}></path></svg>
              <span>${t("appliance_settings")}</span>
            </a>
          </div>`
        : nothing}
    </li>`;
  }

  /** The Energy dashboard line of an appliance card (#13, administrators):
   * what it took over (Undo), or the device it probably is (Take over
   * tracking), and the confirmation step before anything changes. */
  private renderTakeover(c: ApplianceCard, t: Translate): TemplateResult | typeof nothing {
    const state = energyState(this._tracking, c.key);
    if (state.kind === "none") return nothing;
    const form = this._takeover?.key === c.key ? this._takeover : null;
    const open = (mode: "take" | "undo") =>
      (this._takeover = { key: c.key, mode, stat: mode === "take" ? preselected(this._tracking, c.key) : "", busy: false, error: null });
    const cancel = () => (this._takeover = null);
    let line: TemplateResult;
    if (state.kind === "taken_over") {
      line = html`<span>${t("takeover_replaces", { device: state.device })}</span>
        ${form ? nothing : this.textButton(t("takeover_undo"), () => open("undo"))}`;
    } else if (state.kind === "tracked") {
      line = html`<span>${t("takeover_tracked")}</span>`;
    } else {
      line = html`<span
          >${state.kind === "suggested" ? t("takeover_suggested", { device: state.device.name }) : t("takeover_available")}</span
        >
        ${form ? nothing : this.textButton(t("takeover_action"), () => open("take"))}`;
    }
    let confirm: TemplateResult | typeof nothing = nothing;
    if (form?.mode === "undo" && state.kind === "taken_over") {
      confirm = html`<div class="takeover-confirm" role="group" aria-label=${t("takeover_undo")}>
        <p>${t("takeover_undo_confirm", { device: state.device, name: c.name })}</p>
        ${form.error ? html`<p class="takeover-error" role="alert">${form.error}</p>` : nothing}
        <div class="takeover-buttons">
          ${this.textButton(t("takeover_cancel"), cancel, form.busy)}
          ${this.textButton(t("takeover_put_back"), () => void this.applyTakeover(c.name), form.busy, "accent")}
        </div>
      </div>`;
    } else if (form?.mode === "take") {
      const choices = pickList(this._tracking, c.key);
      const picked = choices.find((x) => x.device.stat_consumption === form.stat)?.device;
      const selectId = `takeover-${c.key}`;
      confirm = html`<div class="takeover-confirm" role="group" aria-label=${t("takeover_action")}>
        <label class="field" for=${selectId}>
          <span>${t("takeover_device")}</span>
          <select
            id=${selectId}
            class="text-input"
            .value=${form.stat}
            ?disabled=${form.busy}
            @change=${(ev: Event) => {
              if (this._takeover) this._takeover = { ...this._takeover, stat: (ev.target as HTMLSelectElement).value, error: null };
            }}
          >
            <option value="" ?selected=${!form.stat}></option>
            ${choices.map(
              (x) =>
                html`<option value=${x.device.stat_consumption} ?selected=${x.device.stat_consumption === form.stat}>
                  ${x.suggested ? t("takeover_suggestion", { name: x.device.name }) : x.device.name}
                </option>`,
            )}
          </select>
        </label>
        <p>${picked ? t("takeover_confirm", { device: picked.name, name: c.name }) : t("takeover_confirm_pick")}</p>
        ${form.error ? html`<p class="takeover-error" role="alert">${form.error}</p>` : nothing}
        <div class="takeover-buttons">
          ${this.textButton(t("takeover_cancel"), cancel, form.busy)}
          ${this.textButton(t("takeover_replace"), () => void this.applyTakeover(c.name), form.busy || !picked, "accent")}
        </div>
      </div>`;
    }
    return html`<div class="appliance-takeover">
      <div class="takeover-line">${line}</div>
      ${confirm}
    </div>`;
  }

  /** "Learn from a plug" next to Settings (administrators), while the card
   * is not learning and its form is closed. */
  private teachAction(c: ApplianceCard): TemplateResult | typeof nothing {
    const appliances = this._appliances?.appliances ?? [];
    const state = teachState(this._teaching, appliances.find((a) => a.key === c.key));
    if (state.kind !== "idle" || this._teach?.key === c.key || this._connectedForm?.key === c.key) return nothing;
    const t = this.t;
    // an appliance tracked by an operation state (a sensor) can learn from it (#79)
    const stateSource = appliances
      .find((a) => a.key === c.key)
      ?.sources.some((s) => s.type === "switched_load" && s.entity_id?.startsWith("sensor."));
    return html`${stateSource
      ? this.textButton(t("connected_action_card"), () => void this.openConnected(c.key), false, "plain", t("connected_action_hint"))
      : nothing}
    ${this.textButton(
      t("teach_action"),
      () => {
        const first = sensorChoices(this.hass?.states, appliances, c.key, this.flatSensors)[0];
        this._connectedForm = null;
        this._teach = { key: c.key, mode: "start", entity: first?.suggested ? first.id : "", busy: false, error: null };
      },
      false,
      "plain",
      t("teach_action_hint"),
    )}`;
  }

  /** The labelled examples under the step threshold (#100): seen by the
   * whole-house curve but not tracked yet, or too low for it. */
  private renderExampleLines(c: ApplianceCard, t: Translate, info: LocaleInfo): TemplateResult | typeof nothing {
    const appliance = this._appliances?.appliances.find((a) => a.key === c.key);
    const lines = exampleLines(appliance?.examples_status, t, info);
    if (!lines.length) return nothing;
    return html`<div class="appliance-takeover">
      <div class="teach-status">${lines.map((l) => html`<span class="teach-line">${l}</span>`)}</div>
    </div>`;
  }

  /** Learning from a measured device (#14): the status while a sensor
   * teaches the appliance (every user), and for administrators the actions
   * (learn from a plug, finish, stop) with their confirmation step. */
  private renderTeaching(c: ApplianceCard, t: Translate, info: LocaleInfo): TemplateResult | typeof nothing {
    const appliances = this._appliances?.appliances ?? [];
    const appliance = appliances.find((a) => a.key === c.key);
    const state = teachState(this._teaching, appliance);
    if (state.kind === "none" || (state.kind === "idle" && !this.isAdmin)) return nothing;
    const form = this._teach?.key === c.key ? this._teach : null;
    const cancel = () => (this._teach = null);
    if (state.kind === "idle" && this._connectedForm?.key === c.key) {
      return html`<div class="appliance-takeover appliance-teach">${this.renderConnectedForm(t)}</div>`;
    }
    if (state.kind === "idle") {
      if (!form) return nothing; // the action is next to Settings
      const choices = sensorChoices(this.hass?.states, appliances, c.key, this.flatSensors);
      const selectId = `teach-${c.key}`;
      const picked = choices.find((x) => x.id === form.entity);
      return html`<div class="appliance-takeover appliance-teach">
        <div class="takeover-confirm" role="group" aria-label=${t("teach_action")}>
          ${choices.length
            ? html`<label class="field" for=${selectId}>
                <span>${t("teach_sensor")}</span>
                <select
                  id=${selectId}
                  class="text-input"
                  .value=${form.entity}
                  ?disabled=${form.busy}
                  @change=${(ev: Event) => {
                    if (this._teach) this._teach = { ...this._teach, entity: (ev.target as HTMLSelectElement).value, error: null };
                  }}
                >
                  <option value="" ?selected=${!form.entity}></option>
                  ${choices.map(
                    (x) =>
                      html`<option value=${x.id} ?selected=${x.id === form.entity} ?disabled=${x.flat}>
                        ${x.flat
                          ? t("teach_flat", { name: x.name })
                          : x.suggested
                            ? t("teach_suggestion", { name: x.name })
                            : x.name}
                      </option>`,
                  )}
                </select>
              </label>`
            : html`<p>${t("teach_no_sensor")}</p>`}
          <p>${picked ? t("teach_start_confirm", { sensor: picked.name, name: c.name }) : t("teach_start_pick")}</p>
          ${form.error ? html`<p class="takeover-error" role="alert">${form.error}</p>` : nothing}
          <div class="takeover-buttons">
            ${this.textButton(t("takeover_cancel"), cancel, form.busy)}
            ${this.textButton(t("teach_start"), () => void this.applyTeach(c.name), form.busy || !picked, "accent")}
          </div>
        </div>
      </div>`;
    }
    const sensor = this.sensorName(state.teacher.entity_id);
    const since = state.teacher.since
      ? t("teach_since", { day: formatDay(dayOf(state.teacher.since * 1000, info.time_zone), info, "brief") })
      : "";
    const nameOf = (key: string) => appliances.find((a) => a.key === key)?.name ?? key;
    const lines = teachingLines(state, t, info, nameOf, (e) => this.sensorName(e));
    const connected = state.teacher.mode === "state";
    const open = (mode: "finish" | "stop") => (this._teach = { key: c.key, mode, entity: "", busy: false, error: null });
    let confirm: TemplateResult | typeof nothing = nothing;
    if (form && form.mode !== "start") {
      const finish = form.mode === "finish";
      confirm = html`<div class="takeover-confirm" role="group" aria-label=${finish ? t("teach_finish") : t("teach_stop")}>
        <p>
          ${finish
            ? t(connected ? "teach_finish_confirm_state" : "teach_finish_confirm", { name: c.name, sensor })
            : t(connected ? "teach_stop_confirm_state" : "teach_stop_confirm", { sensor })}
        </p>
        ${form.error ? html`<p class="takeover-error" role="alert">${form.error}</p>` : nothing}
        <div class="takeover-buttons">
          ${this.textButton(t("takeover_cancel"), cancel, form.busy)}
          ${this.textButton(finish ? t("teach_use") : t("teach_stop_button"), () => void this.applyTeach(c.name), form.busy, "accent")}
        </div>
      </div>`;
    }
    return html`<div class="appliance-takeover appliance-teach">
      <div class="teach-status">
        <span class="teach-title">${t("teach_learning", { sensor })}${since ? html` · ${since}` : nothing}</span>
        ${lines.map((l) => html`<span class="teach-line">${l}</span>`)}
      </div>
      ${this.isAdmin && !form
        ? html`<div class="takeover-buttons">
            ${this.textButton(t("teach_stop"), () => open("stop"))}
            ${this.textButton(t("teach_finish"), () => open("finish"), state.blocker !== null, "accent")}
          </div>`
        : nothing}
      ${confirm}
    </div>`;
  }

  /** The power sensors the shown day's analysis found flat (#79). */
  private get flatSensors(): Set<string> {
    return flatPowerSensors(this._report?.findings);
  }

  /** The form "Learn from a connected appliance" (#79): the device, then its
   * operation state and running states, phase state, energy counter and
   * programme as proposed, what the recorder does not keep, and the
   * confirmation. Nothing changes before *Start learning*. */
  private renderConnectedForm(t: Translate): TemplateResult {
    const form = this._connectedForm!;
    const listing = this._connected;
    const devices = deviceChoices(listing, form.key);
    const device = devices.find((d) => d.device_id === form.device);
    const set = (patch: Partial<ConnectedForm>) => {
      if (this._connectedForm) this._connectedForm = { ...this._connectedForm, ...patch, error: null };
    };
    const id = `connected-${form.key ?? "new"}`;
    const select = (
      label: string,
      value: string,
      items: ConnectedEntity[],
      onChange: (v: string) => void,
      optional: boolean,
      suffix: string,
    ) =>
      html`<label class="field" for="${id}-${suffix}">
        <span>${label}</span>
        <select
          id="${id}-${suffix}"
          class="text-input"
          .value=${value}
          ?disabled=${form.busy}
          @change=${(ev: Event) => onChange((ev.target as HTMLSelectElement).value)}
        >
          ${optional ? html`<option value="" ?selected=${!value}>${t("connected_none")}</option>` : nothing}
          ${items.map((e) => html`<option value=${e.entity_id} ?selected=${e.entity_id === value}>${e.name}</option>`)}
        </select>
      </label>`;
    const cancel = () => (this._connectedForm = null);
    let body: TemplateResult;
    if (form.busy && !listing) {
      body = html`<p>${t("loading")}</p>`;
    } else if (listing && !listing.supported) {
      body = html`<p>${t("connected_not_supported")}</p>`;
    } else if (!devices.length) {
      body = html`<p>${listing ? t("connected_no_device") : ""}</p>`;
    } else {
      const states = entitiesOf(device, "state");
      const options = stateOptions(device, form.entity);
      const notes = formNotes(form, device, this.flatSensors, t);
      body = html`<label class="field" for="${id}-device">
          <span>${t("connected_device")}</span>
          <select
            id="${id}-device"
            class="text-input"
            .value=${form.device}
            ?disabled=${form.busy}
            @change=${(ev: Event) => {
              const d = devices.find((x) => x.device_id === (ev.target as HTMLSelectElement).value);
              const keep = form.key === null ? "" : form.name;
              this._connectedForm = formFor(d, form.key, keep);
            }}
          >
            ${devices.map((d) => html`<option value=${d.device_id} ?selected=${d.device_id === form.device}>${d.name}</option>`)}
          </select>
        </label>
        ${form.key === null
          ? html`<label class="field" for="${id}-name">
              <span>${t("connected_name")}</span>
              <input
                id="${id}-name"
                class="text-input"
                .value=${form.name}
                maxlength="100"
                ?disabled=${form.busy}
                @input=${(ev: Event) => set({ name: (ev.target as HTMLInputElement).value })}
              />
            </label>`
          : nothing}
        ${select(t("connected_state"), form.entity, states, (v) => (this._connectedForm = withState(form, device, v)), false, "state")}
        ${options.length
          ? html`<fieldset class="connected-states" ?disabled=${form.busy}>
              <legend>${t("connected_on_states")}</legend>
              ${options.map(
                (o) => html`<label class="check">
                  <input
                    type="checkbox"
                    .checked=${form.on_states.includes(o)}
                    @change=${() => (this._connectedForm = toggleState(form, o))}
                  />
                  <span>${o}</span>
                </label>`,
              )}
            </fieldset>`
          : nothing}
        ${select(t("connected_phase"), form.phase, states.filter((e) => e.entity_id !== form.entity), (v) => set({ phase: v }), true, "phase")}
        ${select(t("connected_energy_entity"), form.energy, entitiesOf(device, "energy"), (v) => set({ energy: v }), true, "energy")}
        ${select(
          t("connected_program"),
          form.program,
          entitiesOf(device, "program", "state").filter((e) => e.entity_id !== form.entity && e.entity_id !== form.phase),
          (v) => set({ program: v }),
          true,
          "program",
        )}
        ${form.key === null
          ? html`<label class="check">
              <input type="checkbox" .checked=${form.track} ?disabled=${form.busy} @change=${() => set({ track: !form.track })} />
              <span>${t("connected_track")}</span>
            </label>`
          : nothing}
        ${notes.map((n) => html`<p class="card-hint">${n}</p>`)}
        <p>
          ${canStart({ ...form, busy: false })
            ? t("connected_confirm", {
                name: form.key === null ? form.name.trim() : (this._appliances?.appliances.find((a) => a.key === form.key)?.name ?? ""),
                state: this.sensorName(form.entity),
              })
            : t("connected_pick")}
        </p>`;
    }
    return html`<div class="takeover-confirm connected-form" role="group" aria-label=${t("connected_title")}>
      ${body}
      ${form.error ? html`<p class="takeover-error" role="alert">${form.error}</p>` : nothing}
      <div class="takeover-buttons">
        ${this.textButton(t("takeover_cancel"), cancel, form.busy && Boolean(listing))}
        ${this.textButton(t("teach_start"), () => void this.applyConnected(), !canStart(form) || !listing?.supported, "accent")}
      </div>
    </div>`;
  }

  /** The daily energy of the last 7 (or 30) days; the selected day stands out. */
  private renderApplianceBars(c: ApplianceCard, period: Period, t: Translate, info: LocaleInfo): TemplateResult {
    const label = t("appliance_bars", { count: formatNumber(c.bars.length, info) });
    const day = this._day;
    const summaryText = c.bars
      .map((b) => t("appliance_bar", { day: formatDay(b.day, info, "brief"), energy: formatEnergy(b.energy_kwh, info) }))
      .join(", ");
    return html`<span class="appliance-bars ${period === "month" ? "month" : ""}" role="img" aria-label="${label}: ${summaryText}">
      ${c.bars.map(
        (b) =>
          html`<span
            class="appliance-bar ${b.day === day ? "current" : ""} ${b.energy_kwh < 0.0005 ? "zero" : ""}"
            title=${t("appliance_bar", { day: formatDay(b.day, info, "brief"), energy: formatEnergy(b.energy_kwh, info) })}
            style="height:${Math.max(b.ratio * 100, 4).toFixed(1)}%"
          ></span>`,
      )}
    </span>`;
  }

  /** The registry of the signatures and the switched loads, collapsed. */
  private renderAllSignatures(t: Translate): TemplateResult {
    const registry = this.renderRegistry(t);
    const loads = this.renderSwitchedLoads(t);
    const count = this._registry?.signatures?.length ?? 0;
    if (registry === nothing && loads === nothing) {
      return this._appliances ? html`` : html`<div class="card"><div class="message">${t("registry_none")}</div></div>`;
    }
    // without the appliances (integration before 0.7.0): open, as before
    return html`<details class="card all-signatures" ?open=${!this._appliances}>
      <summary>${t("all_signatures", { count: formatNumber(count, this.info) })}</summary>
      ${registry} ${loads}
    </details>`;
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

  private renderDayBar(entries: DayEntry[], t: Translate, withAnalyze = true): TemplateResult | typeof nothing {
    if (!entries.length) return nothing;
    const day = this._day;
    const hasReport = entries.some((e) => e.day === day);
    const summaryOnly = entries.find((e) => e.day === day)?.summary;
    return html`<div class="daybar">
      ${this.renderDayPicker(entries, t)}
      ${summaryOnly ? html`<span class="badge neutral">${t("summary_only")}</span>` : nothing}
      <div class="spacer"></div>
      ${withAnalyze && this.isAdmin && day && hasReport
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
      ${this.renderSummary(report, t)} ${this.renderDayCard(report, model, t)}
      <div class="today-columns">
        ${this.renderUsage(report, model, t)}
        <div class="today-side">${this.renderPeaks(report, model, t)} ${this.renderFindings(report.findings ?? [], t)}</div>
      </div>
      ${this.renderQuality(report, t)}
    `;
  }

  private renderFindings(findings: Finding[], t: Translate): TemplateResult | typeof nothing {
    if (!findings.length) return nothing;
    const n = findings.length;
    return html`<details class="card findings">
      <summary>${n === 1 ? t("findings_count_one") : t("findings_count", { count: formatNumber(n, this.info) })}</summary>
      <ul>
        ${findings.map((f) => {
          const { title, text } = findingView(f, t, this.info, (id) => this.friendlyName(id));
          return html`<li><strong>${title}</strong> ${text}</li>`;
        })}
      </ul>
    </details>`;
  }

  /** The category of the split bar selected on the Today tab, for the
   * timeline to highlight (#49); null when none is. */
  get selectedCategory(): SplitKey | null {
    return this._category;
  }

  private selectCategory(key: SplitKey): void {
    this._category = this._category === key ? null : key;
    this._appliance = null;
  }

  /** The appliance selected in "What used energy" (the id of its chart
   * series: "known:<name>", "sig:<registry id>", "base"...), for the timeline
   * to highlight (#49); null when none is. */
  get selectedAppliance(): string | null {
    return this._appliance;
  }

  private selectAppliance(id: string): void {
    this._appliance = this._appliance === id ? null : id;
    this._category = null;
  }

  private clearSelection(): void {
    this._appliance = null;
    this._category = null;
  }

  /** Ids of the chart series of the selected appliance or category (same array while unchanged). */
  private highlight(model: ChartModel | null): string[] | null {
    if (!model) return null;
    const appliance = this._appliance && model.stack.some((s) => s.id === this._appliance) ? this._appliance : null;
    if (!appliance && !this._category) return null;
    const ids = appliance ? [appliance] : categorySeries(model.stack, this._category!, this._registry);
    const key = JSON.stringify([appliance, this._category, ids]);
    if (this.highlightCache?.key !== key) this.highlightCache = { key, ids };
    return this.highlightCache.ids;
  }

  private splitLabel(key: SplitKey, t: Translate): string {
    switch (key) {
      case "measured":
        return t("submetered");
      case "named":
        return t("split_named");
      case "to_name":
        return t("split_to_name");
      default:
        return t(key);
    }
  }

  private renderSummary(report: Report, t: Translate): TemplateResult {
    const s = summary(report);
    const info = this.info;
    const parts = splitParts(report, this._registry);
    const selected = this._category;
    const at = maxPowerTime(report);
    const strong = (text: string) => html`<strong>${text}</strong>`;
    // "Highest 3.8 kW at 19:12": the values in bold, the rest muted
    const fact = (template: string, values: Record<string, string>) =>
      template.split(/(\{\w+\})/).map((piece) => {
        const m = /^\{(\w+)\}$/.exec(piece);
        return m && values[m[1]] != null ? strong(values[m[1]]) : piece;
      });
    const exported = s.grid_export >= 0.05;
    return html`<section class="card summary" aria-labelledby="summary-title">
      <div class="summary-head">
        <div>
          <h2 id="summary-title" class="summary-label">${t("house")}</h2>
          <div class="summary-value">${formatEnergy(s.house, info)}</div>
        </div>
        <div class="summary-facts">
          ${report.max_house_w != null
            ? html`<span
                >${at != null
                  ? fact(t("peak_at"), { power: formatPower(report.max_house_w, info), time: formatTime(at, info) })
                  : fact(t("max_power"), { power: formatPower(report.max_house_w, info) })}</span
              >`
            : nothing}
          <span>${fact(t("grid_line"), { energy: formatEnergy(s.grid_import, info) })}</span>
          ${s.has_pv
            ? html`<span
                ><span class="dot" style="background:var(--energy-solar-color, #ff9800)"></span>${exported
                  ? fact(t("pv_line_export"), { energy: formatEnergy(s.pv, info), export: formatEnergy(s.grid_export, info) })
                  : fact(t("pv_line"), { energy: formatEnergy(s.pv, info) })}</span
              >`
            : nothing}
        </div>
      </div>
      ${parts.length
        ? html`<div class="split-bar ${selected ? "has-selection" : ""}" role="img" aria-label=${t("split_title")}>
              ${parts.map(
                (p) =>
                  html`<div
                    class="segment ${selected === p.key ? "selected" : ""}"
                    style="flex:${p.energy_kwh} 0 0;background:${splitColor(p.key)}"
                    title=${this.splitLabel(p.key, t)}
                    @click=${() => this.selectCategory(p.key)}
                  ></div>`,
              )}
            </div>
            <ul class="split-legend" aria-label=${t("split_hint")}>
              ${parts.map(
                (p) =>
                  html`<li>
                    <button
                      class="legend-item ${selected === p.key ? "selected" : ""}"
                      title=${p.key === "base" && s.base_w != null ? t("always_on", { power: formatPower(s.base_w, info) }) : ""}
                      aria-pressed=${selected === p.key ? "true" : "false"}
                      @click=${() => this.selectCategory(p.key)}
                    >
                      <span class="dot" style="background:${splitColor(p.key)}"></span>
                      <span class="legend-name">${this.splitLabel(p.key, t)}</span>
                      <span class="legend-values"
                        ><strong>${formatEnergy(p.energy_kwh, info)}</strong>
                        <span class="legend-share">${formatPercent(p.share, info)}</span></span
                      >
                    </button>
                  </li>`,
              )}
            </ul>`
        : nothing}
    </section>`;
  }

  private chartElement(): EnergyMapperChart | null {
    return this.renderRoot.querySelector("energy-mapper-chart");
  }

  /** The day card (#49): the timeline of the appliances (default) or the
   * stacked chart of the per-minute breakdown. */
  private renderDayCard(report: Report, model: ChartModel | null, t: Translate): TemplateResult {
    const hasCurve = report.curve.length > 0;
    const rows = timelineRows(report, this._registry, this.namer);
    const timeline = this._dayView === "timeline";
    const selection = this.timelineSelection(rows);
    const view = (key: "timeline" | "stacked", label: string) =>
      html`<button
        class="chip link view ${this._dayView === key ? "selected" : ""}"
        aria-pressed=${this._dayView === key ? "true" : "false"}
        @click=${() => this.setDayView(key)}
      >
        ${label}
      </button>`;
    let content: TemplateResult;
    if (timeline) {
      content =
        rows.length || hasCurve
          ? this.renderTimeline(report, rows, model, selection, t)
          : html`<div class="message">${t("timeline_none")}</div>`;
    } else {
      content = hasCurve
        ? html`<energy-mapper-chart
            .model=${model}
            .highlight=${this.highlight(model)}
            .narrow=${this.narrow}
          ></energy-mapper-chart>`
        : html`<div class="message">${report.curve_pruned ? t("curve_pruned") : t("no_curve")}</div>`;
    }
    return html`<section class="card day-card" aria-labelledby="day-title">
      <div class="card-header">
        <h2 id="day-title">${t("timeline_title")}</h2>
        <div class="spacer"></div>
        ${selection.any ? this.textButton(t("usage_show_all"), () => this.clearSelection()) : nothing}
        <div class="view-toggle" role="group" aria-label=${t("view_label")}>
          ${view("timeline", t("view_timeline"))} ${view("stacked", t("view_stacked"))}
        </div>
        ${!timeline && hasCurve ? this.iconButton(mdiArrowExpandHorizontal, t("reset_zoom"), () => this.resetZoom()) : nothing}
      </div>
      <div class="card-content">${content}</div>
    </section>`;
  }

  private setDayView(view: "timeline" | "stacked"): void {
    this._dayView = view;
    this._window = null;
  }

  /** The rows of the timeline to highlight: the selected appliance, or the
   * rows of the selected category of the split bar. ``any``: something is
   * selected; ``dim``: some row is highlighted (base load and unexplained
   * have no row: selecting them dims nothing). */
  private timelineSelection(rows: TimelineRow[]): TimelineSelection {
    const appliance = this._appliance;
    const category = this._category;
    const named = namedSignatureIds(this._registry);
    const on = (row: TimelineRow) =>
      row.id === appliance || (category !== null && seriesCategory(row, named) === category);
    return { any: appliance !== null || category !== null, dim: rows.some(on), on };
  }

  private renderTimeline(
    report: Report,
    rows: TimelineRow[],
    model: ChartModel | null,
    selection: TimelineSelection,
    t: Translate,
  ): TemplateResult {
    const info = this.info;
    const window: [number, number] = isWholeDay(this._window, report) ? [report.start, report.end] : this._window!;
    const [a, b] = window;
    const span = b - a;
    const zoomed = !isWholeDay(this._window, report);
    const curve = report.curve;
    const pct = (v: number) => `${(v * 100).toFixed(3)}%`;
    // the house strip: the highest power of each slice of the window
    const n = bucketCount(curve, window);
    const buckets = curve.length ? houseBuckets(curve, window, n) : [];
    const max = Math.max(0, ...buckets.map((v) => v ?? 0));
    const peaks = notablePeaks(report);
    const marks = peaks.filter((p) => p.end + 60 > a && p.start < b);
    const ticks = axisTicks(window, report.start, this.narrow ? 5 : 9);
    const dayStartLabel = formatTime(report.start, info);
    const tickLabel = (v: number) => {
      const label = formatTime(v, info);
      // the end of the day reads 24:00, not 00:00 again
      return v >= report.end && label === dayStartLabel && /^0?0[:.h]00$/.test(label) ? label.replace(/^0?0/, "24") : label;
    };
    // runs closer than about 2 px merge into one block
    const gap = span / 600;
    const spans = (row: TimelineRow) =>
      row.periods
        .slice(0, 12)
        .map((p) => timeSpan([p.start, p.end], info))
        .join(", ") + (row.periods.length > 12 ? "…" : "");
    const strip = curve.length
      ? html`<div class="tl-row tl-house">
          <span class="tl-name secondary">${t("chart_house")}</span>
          <div class="tl-strip" aria-hidden="true">
            <svg viewBox="0 0 ${n} 100" preserveAspectRatio="none">
              ${buckets.map((v, i) =>
                v === null || max <= 0
                  ? nothing
                  : svgBar(i, 100 - Math.max((v / max) * 100, 1.5), Math.max((v / max) * 100, 1.5)),
              )}
            </svg>
            ${marks.map((p) => {
              const left = Math.max(p.start, a);
              const right = Math.min(p.end + 60, b);
              return html`<span
                class="tl-mark"
                style="left:${pct((left - a) / span)};width:${pct(Math.max((right - left) / span, 0.004))}"
                title=${`${formatPower(p.house_w, info)} · ${timeSpan([p.start, p.end], info)}`}
                @click=${() => this.zoomOnPeak(report, p)}
              ></span>`;
            })}
          </div>
          <span class="tl-energy secondary">${max > 0 ? formatPower(max, info) : ""}</span>
        </div>`
      : nothing;
    return html`<div class="timeline ${selection.dim ? "has-selection" : ""}">
        ${strip}
        <ul class="tl-rows" aria-label=${t("timeline_hint")}>
          ${rows.map((row) => {
            const color = model?.colors.get(row.id) ?? "var(--disabled-color, #bdbdbd)";
            const name = model?.names.get(row.id) ?? this.seriesName(row, t);
            const on = selection.on(row);
            const detail = usageDetail(row, t, info);
            const blocks = visibleBlocks(row.periods, window, gap);
            return html`<li class="tl-row ${on ? "selected" : ""} ${row.unnamed ? "unnamed" : ""}" style="--c:${color}">
              <button
                class="tl-name"
                title=${detail}
                aria-pressed=${this._appliance === row.id ? "true" : "false"}
                @click=${() => this.selectAppliance(row.id)}
              >
                ${name}
              </button>
              <div
                class="tl-track"
                role="img"
                aria-label=${t("timeline_runs", { name, spans: spans(row) || "—" })}
                @click=${() => this.selectAppliance(row.id)}
              >
                ${blocks.map(
                  (k) =>
                    html`<span
                      class="tl-block ${k.cycling ? "cycling" : ""}"
                      style="left:${pct(k.left)};width:${pct(k.width)}"
                      title=${[
                        name,
                        timeSpan([k.start, k.end], info),
                        k.cycling ? t("timeline_cycling") : "",
                        k.power_w ? formatPower(k.power_w, info) : "",
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    ></span>`,
                )}
              </div>
              <span class="tl-energy">${formatEnergy(row.energy_kwh, info)}</span>
            </li>`;
          })}
        </ul>
        ${rows.length ? nothing : html`<div class="message">${t("timeline_none")}</div>`}
        ${!curve.length && report.curve_pruned ? html`<div class="secondary tl-pruned">${t("curve_pruned")}</div>` : nothing}
        <div class="tl-row tl-axis-row" aria-hidden="true">
          <span class="tl-name"></span>
          <div class="tl-axis">
            ${ticks.map((v) => {
              const x = (v - a) / span;
              const edge = x <= 0.001 ? "start" : x >= 0.999 ? "end" : "";
              return html`<span class="tl-tick ${edge}" style="left:${pct(x)}">${tickLabel(v)}</span>`;
            })}
          </div>
          <span class="tl-energy"></span>
        </div>
      </div>
      ${peaks.length || zoomed
        ? html`<div class="tl-peaks">
            ${peaks.length ? html`<span class="secondary">${t("timeline_peaks")}</span>` : nothing}
            ${peaks.map((p) => {
              const [za, zb] = peakZoom(p, report);
              const current = zoomed && za === a && zb === b;
              const what = [...p.parts]
                .sort((x, y) => y.power_w - x.power_w)
                .slice(0, 2)
                .map((part) => this.partLabel(part, t))
                .join(" + ");
              return html`<button
                class="chip link peak-chip ${current ? "selected" : ""}"
                aria-pressed=${current ? "true" : "false"}
                aria-label=${t("timeline_zoom_peak", { time: formatTime(p.max_ts, info) })}
                title=${t("timeline_zoom_peak", { time: formatTime(p.max_ts, info) })}
                @click=${() => (current ? this.resetZoom() : this.zoomOnPeak(report, p))}
              >
                <strong>${formatPower(p.house_w, info)}</strong> ${formatTime(p.max_ts, info)}${what ? ` · ${what}` : ""}
              </button>`;
            })}
            ${zoomed ? html`<button class="chip link" @click=${() => this.resetZoom()}>${t("reset_zoom")}</button>` : nothing}
          </div>`
        : nothing}`;
  }

  private partLabel(part: PeakPart, t: Translate): string {
    if (part.kind === "submetered" || part.kind === "estimated") return t(part.kind);
    if (part.kind === "signature" && this._report) {
      const name = peakPartName(part, this._report, this._registry, this.namer);
      return name === "?" ? t("unclassified") : name;
    }
    return part.name;
  }

  private partColor(part: PeakPart, model: ChartModel | null, report: Report): string | undefined {
    if (!model) return undefined;
    if (part.kind === "submetered" || part.kind === "estimated") return model.colors.get(part.kind);
    if (part.kind === "known") return model.colors.get(`known:${part.name}`);
    if (part.kind === "signature") return model.colors.get(`sig:${part.registry_id || registryIdOf(report, part.name)}`);
    return undefined;
  }

  private zoomOnPeak(report: Report, peak: Peak): void {
    const [a, b] = peakZoom(peak, report);
    this.zoomTo(a, b);
  }

  /** Show [a, b] (s) in the day card: the timeline's window, or the chart's zoom. */
  private zoomTo(a: number, b: number): void {
    if (this._dayView === "timeline") {
      this._window = [a, b];
      this.renderRoot.querySelector(".day-card")?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const chart = this.chartElement();
    if (!chart) return;
    chart.zoom(a * 1000, b * 1000);
    chart.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  private resetZoom(): void {
    this._window = null;
    this.chartElement()?.resetZoom();
  }

  private renderPeaks(report: Report, model: ChartModel | null, t: Translate): TemplateResult {
    const info = this.info;
    const peaks = report.peaks ?? [];
    const canZoom = report.curve.length > 0;
    return html`<section class="card" aria-labelledby="peaks-title">
      <div class="card-header"><h2 id="peaks-title">${t("peaks")}</h2></div>
      ${report.peak_threshold_w != null
        ? html`<div class="card-hint">${t("peak_threshold", { power: formatPower(report.peak_threshold_w, info) })}</div>`
        : nothing}
      ${peaks.length === 0
        ? html`<div class="message">${t("peaks_none")}</div>`
        : html`<ul class="peaks">
            ${peaks.map((p) => {
              const when = [timeSpan([p.start, p.end], info), p.notable ? t("peak_notable") : ""].filter(Boolean).join(" · ");
              const ran = p.parts.map((part) => {
                const c = this.partColor(part, model, report);
                return html`<span class="peak-part" title=${part.kind === "signature" ? part.registry_id || part.name : ""}
                  >${c ? html`<span class="dot" style="background:${c}"></span>` : nothing}${this.partLabel(part, t)}
                  <span class="secondary">${formatPower(part.power_w, info)}</span></span
                >`;
              });
              if (p.unexplained_w > 0) {
                ran.push(
                  html`<span class="peak-part secondary">${t("peak_unexplained", { power: formatPower(p.unexplained_w, info) })}</span>`,
                );
              }
              if (!ran.length) ran.push(html`<span class="peak-part secondary">${t("nothing_identified")}</span>`);
              return html`<li>
                <button
                  class="peak ${p.notable ? "notable" : ""}"
                  title=${canZoom ? t("show_on_chart") : ""}
                  @click=${() => canZoom && this.zoomOnPeak(report, p)}
                >
                  <div class="peak-head">
                    <span class="peak-power">${formatPower(p.house_w, info)}</span>
                    <span class="peak-time">${when}</span>
                  </div>
                  <div class="peak-parts">${ran}</div>
                </button>
              </li>`;
            })}
          </ul>`}
    </section>`;
  }

  /** "What used energy" (#50): one row per appliance of the day, by energy;
   * a row selects its series in the chart. */
  private renderUsage(report: Report, model: ChartModel | null, t: Translate): TemplateResult {
    const info = this.info;
    const rows = usageRows(report, this._registry, this.namer);
    const named = namedSignatureIds(this._registry);
    const selected = rows.some((r) => r.id === this._appliance) ? this._appliance : null;
    const category = this._category;
    const reviewable = (r: UsageRow) =>
      this.tabs.includes("review") && r.unnamed && r.registry_id !== null && r.registry_id !== "?";
    // the rows without a review button keep the bars aligned
    const space = rows.some(reviewable) ? html`<span class="usage-review-space"></span>` : nothing;
    return html`<section class="card usage" aria-labelledby="usage-title">
      <div class="card-header">
        <h2 id="usage-title">${t("usage_title")}</h2>
        <div class="spacer"></div>
        ${selected || category ? this.textButton(t("usage_show_all"), () => this.clearSelection()) : nothing}
      </div>
      ${rows.length
        ? html`<ul class="usage-list ${selected || category ? "has-selection" : ""}" aria-label=${t("usage_hint")}>
            ${rows.map((r) => {
              const color = model?.colors.get(r.id) ?? "var(--disabled-color, #bdbdbd)";
              const name = model?.names.get(r.id) ?? this.seriesName(r, t);
              const on = selected === r.id || (category !== null && seriesCategory(r, named) === category);
              const detail = usageDetail(r, t, info);
              return html`<li class="usage-item ${on ? "selected" : ""}">
                <button
                  class="usage-row"
                  aria-pressed=${selected === r.id ? "true" : "false"}
                  @click=${() => this.selectAppliance(r.id)}
                >
                  <span
                    class="dot ${r.unnamed ? "unnamed" : ""}"
                    style=${r.unnamed ? `border-color:${color}` : `background:${color}`}
                  ></span>
                  <span class="usage-text">
                    <span class="usage-name ${r.unnamed ? "unnamed" : ""}">${name}</span>
                    ${detail ? html`<span class="usage-detail">${detail}</span>` : nothing}
                  </span>
                  <span class="usage-bar" aria-hidden="true"
                    ><span style="width:${(r.ratio * 100).toFixed(1)}%;background:${color}"></span
                  ></span>
                  <span class="usage-energy">${formatEnergy(r.energy_kwh, info)}</span>
                </button>
                ${reviewable(r)
                  ? html`<button
                      class="usage-review"
                      title=${t("usage_review_label", { name })}
                      aria-label=${t("usage_review_label", { name })}
                      @click=${() => void this.openReview(r.registry_id!)}
                    >
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d=${mdiFormatListChecks}></path></svg>
                    </button>`
                  : space}
              </li>`;
            })}
          </ul>`
        : html`<div class="message">${t("no_curve")}</div>`}
    </section>`;
  }

  /** To review, showing one signature (in the filter that lists it). */
  private async openReview(registryId: string): Promise<void> {
    const sig = this._review?.signatures.find((x) => x.id === registryId);
    const filter = filterOf(sig);
    if (filter) {
      if (filter !== this._reviewFilter) this.setFilter(filter);
      this.selectSignature(registryId);
    }
    await this.selectTab("review", false, true);
    if (filter) this.renderRoot.querySelector<HTMLElement>("#rq-title")?.focus({ preventScroll: true });
  }

  private renderSwitchedLoads(t: Translate): TemplateResult | typeof nothing {
    const loads = this._registry?.switched_loads ?? [];
    const info = this.info;
    const today = new Map((this._report?.known_loads ?? []).map((k) => [k.entity_id, k]));
    if (!loads.length && !today.size) return nothing;
    const rows = loads.length
      ? loads.map((l) => ({ name: l.name, entity_id: l.entity_id, power_w: l.power_w, inverted: l.inverted, days: l.days_seen }))
      : [...today.values()].map((k) => ({ name: k.name, entity_id: k.entity_id, power_w: k.power_w, inverted: k.inverted, days: null }));
    return html`<div class="sub-section">
      <h3 class="sub-header">${t("switched_loads")}</h3>
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
    return html`<div class="sub-section">
      <h3 class="sub-header">${t("registry")}</h3>
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
      (s.kind === "cycling" || s.kind === "cold") && s.on_s && s.period_s
        ? t("cycle", { on: formatDuration(s.on_s, info), period: formatDuration(s.period_s, info) })
        : s.kind === "multiphase"
          ? phasesSummary(s.phases, t, info)
          : "";
    const hoursTitle = s.hours
      .map((v, h) => (v > 0 ? `${h}h` : ""))
      .filter(Boolean)
      .join(" ");
    return {
      name: html`<div class="sig-name">
          ${c ? html`<span class="dot" style="background:${c}"></span>` : nothing} ${registryName(s, this.namer)}
          ${seen.has(s.id) ? html`<span class="badge neutral">${t("seen_this_day")}</span>` : nothing}
        </div>
        <div class="secondary sig-id">${s.id}</div>`,
      kind:
        s.kind === "cycling" ? t("cycling") : s.kind === "multiphase" ? t("multiphase") : s.kind === "cold" ? t("cold") : t("steady"),
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

  // -- review of the signatures: one at a time (#10, #51) --------------------
  /** The queue of the filter shown (memoised on the listing). */
  private get queue(): ReviewSignature[] {
    const c = this.queueCache;
    if (c && c.listing === this._review && c.filter === this._reviewFilter) return c.queue;
    const queue = reviewQueue(this._review, this._reviewFilter);
    this.queueCache = { listing: this._review, filter: this._reviewFilter, queue };
    return queue;
  }

  /** The signature shown in detail. */
  private get currentSig(): ReviewSignature | undefined {
    const id = currentOf(this.queue, this._reviewCurrent);
    return this.queue.find((s) => s.id === id);
  }

  /** The validation form of a signature (a blank one until it is edited). */
  private formOf(s: Pick<ReviewSignature, "id">): ReviewForm {
    return this._reviewForm?.id === s.id ? this._reviewForm : blankForm(s.id);
  }

  private selectSignature(id: string | null): void {
    this._reviewCurrent = id;
    if (id && this._reviewForm?.id !== id) this._reviewForm = null;
  }

  private step(delta: number): void {
    const id = stepFrom(this.queue, this.currentSig?.id ?? null, delta);
    if (id) this.selectSignature(id);
  }

  private setFilter(filter: ReviewFilter): void {
    this._reviewFilter = filter;
    this._reviewChecked = [];
    this._queueAll = false;
  }

  private reviewKeydown = (ev: KeyboardEvent) => {
    if (ev.defaultPrevented || typingTarget(ev.composedPath())) return;
    const action = shortcutFor(ev);
    const sig = this.currentSig;
    if (!action || !sig) return;
    // also keeps Home Assistant's own one-letter shortcuts out of the way
    ev.preventDefault();
    ev.stopPropagation();
    if (this._reviewBusy) return;
    switch (action) {
      case "prev":
        return this.step(-1);
      case "next":
        return this.step(1);
      case "validate":
        return void this.validate(sig);
      case "wait":
        if (sig.review_state === "proposed") void this.decide([sig.id], "wait");
        return;
      case "ignore":
        if (sig.review_state !== "ignored") void this.decide([sig.id], "ignore");
        return;
    }
  };

  /** The house curves of the days of the examples shown, loaded a moment
   * after a signature is shown (stepping through the queue loads nothing). */
  private loadExampleCurves(): void {
    if (this.shownTab !== "review") return;
    const sig = this.currentSig;
    if (!sig) return;
    const available = new Set(dayEntries(this._days).map((e) => e.day));
    const days = [...new Set((sig.examples ?? []).slice(0, 3).map((e) => e.day))].filter(
      (d) => available.has(d) && !(d in this._exampleCurves) && !this.curveRequests.has(d),
    );
    if (!days.length) return;
    clearTimeout(this.curveTimer);
    this.curveTimer = setTimeout(() => {
      if (this.currentSig?.id !== sig.id) return;
      for (const day of days) void this.loadCurve(day);
    }, 300);
  }

  private async loadCurve(day: string): Promise<void> {
    if (this.curveRequests.has(day)) return;
    this.curveRequests.add(day);
    let curve: ExampleCurve | null = null;
    try {
      const report =
        this._report?.day === day ? this._report : await this.ws<Report>({ type: "energy_mapper/report", day });
      curve = { start: report.start, end: report.end, points: report.curve.map((p) => ({ t: p.t, house: p.house })) };
    } catch {
      curve = null;
    } finally {
      this.curveRequests.delete(day);
    }
    const kept = Object.entries(this._exampleCurves).slice(-(MAX_EXAMPLE_CURVES - 1));
    this._exampleCurves = { ...Object.fromEntries(kept), [day]: curve };
  }

  private renderReview(t: Translate): TemplateResult | typeof nothing {
    const listing = this._review;
    if (!listing || !this.isAdmin) return nothing;
    const queue = this.queue;
    const sig = this.currentSig;
    return html`<div class="review" id="review" @keydown=${this.reviewKeydown}>
      ${this.renderProgress(listing, t)}
      ${this._reviewError ? this.alert("error", this._reviewError) : nothing}
      <div class="rq-layout">
        <div class="rq-grid">
          ${this.renderQueue(queue, sig, t)}
          <section class="card rq-detail" aria-labelledby="rq-title">
            ${queue.length ? this.renderStepper(queue, sig, t) : nothing}
            ${sig ? this.renderDetail(sig, queue, t) : this.renderEmptyQueue(listing, t)}
          </section>
        </div>
      </div>
    </div>`;
  }

  /** Named / to review / waiting / ignored, the share explained and the filters. */
  private renderProgress(listing: ReviewListing, t: Translate): TemplateResult {
    const info = this.info;
    const p = reviewProgress(listing, this._registry);
    const counts = filterCounts(listing);
    const n = (v: number) => formatNumber(v, info);
    const share = p.share ?? 0;
    return html`<section class="card rq-progress">
      <div class="rq-progress-head">
        <span>
          <strong>${p.named === 1 ? t("rq_named_one") : t("rq_named", { count: n(p.named) })}</strong> ·
          ${t("rq_to_review", { count: n(p.toReview) })} · ${t("rq_waiting", { count: n(p.waiting) })} ·
          ${p.ignored === 1 ? t("rq_ignored_one") : t("rq_ignored", { count: n(p.ignored) })}
        </span>
        ${p.share !== null
          ? html`<span class="secondary">
              ${t(p.basis === "energy" ? "rq_share_energy" : "rq_share_count", { share: formatPercent(share, info) })}
            </span>`
          : nothing}
      </div>
      <div
        class="rq-bar"
        role="progressbar"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow=${Math.round(share * 100)}
        aria-label=${t(p.basis === "energy" ? "rq_share_energy" : "rq_share_count", { share: formatPercent(share, info) })}
      >
        <div style="width:${(share * 100).toFixed(1)}%"></div>
      </div>
      <div class="rq-filters" role="group" aria-label=${t("rq_filters")}>
        ${REVIEW_FILTERS.map((f) => {
          const label =
            f === "energy" ? t("rq_filter_energy") : t(`rq_filter_${f}` as StringKey, { count: n(counts[f]) });
          const on = this._reviewFilter === f;
          return html`<button type="button" class="filter-chip ${on ? "selected" : ""}" aria-pressed=${on ? "true" : "false"} @click=${() => this.setFilter(f)}>
            ${label}
          </button>`;
        })}
      </div>
      ${listing.suggest?.unrecorded_domains?.length
        ? html`<div class="secondary">${t("review_unrecorded", { domains: listing.suggest.unrecorded_domains.join(", ") })}</div>`
        : nothing}
    </section>`;
  }

  /** The short detail of a queue item: uses per week and a hint. */
  private queueDetail(s: ReviewSignature, t: Translate): string {
    const parts: string[] = [];
    if (s.per_week != null) parts.push(t("rq_per_week", { count: formatNumber(s.per_week, this.info, s.per_week < 10 ? 1 : 0) }));
    const best = bestSuggestion(s);
    if (best) parts.push(best.appliance ? t("rq_known") : t("rq_suggestion"));
    if (s.review_state === "waiting") {
      const back = this.waitText(s, t);
      if (back) parts.push(back);
    }
    return parts.join(" · ");
  }

  private renderQueue(queue: ReviewSignature[], sig: ReviewSignature | undefined, t: Translate): TemplateResult {
    const info = this.info;
    const checked = new Set(this._reviewChecked.filter((id) => queue.some((s) => s.id === id)));
    const busy = this._reviewBusy !== null;
    const index = queue.findIndex((s) => s.id === sig?.id);
    const limit = this._queueAll ? queue.length : Math.max(QUEUE_ROWS, index + 1);
    const shown = queue.slice(0, limit);
    const ids = [...checked];
    const filter = this._reviewFilter;
    return html`<section class="card rq-queue" aria-label=${t("rq_queue")}>
      <div class="rq-queue-head">
        <span class="rq-queue-title">
          ${checked.size === 0
            ? t("rq_queue")
            : checked.size === 1
              ? t("rq_selected_one")
              : t("rq_selected", { count: formatNumber(checked.size, info) })}
        </span>
        ${checked.size
          ? html`${filter === "waiting" || filter === "ignored"
                ? this.textButton(t("review_restore"), () => void this.decide(ids, "restore"), busy)
                : this.textButton(t("review_wait"), () => void this.decide(ids, "wait"), busy, "plain", t("review_wait_hint"))}
              ${filter !== "ignored"
                ? this.textButton(t("review_ignore"), () => void this.decide(ids, "ignore"), busy, "plain", t("review_ignore_hint"))
                : nothing}`
          : nothing}
      </div>
      ${queue.length
        ? html`<ol class="rq-list">
            ${shown.map((s) => {
              const name = registryName(s, this.namer);
              const current = s.id === sig?.id;
              return html`<li class=${current ? "current" : ""}>
                <input
                  type="checkbox"
                  .checked=${checked.has(s.id)}
                  aria-label=${t("rq_select", { name })}
                  @change=${(ev: Event) => {
                    const on = (ev.target as HTMLInputElement).checked;
                    this._reviewChecked = on ? [...checked, s.id] : [...checked].filter((id) => id !== s.id);
                  }}
                />
                <button type="button" class="rq-row" aria-current=${current ? "true" : "false"} title=${name} @click=${() => this.selectSignature(s.id)}>
                  <span class="rq-row-name">${name}</span>
                  <span class="secondary">${this.queueDetail(s, t)}</span>
                </button>
                <span class="rq-row-energy">${formatEnergy(s.energy_kwh, info)}</span>
              </li>`;
            })}
          </ol>`
        : html`<div class="rq-queue-empty secondary">${t("rq_empty")}</div>`}
      ${shown.length < queue.length
        ? html`<div class="rq-queue-more">
            ${this.textButton(t("rq_more", { count: formatNumber(queue.length - shown.length, info) }), () => (this._queueAll = true))}
          </div>`
        : nothing}
    </section>`;
  }

  /** Phones: "1 of 34" with previous and next, instead of the queue. */
  private renderStepper(queue: ReviewSignature[], sig: ReviewSignature | undefined, t: Translate): TemplateResult {
    const info = this.info;
    const i = queue.findIndex((s) => s.id === sig?.id);
    return html`<div class="rq-stepper">
      ${this.iconButton(mdiChevronLeft, t("rq_prev"), () => this.step(-1), i <= 0)}
      <span class="rq-stepper-pos">${t("rq_step", { pos: formatNumber(i + 1, info), count: formatNumber(queue.length, info) })}</span>
      ${this.iconButton(mdiChevronRight, t("rq_next"), () => this.step(1), i < 0 || i >= queue.length - 1)}
    </div>`;
  }

  private renderEmptyQueue(listing: ReviewListing, t: Translate): TemplateResult {
    const c = listing.criteria;
    const info = this.info;
    return html`<div class="message">
      ${this._reviewFilter === "energy"
        ? t("review_none", { uses: formatNumber(c.min_uses, info), days: formatNumber(c.min_days, info) })
        : t("rq_empty")}
    </div>`;
  }

  private renderDetail(s: ReviewSignature, queue: ReviewSignature[], t: Translate): TemplateResult {
    const info = this.info;
    const i = queue.findIndex((q) => q.id === s.id);
    const note = s.review_state === "waiting" ? this.waitText(s, t) : "";
    return html`<div class="rq-head">
        <div class="rq-head-main">
          <div class="secondary">
            <span class="rq-pos">${t("rq_position", { pos: formatNumber(i + 1, info), count: formatNumber(queue.length, info) })}</span
            >${note ? html`<span class="rq-note">${note}</span>` : nothing}
          </div>
          <h2 id="rq-title" tabindex="-1" title=${s.id}>${registryName(s, this.namer)}</h2>
        </div>
        <span class="rq-nav">
          ${this.iconButton(mdiChevronLeft, t("rq_prev"), () => this.step(-1), i <= 0)}
          ${this.iconButton(mdiChevronRight, t("rq_next"), () => this.step(1), i >= queue.length - 1)}
        </span>
      </div>
      ${this.renderFacts(s, t)} ${s.occurrences ? this.renderRaster(s, t) : this.renderHistogram(s, t)}
      ${this.renderExamples(s, t)} ${this.renderSuggestion(s, t)}
      ${this.renderValidateForm(s, t)}`;
  }

  private renderFacts(s: ReviewSignature, t: Translate): TemplateResult {
    const info = this.info;
    const facts: Array<[string, string, string?]> = [[t("rq_power"), formatPower(s.power_w, info)]];
    const cold = s.kind === "cold";
    if ((s.kind === "cycling" || cold) && s.on_s && s.period_s) {
      facts.push([t("rq_cycle"), t("cycle", { on: formatDuration(s.on_s, info), period: formatDuration(s.period_s, info) })]);
    }
    if (s.duration_s && !cold) facts.push([t("rq_duration"), formatDuration(s.duration_s, info)]);
    if (s.kind === "multiphase" && s.cycle_kwh) facts.push([t("rq_cycle_energy"), formatEnergy(s.cycle_kwh, info)]);
    if (s.per_week != null) {
      facts.push([t("rq_frequency"), t("rq_times_week", { count: formatNumber(s.per_week, info, s.per_week < 10 ? 1 : 0) })]);
    }
    facts.push([
      t("rq_energy"),
      formatEnergy(s.energy_kwh, info),
      `${t("rq_energy_hint")} · ${t("review_uses", { count: formatNumber(s.count, info), days: formatNumber(s.days_seen, info) })}`,
    ]);
    if (cold && s.estimated_kwh && s.estimated_kwh > 0) {
      // the part that is an estimate, not a detection (#89)
      facts.push([t("rq_estimated"), formatEnergy(s.estimated_kwh, info), t("rq_estimated_hint")]);
    }
    if (s.first_seen) facts.push([t("rq_first_seen"), formatDay(s.first_seen, info, "short")]);
    const phases = s.kind === "multiphase" && s.phases?.length ? phasesSummary(s.phases, t, info) : "";
    return html`<dl class="rq-facts">
      ${facts.map(([label, value, title]) => html`<div title=${title ?? nothing}><dt>${label}</dt><dd>${value}</dd></div>`)}
      ${phases ? html`<div class="rq-phases"><dt>${t("rq_phases")}</dt><dd>${phases}</dd></div>` : nothing}
    </dl>`;
  }

  /** The usual hours: a 24-hour histogram. */
  private renderHistogram(s: ReviewSignature, t: Translate): TemplateResult | typeof nothing {
    const profile = hourProfile(s.hours);
    if (!profile.some((v) => v > 0)) return nothing;
    const hours = formatHours(usualHours(s.hours));
    return html`<div>
      <div class="secondary rq-label">${t("rq_usual_hours")}</div>
      <div class="rq-hist" role="img" aria-label=${hours ? t("review_hours", { hours }) : t("rq_usual_hours")} title=${formatHours(usualHours(s.hours, 0.01, 24))}>
        ${profile.map((v) => html`<div class=${v >= 0.5 ? "peak" : v > 0 ? "" : "zero"} style="height:${Math.max(v * 100, 4)}%"></div>`)}
      </div>
      <div class="rq-hist-axis secondary"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>24 h</span></div>
    </div>`;
  }

  /** The day raster (#65): one row per day, the most recent on top, each use
   * a block (width: its duration, shade: its power), under a thin strip of
   * the usual hours. A click, or Enter, opens the day zoomed on the use; the
   * arrow keys move between the uses. Needs the add-on 0.9.0+ (otherwise the
   * histogram is shown). */
  private renderRaster(s: ReviewSignature, t: Translate): TemplateResult {
    const info = this.info;
    const tz = info.time_zone;
    const rows = rasterRows(s.occurrences, tz, {
      lastDay: rasterLastDay(Date.now(), tz),
      firstDay: s.first_seen,
    });
    const available = new Set(dayEntries(this._days).map((e) => e.day));
    const remembered = this.rasterPos?.sig === s.id ? this.rasterPos.pos : null;
    const focusPos =
      remembered && rows[remembered[0]]?.blocks[remembered[1]] ? remembered : firstBlock(rows);
    const profile = hourProfile(s.hours);
    const hours = formatHours(usualHours(s.hours));
    const pct = (v: number) => `${(v * 100).toFixed(3)}%`;
    const block = (day: string, b: RasterBlock, r: number, i: number) => {
      const label = t("rq_raster_use", {
        day: formatDay(day, info, "brief"),
        from: formatTime(b.from, info),
        to: formatTime(b.to, info),
        duration: formatDuration(b.to - b.from, info),
        power: formatPower(b.power_w, info),
      });
      const open = available.has(day);
      const focused = focusPos?.[0] === r && focusPos?.[1] === i;
      return html`<button
        type="button"
        class="rq-block ${b.cut_before ? "cut-before" : ""} ${b.cut_after ? "cut-after" : ""}"
        data-pos="${r}:${i}"
        tabindex=${focused ? 0 : -1}
        style="left:${pct(b.x)};width:${pct(b.w)};opacity:${(0.45 + 0.55 * b.shade).toFixed(2)}"
        title=${open ? `${label}\n${t("show_on_chart")}` : label}
        aria-label=${label}
        aria-disabled=${open ? "false" : "true"}
        @focus=${() => (this.rasterPos = { sig: s.id, pos: [r, i] })}
        @click=${() => {
          if (open) void this.showExample({ day, start: b.start, end: b.end, power_w: b.power_w });
        }}
      ></button>`;
    };
    return html`<div class="rq-raster-wrap">
      <div class="secondary rq-label">${t("rq_raster")}</div>
      <div class="rq-raster" role="group" aria-label=${t("rq_raster_hint")} @keydown=${(ev: KeyboardEvent) => this.rasterKeydown(ev, s.id, rows)}>
        <div class="rq-raster-row rq-raster-head">
          <span></span>
          <div class="rq-strip" role="img" aria-label=${hours ? t("review_hours", { hours }) : t("rq_usual_hours")} title=${formatHours(usualHours(s.hours, 0.01, 24))}>
            ${profile.map((v) => html`<div class=${v > 0 ? "" : "zero"} style=${v > 0 ? `opacity:${(0.2 + 0.8 * v).toFixed(2)}` : ""}></div>`)}
          </div>
        </div>
        <div class="rq-raster-row rq-raster-axis secondary" aria-hidden="true">
          <span></span>
          <div class="rq-axis-track">
            ${rasterTicks(false).map(
              (h) => html`<span class="${h % 6 ? "minor" : ""} ${h === 0 ? "first" : h === 24 ? "last" : ""}" style="left:${pct(h / 24)}">${h} h</span>`,
            )}
          </div>
        </div>
        <div class="rq-raster-body">
          ${rows.map(
            (row, r) => html`<div class="rq-raster-row ${row.weekend ? "weekend" : ""}">
              <span class="rq-day secondary">${rasterDayLabel(row.day, info.language)}</span>
              <div class="rq-track">
                ${rasterTicks(true).slice(1, -1).map((h) => html`<i class="rq-hour-line" style="left:${pct(h / 24)}"></i>`)}
                ${row.blocks.map((b, i) => block(row.day, b, r, i))}
              </div>
            </div>`,
          )}
        </div>
      </div>
      ${s.occurrences?.length ? nothing : html`<div class="secondary small">${t("rq_raster_empty")}</div>`}
    </div>`;
  }

  /** Arrow keys, Home and End in the raster: move the focus between uses
   * (and keep the review shortcuts for the signatures out of the way). */
  private rasterKeydown(ev: KeyboardEvent, sig: string, rows: ReturnType<typeof rasterRows>): void {
    const target = (ev.target as HTMLElement | null)?.closest?.(".rq-block") as HTMLElement | null;
    if (!target || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const [r, b] = (target.dataset.pos ?? "").split(":").map(Number);
    const next = moveBlock(rows, [r, b], ev.key);
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(ev.key)) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (!next) return;
    const raster = target.closest(".rq-raster");
    const el = raster?.querySelector<HTMLElement>(`.rq-block[data-pos="${next[0]}:${next[1]}"]`);
    if (!el) return;
    target.tabIndex = -1;
    el.tabIndex = 0;
    this.rasterPos = { sig, pos: next };
    el.focus();
  }

  /** Up to three uses drawn on the house curve; a click opens the day, zoomed. */
  private renderExamples(s: ReviewSignature, t: Translate): TemplateResult | typeof nothing {
    const examples = (s.examples ?? []).slice(0, 3);
    if (!examples.length) return nothing;
    const info = this.info;
    const days = new Set(dayEntries(this._days).map((e) => e.day));
    return html`<div>
      <div class="secondary rq-label">${t("rq_examples")}</div>
      <div class="rq-examples">
        ${examples.map((ex) => {
          const curve = this._exampleCurves[ex.day];
          const drawing = curve ? exampleDrawing(curve.points, ex, curve.start, curve.end, 200, 56) : null;
          const label = html`<span class="rq-example-label">
            <strong>${formatDay(ex.day, info, "brief")} ${formatTime(ex.start, info)}</strong>
            <span class="secondary">${formatDuration(ex.end - ex.start, info)}</span>
          </span>`;
          const picture = drawing
            ? html`<svg class="rq-example-curve" viewBox="0 0 200 56" preserveAspectRatio="none" aria-hidden="true">
                <rect class="use" x=${drawing.from} y="0" width=${drawing.to - drawing.from} height="56"></rect>
                <path class="area" d=${drawing.area}></path>
                <path class="line" d=${drawing.path}></path>
              </svg>`
            : html`<span class="rq-example-curve empty secondary small">${days.has(ex.day) && curve === undefined ? "…" : t("rq_no_curve")}</span>`;
          return days.has(ex.day)
            ? html`<button type="button" class="rq-example" title=${t("show_on_chart")} @click=${() => void this.showExample(ex)}>
                ${picture}${label}
              </button>`
            : html`<div class="rq-example">${picture}${label}</div>`;
        })}
      </div>
    </div>`;
  }

  /** The best entity suggested (#41, #43), with "Use" and, when it belongs
   * to an appliance, "Merge". */
  private renderSuggestion(s: ReviewSignature, t: Translate): TemplateResult | typeof nothing {
    const best = bestSuggestion(s);
    if (!best) return nothing;
    const info = this.info;
    const busy = this._reviewBusy !== null;
    const why = drivesLoad(best)
      ? t("review_probably_hint", {
          matches: formatNumber(best.matches, info),
          steps: formatNumber(best.steps ?? 0, info),
          days: formatNumber(best.days, info),
        })
      : t("review_contained_hint", {
          states: best.on_states.join(", "),
          matches: formatNumber(best.matches, info),
          sessions: formatNumber(best.sessions ?? best.matches, info),
          days: formatNumber(best.days, info),
          share: formatPercent(best.time_share ?? 0, info),
        });
    const friendly = (e: string) => this.friendlyName(e);
    const name = suggestionLabel(best, friendly);
    const score = formatPercent(best.score, info);
    const ident = best.attribute ? `${best.entity_id} · ${best.attribute}` : best.entity_id;
    const merge = formForMerge(s.id, best);
    return html`<div class="rq-suggestion">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d=${mdiLightbulbOutline}></path></svg>
      <div class="rq-suggestion-text" title=${ident}>
        <strong>
          ${best.appliance
            ? t("review_probably_part", { appliance: best.appliance.name, name, score })
            : t("review_probably", { name, score })}
        </strong>
        <span class="secondary">${why}</span>
      </div>
      <span class="rq-suggestion-actions">
        ${this.textButton(t("rq_use"), () => (this._reviewForm = formForUse(s.id, best, friendly)), busy, "plain", t("rq_use_hint"))}
        ${merge
          ? this.textButton(t("rq_merge"), () => (this._reviewForm = merge), busy, "plain", t("rq_merge_hint", { name: best.appliance?.name ?? "" }))
          : nothing}
      </span>
    </div>`;
  }

  private waitText(s: ReviewSignature, t: Translate): string {
    const r = s.review;
    if (!r?.until_count || !r.until) return "";
    const day = dayOf(r.until * 1000, this.info.time_zone);
    return t("review_back", { uses: formatNumber(r.until_count, this.info), day: formatDay(day, this.info, "short") });
  }

  /** The friendly name of an entity, if Home Assistant knows it. */
  private friendlyName(entityId: string): string | undefined {
    const name = this.hass?.states?.[entityId]?.attributes?.friendly_name;
    return typeof name === "string" ? name : undefined;
  }

  /** The other suggested entities in the form: one click links one. */
  private renderSuggested(s: ReviewSignature, form: ReviewForm, t: Translate): TemplateResult | typeof nothing {
    if ((s.suggestions?.length ?? 0) < 2) return nothing;
    const info = this.info;
    const friendly = (e: string) => this.friendlyName(e);
    return html`<div class="chips">
      <span class="secondary">${t("review_suggested")}</span>
      ${s.suggestions!.map((c) => {
        const picked = formForSuggestion(s.id, c, friendly);
        const selected =
          form.mode === picked.mode &&
          (picked.mode === "merge"
            ? form.appliance === picked.appliance
            : picked.mode === "switch"
              ? form.entity === picked.entity
              : form.name === picked.name);
        const label = suggestionLabel(c, friendly);
        return html`<button
          type="button"
          class="chip link ${selected ? "selected" : ""}"
          aria-pressed=${selected ? "true" : "false"}
          title=${c.appliance ? `${c.entity_id} → ${c.appliance.name}` : c.entity_id}
          @click=${() => {
            if (picked.mode === "switch") {
              this.setForm(s, { mode: "switch" });
              this.pickEntity(s, c.entity_id);
            } else {
              this.setForm(s, { mode: picked.mode, appliance: picked.appliance, name: picked.name || form.name, entity: "" });
            }
          }}
        >
          ${c.appliance ? `${c.appliance.name} · ${label}` : label}<span class="chip-value">${formatPercent(c.score, info)}</span>
        </button>`;
      })}
    </div>`;
  }

  private setForm(s: Pick<ReviewSignature, "id">, patch: Partial<ReviewForm>): void {
    this._reviewForm = { ...this.formOf(s), ...patch, missing: undefined };
  }

  private pickEntity(s: Pick<ReviewSignature, "id">, entity: string): void {
    const form = this.formOf(s);
    const name = this.friendlyName(entity);
    // the name follows the entity until the user types one
    const typed = form.name.trim() && form.name !== this.friendlyName(form.entity);
    this.setForm(s, { entity, name: typed || name === undefined ? form.name : name });
  }

  private renderEntityPicker(s: ReviewSignature, form: ReviewForm, t: Translate): TemplateResult {
    if (defined("ha-entity-picker")) {
      return html`<ha-entity-picker
        .hass=${this.hass}
        .value=${form.entity}
        .label=${t("review_entity")}
        .includeDomains=${linkDomains(form.mode)}
        .includeDeviceClasses=${form.mode === "sensor" ? ["power"] : undefined}
        .entityFilter=${(st: { entity_id: string; attributes?: Record<string, unknown> }) =>
          linkable(form.mode, st.entity_id, st.attributes)}
        @value-changed=${(ev: CustomEvent<{ value?: string }>) => this.pickEntity(s, ev.detail?.value ?? "")}
      ></ha-entity-picker>`;
    }
    const listId = `em-entities-${form.mode}`;
    return html`<label class="field">
      <span>${t("review_entity")}</span>
      <input
        class="text-input"
        list=${listId}
        .value=${form.entity}
        @change=${(ev: Event) => this.pickEntity(s, (ev.target as HTMLInputElement).value.trim())}
      />
      <datalist id=${listId}>
        ${entityChoices(this.hass?.states, form.mode).map((e) => html`<option value=${e.id}>${e.name}</option>`)}
      </datalist>
    </label>`;
  }

  /** The validation form, inline: name (or appliance), how to track it,
   * "Validate and next", Wait, Ignore (Restore for the waiting and the
   * ignored), and the keyboard shortcuts. */
  private renderValidateForm(s: ReviewSignature, t: Translate): TemplateResult {
    const form = this.formOf(s);
    const busy = this._reviewBusy !== null;
    const appliances = this._review?.appliances ?? [];
    const modes: LinkMode[] = ["new", "switch", "sensor"];
    if (appliances.length || form.mode === "merge") modes.push("merge");
    const keys = shortcutLetters(this.hass?.locale?.language || this.hass?.language);
    const kbd = (k: string) => html`<kbd>${k}</kbd>`;
    return html`<form
      class="rq-form"
      @submit=${(ev: Event) => {
        ev.preventDefault();
        void this.validate(s);
      }}
    >
      ${form.mode === "merge"
        ? html`<label class="field">
            <span>${t("review_appliance")}</span>
            <select
              id="rq-input"
              class="text-input"
              .value=${form.appliance}
              @change=${(ev: Event) => this.setForm(s, { appliance: (ev.target as HTMLSelectElement).value })}
            >
              <option value="" ?selected=${!form.appliance}></option>
              ${appliances.map((a) => html`<option value=${a.key} ?selected=${a.key === form.appliance}>${a.name}</option>`)}
            </select>
          </label>`
        : html`<label class="field">
            <span>${t("rq_name")}</span>
            <input
              id="rq-input"
              class="text-input"
              maxlength="100"
              placeholder=${t("rq_name_placeholder")}
              .value=${form.name}
              @input=${(ev: Event) => this.setForm(s, { name: (ev.target as HTMLInputElement).value })}
              @keydown=${(ev: KeyboardEvent) => {
                if (ev.key === "Enter") {
                  ev.preventDefault();
                  void this.validate(s);
                }
              }}
            />
          </label>`}
      <div class="rq-how">
        <span class="secondary" id="rq-how-label">${t("rq_how")}</span>
        <div class="seg" role="group" aria-labelledby="rq-how-label">
          ${modes.map(
            (m) => html`<button
              type="button"
              class=${form.mode === m ? "selected" : ""}
              aria-pressed=${form.mode === m ? "true" : "false"}
              @click=${() => this.setForm(s, { mode: m, entity: "" })}
            >
              ${t(`rq_track_${m}` as StringKey)}
            </button>`,
          )}
        </div>
        <span class="secondary">${t(`review_mode_${form.mode}_hint` as StringKey)}</span>
      </div>
      ${form.mode === "switch" || form.mode === "sensor" ? this.renderEntityPicker(s, form, t) : nothing}
      ${this.renderSuggested(s, form, t)}
      ${form.missing
        ? html`<div class="field-error" role="alert">
            ${form.missing === "name"
              ? t("review_missing_name")
              : form.missing === "appliance"
                ? t("review_missing_appliance")
                : t("review_missing_entity")}
          </div>`
        : nothing}
      <div class="rq-actions">
        ${this.textButton(t("rq_validate_next"), () => void this.validate(s), busy, "accent")}
        ${s.review_state === "proposed"
          ? this.textButton(t("review_wait"), () => void this.decide([s.id], "wait"), busy, "plain", t("review_wait_hint"))
          : this.textButton(t("review_restore"), () => void this.decide([s.id], "restore"), busy)}
        ${s.review_state !== "ignored"
          ? this.textButton(t("review_ignore"), () => void this.decide([s.id], "ignore"), busy, "plain", t("review_ignore_hint"))
          : nothing}
        <span class="rq-keys secondary small" aria-label=${t("rq_keys")}>
          ${kbd(keys.validate)} ${t("rq_key_validate")} · ${kbd(keys.wait)} ${t("rq_key_wait")} · ${kbd(keys.ignore)}
          ${t("rq_key_ignore")} · ${kbd("←")} ${kbd("→")} ${t("rq_key_navigate")}
        </span>
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
