// The date picker of the Home Assistant frontend (History and Energy panels).
//
// `ha-date-range-nav` (HA 2026.11+: a pill with previous/next buttons) and
// `ha-date-range-picker` (earlier versions) are internal elements, loaded
// lazily with the History panel. When neither is defined yet, the History
// panel's chunk is loaded through the route table of `partial-panel-resolver`
// (the element of HA that loads every panel), exactly as HA does when the
// user opens History. Nothing of HA is imported or bundled: if this does not
// work any more, the panel falls back to a native date input.

export type HaPickerTag = "ha-date-range-nav" | "ha-date-range-picker";

const TAGS: HaPickerTag[] = ["ha-date-range-nav", "ha-date-range-picker"];
const ROUTE = "energy-mapper-date-picker";

let loading: Promise<HaPickerTag | null> | null = null;

export function definedPicker(): HaPickerTag | null {
  return TAGS.find((tag) => customElements.get(tag)) ?? null;
}

interface RouteTable {
  routes?: Record<string, { load?: () => Promise<unknown> }>;
}

type Resolver = HTMLElement & {
  getRoutes?: (panels: unknown) => RouteTable;
  _getRoutes?: (panels: unknown) => RouteTable;
};

async function loadHistoryChunk(): Promise<void> {
  if (!customElements.get("partial-panel-resolver")) return; // not inside HA
  const resolver = document.createElement("partial-panel-resolver") as Resolver;
  // public up to 2025, private (same behaviour) since
  const getRoutes = resolver.getRoutes ?? resolver._getRoutes;
  if (typeof getRoutes !== "function") return;
  const table = getRoutes.call(resolver, { [ROUTE]: { component_name: "history", url_path: ROUTE } });
  await table?.routes?.[ROUTE]?.load?.();
}

/** The tag of HA's date picker once it is defined, or null after ``timeoutMs``
 * (outside Home Assistant, or when the loading technique stops working). */
export function loadHaDatePicker(timeoutMs = 10000): Promise<HaPickerTag | null> {
  loading ??= (async () => {
    const found = definedPicker();
    if (found) return found;
    try {
      await loadHistoryChunk();
    } catch {
      // fall through: maybe defined another way, else the timeout
    }
    const defined = Promise.any(TAGS.map((tag) => customElements.whenDefined(tag).then(() => definedPicker())));
    const timeout = new Promise<null>((resolve) => setTimeout(() => resolve(null), timeoutMs));
    return Promise.race([defined, timeout]);
  })();
  return loading;
}

type Calendar = HTMLElement & { min?: string; max?: string; value?: string; tentative?: string };

const adapted = new WeakSet<Element>();

/** One day at a time: the first click on a day completes the range (start =
 * end) instead of waiting for a second click, then "Select" applies it. */
function selectSingleDay(this: Calendar, ev: Event): void {
  const detail = (ev as CustomEvent<unknown>).detail;
  const day =
    this.tentative || (detail instanceof Date && !isNaN(detail.getTime()) ? detail.toISOString().slice(0, 10) : "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
  this.value = `${day}/${day}`;
  this.tentative = "";
  // what calendar-range dispatches when a range is complete: the picker reads `value`
  this.dispatchEvent(new Event("change", { bubbles: true }));
}

/** Adapt the calendar of an opened picker: limit it to [min, max]
 * ("YYYY-MM-DD") and select one day per click. The picker has no such
 * options; the `calendar-range` element (cally) it renders does. Best effort:
 * does nothing if the inner structure of the picker changed. */
export function adaptCalendar(picker: Element, min: string, max: string): void {
  let frames = 0;
  const attempt = () => {
    const inner = picker.shadowRoot?.querySelector("date-range-picker");
    const calendar = inner?.shadowRoot?.querySelector("calendar-range") as Calendar | null | undefined;
    if (calendar) {
      if (calendar.min !== min) calendar.min = min;
      if (calendar.max !== max) calendar.max = max;
      if (!adapted.has(calendar)) {
        adapted.add(calendar);
        calendar.addEventListener("rangestart", selectSingleDay);
      }
      return;
    }
    if (++frames < 120) requestAnimationFrame(attempt);
  };
  requestAnimationFrame(attempt);
}
