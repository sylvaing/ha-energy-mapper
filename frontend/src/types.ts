// Shapes of the websocket answers (see energy_mapper/DOCS.md) and the part of
// the Home Assistant frontend objects the panel uses.

export interface HassLocale {
  language: string;
  number_format?: string; // language | system | comma_decimal | decimal_comma | space_comma | none
  time_format?: string; // language | system | 12 | 24
  time_zone?: string; // local | server
}

export interface HomeAssistant {
  language: string;
  locale: HassLocale;
  config: { time_zone: string };
  user?: { is_admin: boolean; name?: string };
  themes?: { darkMode?: boolean; theme?: string };
  states?: Record<string, { entity_id: string; state?: string; attributes?: Record<string, unknown> }>;
  callWS<T>(msg: Record<string, unknown>): Promise<T>;
}

export interface Status {
  configured: boolean;
  state: string | null;
  url: string | null;
  available: boolean;
  health: {
    status: string;
    version: string;
    last_run: number | null;
    days: number;
    signatures: number;
    storage_bytes: number;
  } | null;
  error: string | null;
}

export interface Days {
  days: string[];
  summary_days: string[];
}

export interface EnergyKwh {
  house: number;
  grid_import: number;
  grid_export: number;
  pv: number;
  submetered: number;
  /** part of `submetered` that is estimated (absent in older reports) */
  estimated?: number;
  base: number;
  other: number;
  base_w?: number;
  known: Record<string, number>;
  signatures: Record<string, number>;
}

export interface CurvePoint {
  t: number;
  house: number;
  grid: number;
  pv: number;
  submetered: number;
  estimated?: number;
  base: number;
  other: number;
  known: Record<string, number>;
  signatures: Record<string, number>;
}

export interface PeakPart {
  kind: "submetered" | "estimated" | "known" | "signature" | string;
  name: string;
  power_w: number;
  since?: number;
  registry_id?: string | null;
}

export interface Peak {
  start: number;
  end: number;
  max_ts: number;
  house_w: number;
  grid_w: number;
  pv_w: number;
  parts: PeakPart[];
  unexplained_w: number;
  notable?: boolean;
}

export interface DailySignature {
  id: string;
  kind: string;
  band: string;
  power_w: number;
  count: number;
  duration_s: number;
  energy_kwh: number;
  registry_id?: string | null;
}

export interface KnownLoad {
  name: string;
  entity_id: string;
  power_w: number;
  transitions: number;
  matched: number;
  inverted: boolean;
  on_s: number;
}

export interface Finding {
  kind: string;
  entity_id: string;
  detail: string;
}

export interface Report {
  day: string;
  start: number;
  end: number;
  time_zone?: string;
  energy_kwh: EnergyKwh;
  max_house_w: number | null;
  peak_threshold_w: number | null;
  curve: CurvePoint[];
  curve_pruned?: boolean;
  peaks: Peak[];
  signatures: DailySignature[];
  known_loads: KnownLoad[];
  findings?: Finding[];
  config?: { pv_entity?: string | null; grid_entity?: string | null };
  quality?: { samples: number; max_gap_s: number | null; pv_unknown_s?: number; compute_s?: number };
}

export interface RegistrySignature {
  id: string;
  kind: "steady" | "cycling" | string;
  band: string;
  label: string | null;
  power_w: number;
  duration_s: number;
  on_s: number | null;
  period_s: number | null;
  duty: number | null;
  weight: number;
  count: number;
  days_seen: number;
  per_week: number;
  first_seen: string;
  last_seen: string;
  hours: number[];
  energy_kwh: number;
  weighted_energy_kwh?: number;
  examples?: SignatureExample[];
  appliance?: string | null;
}

export interface SignatureExample {
  day: string;
  start: number;
  end: number;
  power_w: number;
}

export type ReviewState = "proposed" | "waiting" | "ignored" | "validated" | "immature";

/** An entity whose state changes coincide with a signature's steps (#41),
 * or whose state contains its uses (#43). */
export interface EntitySuggestion {
  entity_id: string;
  /** an attribute read as the state (climate: hvac_action), add-on 0.8.0+ */
  attribute?: string;
  /** "edge": changes at the steps (a ground truth); "containment": the
   * signature only runs in those states (add-on 0.8.0+; absent: edge) */
  match?: "edge" | "containment";
  /** edge: share of the signature's steps matched; containment: share of
   * its on-time inside the on states (0..1) */
  score: number;
  on_share?: number | null;
  off_share?: number | null;
  /** edge: share of the entity's changes near a step */
  explained?: number;
  /** edge: steps matched; containment: sessions inside */
  matches: number;
  steps?: number;
  /** containment: the signature's sessions, the entity's time in the on
   * states, the share of its on periods with a use, the chance level */
  sessions?: number;
  time_share?: number;
  used?: number;
  chance?: number;
  days: number;
  /** the states in which the appliance runs */
  on_states: string[];
  inverted: boolean;
  /** the appliance the entity already belongs to: merge into it (#43) */
  appliance?: { key: string; name: string; via: "entity" | "device" };
}

/** An appliance a signature can be merged into (energy_mapper/review). */
export interface ApplianceRef {
  key: string;
  name: string;
}

export interface ReviewSignature extends RegistrySignature {
  review_state: ReviewState;
  /** the best entities first (add-on 0.7.0+) */
  suggestions?: EntitySuggestion[];
  /** the decision recorded by the add-on */
  review?: {
    state: "waiting" | "ignored" | "validated";
    at: number;
    count?: number;
    until_count?: number;
    until?: number;
    appliance?: string;
  };
}

/** energy_mapper/review */
export interface ReviewListing {
  criteria: { min_uses: number; min_days: number; wait_days: number; wait_factor: number };
  now: number;
  counts: Record<string, number>;
  signatures: ReviewSignature[];
  repair?: boolean;
  /** the appliances (integration 0.6.0+) */
  appliances?: ApplianceRef[];
  /** the entity suggestions of the nightly run (add-on 0.7.0+) */
  suggest?: {
    computed_at: number | null;
    days: number;
    candidates: number;
    attribute_candidates?: number;
    min_score: number | null;
    /** candidate domains none of whose entities is recorded */
    unrecorded_domains: string[];
    unrecorded_entities: number;
  };
}

export interface RegistrySwitchedLoad {
  entity_id: string;
  name: string;
  power_w: number;
  inverted: boolean;
  days_seen: number;
  first_seen?: string;
  last_seen?: string;
}

export interface Registry {
  signatures: RegistrySignature[];
  switched_loads: RegistrySwitchedLoad[];
  peak: { threshold_w: number; reference_w: number; window_days: number } | null;
}
