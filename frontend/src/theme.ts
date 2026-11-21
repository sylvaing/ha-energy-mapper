// Colours of the Home Assistant theme, read from its CSS variables.

/** Chart palette of the HA frontend (fallback of --graph-color-1..30). */
export const HA_PALETTE = [
  "#4269d0",
  "#f4bd4a",
  "#ff725c",
  "#5cc96a",
  "#ff8ab7",
  "#9c6b4e",
  "#97bbf5",
  "#01ab63",
  "#9498a0",
  "#094bad",
  "#c99000",
  "#d84f3e",
  "#49a28f",
  "#048732",
  "#d96895",
  "#8043ce",
  "#7599d1",
  "#7a4c31",
  "#74787a",
  "#6989f4",
  "#ffd444",
  "#ff957c",
  "#8fe9d3",
  "#62cc71",
  "#ffadda",
  "#c884ff",
  "#badeff",
  "#bf8b6d",
  "#b6b7bb",
  "#6a6a6a",
];

/** Palette indexes of the fixed series. */
export const BASE_INDEX = 8; // grey
export const SUBMETERED_INDEX = 0; // blue
export const ESTIMATED_INDEX = 6; // light blue
const GREYS = [8, 18, 28, 29];

/** Palette indexes given to the appliances: all but the fixed series and the greys. */
export const SERIES_INDEXES = HA_PALETTE.map((_, i) => i).filter(
  (i) => i !== SUBMETERED_INDEX && i !== ESTIMATED_INDEX && !GREYS.includes(i),
);

export function cssVar(el: Element, name: string, fallback: string): string {
  const value = getComputedStyle(el).getPropertyValue(name).trim();
  return value || fallback;
}

export function palette(el: Element): string[] {
  return HA_PALETTE.map((c, i) => cssVar(el, `--graph-color-${i + 1}`, c));
}

export interface ChartTheme {
  palette: string[];
  text: string;
  secondaryText: string;
  divider: string;
  card: string;
  primary: string;
  error: string;
  house: string;
  pv: string;
  other: string;
  fontFamily: string;
}

export function chartTheme(el: Element): ChartTheme {
  return {
    palette: palette(el),
    text: cssVar(el, "--primary-text-color", "#212121"),
    secondaryText: cssVar(el, "--secondary-text-color", "#727272"),
    divider: cssVar(el, "--divider-color", "rgba(0, 0, 0, 0.12)"),
    card: cssVar(el, "--card-background-color", "#ffffff"),
    primary: cssVar(el, "--primary-color", "#03a9f4"),
    error: cssVar(el, "--error-color", "#db4437"),
    house: cssVar(el, "--primary-text-color", "#212121"),
    pv: cssVar(el, "--energy-solar-color", "#ff9800"),
    other: cssVar(el, "--disabled-color", "#bdbdbd"),
    fontFamily: cssVar(el, "--ha-font-family-body", "Roboto, Noto, sans-serif"),
  };
}
