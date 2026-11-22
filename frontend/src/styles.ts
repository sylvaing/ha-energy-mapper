// Styles of the panel: only Home Assistant theme variables (with the default
// theme's values as fallbacks), so light/dark themes and the app follow.

import { css } from "lit";

export const panelStyles = css`
  :host {
    display: block;
    min-height: 100%;
    background: var(--primary-background-color, #fafafa);
    color: var(--primary-text-color, #212121);
    font-family: var(--ha-font-family-body, Roboto, Noto, sans-serif);
    -webkit-font-smoothing: var(--ha-font-smoothing, antialiased);
    /* phones (360 px and up): long names and entity ids break instead of
       widening the page */
    overflow-wrap: anywhere;
  }

  /* toolbar, as in the HA panels */
  .header {
    position: sticky;
    top: 0;
    z-index: 4;
    background-color: var(--app-header-background-color, var(--primary-color, #03a9f4));
    color: var(--app-header-text-color, #fff);
    border-bottom: var(--app-header-border-bottom, none);
    padding-top: env(safe-area-inset-top);
  }
  .toolbar {
    height: var(--header-height, 56px);
    display: flex;
    align-items: center;
    padding: 0 var(--ha-space-3, 12px);
    box-sizing: border-box;
    font-size: var(--ha-font-size-xl, 20px);
    font-weight: var(--ha-font-weight-normal, 400);
  }
  .main-title {
    margin-inline-start: var(--ha-space-6, 24px);
    line-height: var(--ha-line-height-normal, 1.6);
    flex-grow: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .narrow .main-title,
  .menu .main-title {
    margin-inline-start: var(--ha-space-2, 8px);
  }

  /* tabs (#47), as HA's ha-tab: under the toolbar, the active one underlined;
     on phones they share the width */
  .tabs {
    display: flex;
    gap: var(--ha-space-1, 4px);
    padding: 0 var(--ha-space-4, 16px);
    overflow-x: auto;
    scrollbar-width: none;
  }
  .tab {
    position: relative;
    height: 48px;
    min-width: 0;
    padding: 0 var(--ha-space-4, 16px);
    border: none;
    background: none;
    color: inherit;
    opacity: 0.8;
    font: inherit;
    font-size: var(--ha-font-size-m, 14px);
    font-weight: var(--ha-font-weight-medium, 500);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--ha-space-2, 8px);
    white-space: nowrap;
    overflow-wrap: normal;
  }
  .tab::after {
    content: "";
    position: absolute;
    left: var(--ha-space-2, 8px);
    right: var(--ha-space-2, 8px);
    bottom: 0;
    height: 2px;
    border-radius: 2px 2px 0 0;
  }
  .tab:hover {
    opacity: 1;
  }
  .tab[aria-selected="true"] {
    opacity: 1;
    color: var(--app-header-selection-bar-color, var(--primary-color, #03a9f4));
  }
  .tab[aria-selected="true"]::after {
    background: currentColor;
  }
  .tab:focus-visible {
    outline: 2px solid currentColor;
    outline-offset: -4px;
    border-radius: var(--ha-border-radius-md, 8px);
  }
  .tab-label {
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tab-badge {
    flex: none;
    min-width: 20px;
    height: 20px;
    padding: 0 6px;
    box-sizing: border-box;
    border-radius: var(--ha-border-radius-pill, 9999px);
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    font-size: var(--ha-font-size-s, 12px);
    line-height: 20px;
    text-align: center;
  }
  /* the pulsing dot of the Live tab (#17) while it streams */
  @keyframes tab-live-pulse {
    50% {
      opacity: 0.35;
    }
  }
  .tab-live {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--error-color, #db4437);
    animation: tab-live-pulse 1.4s infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .tab-live {
      animation: none;
    }
  }
  .narrow .tabs {
    padding: 0;
    gap: 0;
  }
  .narrow .tab {
    flex: 1 0 auto;
    padding: 0 var(--ha-space-1, 4px);
    font-size: var(--ha-font-size-s, 13px);
    gap: 6px;
  }

  /* admin banner of Today: signatures waiting for a name */
  .review-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ha-space-2, 8px) var(--ha-space-4, 16px);
    width: 100%;
    box-sizing: border-box;
    padding: 12px 16px;
    font: inherit;
    text-align: start;
    color: var(--primary-text-color);
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 10%, var(--card-background-color, #fff));
    border: 1px solid color-mix(in srgb, var(--primary-color, #03a9f4) 35%, transparent);
    border-radius: var(--ha-card-border-radius, 12px);
    cursor: pointer;
  }
  .review-banner:hover {
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 16%, var(--card-background-color, #fff));
  }
  .review-banner:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .review-banner svg {
    flex: none;
    width: 24px;
    height: 24px;
    fill: var(--primary-color, #03a9f4);
  }
  .review-banner-text {
    flex: 1 1 200px;
    min-width: 0;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .review-banner-action {
    font-weight: var(--ha-font-weight-medium, 500);
    color: var(--primary-color, #03a9f4);
    white-space: nowrap;
  }

  .content {
    max-width: 1440px;
    margin: 0 auto;
    padding: var(--ha-space-4, 16px);
    padding-bottom: calc(var(--ha-space-4, 16px) + env(safe-area-inset-bottom));
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: var(--ha-space-4, 16px);
  }
  .narrow .content {
    padding: var(--ha-space-2, 8px);
    gap: var(--ha-space-2, 8px);
  }

  /* day bar */
  .daybar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--ha-space-1, 4px);
  }
  /* HA's picker sizes its popover from its own width: room for the presets */
  .ha-day-picker {
    display: block;
    flex: 0 1 600px;
    min-width: 0;
  }
  .no-report-day {
    color: var(--primary-text-color);
    font-size: var(--ha-font-size-l, 16px);
    font-weight: var(--ha-font-weight-medium, 500);
    margin-bottom: 8px;
  }
  .day-input,
  .day-select {
    font: inherit;
    font-size: var(--ha-font-size-l, 16px);
    color: var(--primary-text-color);
    background: var(--card-background-color, #fff);
    border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
    border-radius: var(--ha-border-radius-md, 8px);
    padding: 8px 12px;
    min-height: 40px;
    max-width: 100%;
    cursor: pointer;
  }
  .day-input:focus-visible,
  .day-select:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .spacer {
    flex: 1;
  }

  /* own fallbacks of the HA elements */
  .icon-button {
    width: 40px;
    height: 40px;
    border: none;
    border-radius: 50%;
    background: none;
    color: inherit;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
  }
  .icon-button:hover:not([disabled]) {
    background: rgba(127, 127, 127, 0.15);
  }
  .icon-button[disabled] {
    color: var(--disabled-text-color, #bdbdbd);
    cursor: default;
  }
  .icon-button svg {
    width: 24px;
    height: 24px;
    fill: currentColor;
  }
  .text-button {
    font: inherit;
    font-weight: var(--ha-font-weight-medium, 500);
    font-size: var(--ha-font-size-m, 14px);
    color: var(--primary-color);
    background: none;
    border: none;
    border-radius: var(--ha-border-radius-pill, 9999px);
    padding: 0 12px;
    height: 36px;
    cursor: pointer;
  }
  .text-button:hover:not([disabled]) {
    background: rgba(127, 127, 127, 0.12);
  }
  .text-button.accent {
    color: var(--text-primary-color, #fff);
    background: var(--primary-color);
  }
  .text-button.accent:hover:not([disabled]) {
    background: var(--primary-color);
    filter: brightness(1.1);
  }
  .text-button[disabled] {
    color: var(--disabled-text-color, #bdbdbd);
    cursor: default;
  }
  .text-button.accent[disabled] {
    background: var(--disabled-color, #bdbdbd);
    color: var(--text-primary-color, #fff);
  }
  .alert {
    display: flex;
    gap: 12px;
    padding: 8px 12px;
    border-radius: var(--ha-border-radius-sm, 4px);
    background: var(--secondary-background-color, #e5e5e5);
    border-inline-start: 4px solid var(--alert-color);
    color: var(--primary-text-color);
  }
  .alert > div {
    flex: 1;
    min-width: 0;
  }
  .alert.warning {
    --alert-color: var(--warning-color, #ffa600);
  }
  .alert.error {
    --alert-color: var(--error-color, #db4437);
  }
  .alert.info {
    --alert-color: var(--info-color, #039be5);
  }
  .alert.success {
    --alert-color: var(--success-color, #43a047);
  }
  .alert .alert-title {
    font-weight: var(--ha-font-weight-medium, 500);
  }

  /* cards, as ha-card */
  .card {
    background: var(--ha-card-background, var(--card-background-color, #fff));
    border-radius: var(--ha-card-border-radius, 12px);
    border-width: var(--ha-card-border-width, 1px);
    border-style: solid;
    border-color: var(--ha-card-border-color, var(--divider-color, #e0e0e0));
    box-shadow: var(--ha-card-box-shadow, none);
    color: var(--primary-text-color);
    box-sizing: border-box;
    min-width: 0;
  }
  .card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--ha-card-header-font-size, var(--ha-font-size-2xl, 24px));
    font-weight: var(--ha-font-weight-normal, 400);
    line-height: var(--ha-line-height-condensed, 1.2);
    letter-spacing: -0.012em;
    padding: 16px 16px 8px;
  }
  .card-header .spacer {
    flex: 1;
  }
  .card-hint {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    padding: 0 16px 8px;
  }
  .card-content {
    padding: 0 16px 16px;
  }
  .narrow .card-header {
    font-size: var(--ha-font-size-xl, 20px);
  }

  /* summary card and split bar (#48) */
  .summary {
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .summary-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 32px;
  }
  .summary-label {
    margin: 0;
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-m, 14px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .summary-value {
    font-size: var(--ha-font-size-4xl, 36px);
    line-height: 1.2;
  }
  .summary-facts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 24px;
    color: var(--secondary-text-color);
  }
  .summary-facts strong,
  .split-legend strong {
    color: var(--primary-text-color);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .summary-facts .dot {
    margin-inline-end: 6px;
    vertical-align: baseline;
  }
  .split-bar {
    display: flex;
    height: 28px;
    border-radius: 6px;
    overflow: hidden;
    gap: 1px;
  }
  .split-bar .segment {
    min-width: 2px;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .split-bar.has-selection .segment:not(.selected) {
    opacity: 0.3;
  }
  .split-legend {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    font-size: var(--ha-font-size-s, 13px);
  }
  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    margin: 0 -8px 0 0;
    border: 1px solid transparent;
    border-radius: var(--ha-border-radius-pill, 9999px);
    background: none;
    color: var(--primary-text-color);
    font: inherit;
    cursor: pointer;
    text-align: start;
  }
  .legend-item:hover {
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.1));
  }
  .legend-item:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .legend-item.selected {
    border-color: var(--primary-color);
  }
  .legend-values {
    white-space: nowrap;
  }
  .legend-share {
    color: var(--secondary-text-color);
  }
  .narrow .summary {
    padding: 16px;
    gap: 12px;
  }
  .narrow .summary-value {
    font-size: var(--ha-font-size-3xl, 30px);
  }
  .narrow .split-bar {
    height: 22px;
  }
  .narrow .split-legend {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 2px 8px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .narrow .legend-item {
    width: 100%;
    margin: 0;
    padding: 4px 6px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    column-gap: 6px;
    border-radius: 8px;
  }
  .narrow .legend-values {
    grid-column: 2;
  }
  .narrow .legend-name {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex: none;
    display: inline-block;
  }

  .message {
    color: var(--secondary-text-color);
    padding: 24px 16px;
    text-align: center;
  }

  /* Today, under the chart: what used energy on the left, the peaks and the
     things to check on the right; stacked when narrow */
  .today-columns {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr));
    gap: var(--ha-space-4, 16px);
    align-items: start;
  }
  .today-side {
    display: flex;
    flex-direction: column;
    gap: var(--ha-space-4, 16px);
    min-width: 0;
  }
  .narrow .today-columns,
  .narrow .today-side {
    gap: var(--ha-space-2, 8px);
  }
  .card-header h2 {
    margin: 0;
    font: inherit;
    letter-spacing: inherit;
  }

  /* what used energy (#50) */
  .usage-list {
    list-style: none;
    margin: 0;
    padding: 0 0 8px;
  }
  .usage-item {
    display: flex;
    align-items: center;
    border-top: 1px solid var(--divider-color);
  }
  .usage-item.selected {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
  }
  .usage-list.has-selection .usage-item:not(.selected) .usage-row {
    opacity: 0.55;
  }
  .usage-row {
    flex: 1;
    min-width: 0;
    display: grid;
    grid-template-columns: 12px minmax(0, 1fr) 80px 72px;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    font: inherit;
    color: inherit;
    text-align: start;
    background: none;
    border: none;
    cursor: pointer;
  }
  .usage-row:hover {
    background: rgba(127, 127, 127, 0.08);
  }
  .usage-row:focus-visible,
  .usage-review:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }
  .dot.unnamed {
    border: 2px dashed;
    box-sizing: border-box;
    background: transparent;
  }
  .usage-text {
    min-width: 0;
  }
  .usage-name {
    display: block;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .usage-name.unnamed {
    font-style: italic;
  }
  .usage-detail {
    display: block;
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .usage-bar {
    display: block;
    height: 6px;
    border-radius: 3px;
    overflow: hidden;
    background: var(--secondary-background-color, #e5e5e5);
  }
  .usage-bar > span {
    display: block;
    height: 100%;
    min-width: 2px;
  }
  .usage-energy {
    text-align: end;
    font-weight: var(--ha-font-weight-medium, 500);
    white-space: nowrap;
  }
  .usage-review {
    flex: none;
    width: 40px;
    height: 40px;
    margin-inline-end: 8px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: none;
    border-radius: 50%;
    background: none;
    color: var(--secondary-text-color);
    cursor: pointer;
  }
  .usage-review-space {
    flex: none;
    width: 48px;
  }
  .usage-review:hover {
    background: rgba(127, 127, 127, 0.12);
  }
  .usage-review svg {
    width: 22px;
    height: 22px;
    fill: currentColor;
  }
  /* phones: the bar goes under the name, which keeps the width */
  .narrow .usage-row {
    grid-template-columns: 12px minmax(0, 1fr) auto;
    gap: 6px 10px;
    padding: 10px 12px;
  }
  .narrow .usage-bar {
    grid-row: 2;
    grid-column: 2 / 4;
  }
  .narrow .usage-review {
    margin-inline-end: 4px;
  }
  .narrow .usage-review-space {
    width: 44px;
  }
  /* the day, appliance by appliance (#49) */
  .day-card .card-header {
    flex-wrap: wrap;
  }
  .view-toggle {
    display: inline-flex;
    gap: 6px;
  }
  .chip.link.view {
    padding: 4px 12px;
    font-size: var(--ha-font-size-m, 14px);
  }
  .timeline {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .tl-rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .tl-row {
    display: grid;
    grid-template-columns: clamp(110px, 22%, 220px) minmax(0, 1fr) 72px;
    gap: 12px;
    align-items: center;
  }
  .tl-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font: inherit;
    font-size: var(--ha-font-size-s, 13px);
    color: var(--primary-text-color);
    text-align: start;
    background: none;
    border: none;
    border-radius: var(--ha-border-radius-sm, 4px);
    padding: 2px 0;
    cursor: pointer;
  }
  .tl-house .tl-name,
  .tl-axis-row .tl-name {
    cursor: default;
    color: var(--secondary-text-color);
  }
  .tl-name:hover {
    text-decoration: underline;
  }
  .tl-name:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .tl-row.unnamed .tl-name {
    font-style: italic;
  }
  .tl-row.selected .tl-name {
    font-weight: var(--ha-font-weight-bold, 700);
  }
  .tl-energy {
    text-align: end;
    white-space: nowrap;
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .tl-house .tl-energy {
    font-weight: var(--ha-font-weight-normal, 400);
  }
  .tl-strip {
    position: relative;
    height: 56px;
  }
  .tl-strip svg {
    display: block;
    width: 100%;
    height: 100%;
    fill: var(--disabled-text-color, #bdbdbd);
  }
  .tl-mark {
    position: absolute;
    top: 0;
    bottom: 0;
    background: var(--error-color, #db4437);
    opacity: 0.18;
    border-radius: 2px;
    cursor: pointer;
  }
  .tl-track {
    position: relative;
    height: 22px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--secondary-background-color, #f5f5f5);
    cursor: pointer;
  }
  .tl-row.selected .tl-track {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.16);
  }
  .tl-block {
    position: absolute;
    top: 3px;
    bottom: 3px;
    border-radius: 3px;
    background: var(--c);
    box-sizing: border-box;
  }
  /* a cycling session: one block, striped like its cycles */
  .tl-block.cycling {
    background: repeating-linear-gradient(90deg, var(--c) 0 3px, transparent 3px 5px);
  }
  /* a signature still to name: dashed, like its dot in the list */
  .tl-row.unnamed .tl-block {
    border: 2px dashed var(--c);
    background: color-mix(in srgb, var(--c) 22%, transparent);
  }
  .tl-row.unnamed .tl-block.cycling {
    background: repeating-linear-gradient(90deg, color-mix(in srgb, var(--c) 45%, transparent) 0 3px, transparent 3px 5px);
  }
  .timeline.has-selection .tl-row:not(.selected):not(.tl-house):not(.tl-axis-row) .tl-block {
    opacity: 0.25;
  }
  .timeline.has-selection .tl-row:not(.selected):not(.tl-house):not(.tl-axis-row) :is(.tl-name, .tl-energy) {
    opacity: 0.5;
  }
  .tl-axis {
    position: relative;
    height: 16px;
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-xs, 11px);
  }
  .tl-tick {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
    white-space: nowrap;
  }
  .tl-tick.start {
    transform: none;
  }
  .tl-tick.end {
    transform: translateX(-100%);
  }
  .tl-peaks {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--divider-color);
  }
  .chip.link.peak-chip {
    display: inline-block;
    text-align: start;
    max-width: 100%;
    padding: 4px 12px;
    color: var(--error-color, #db4437);
    border-color: color-mix(in srgb, var(--error-color, #db4437) 40%, transparent);
    background: color-mix(in srgb, var(--error-color, #db4437) 8%, transparent);
  }
  .chip.link.peak-chip strong {
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .chip.link.peak-chip.selected {
    border-color: var(--error-color, #db4437);
    background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
  }
  /* phones: the name and the energy above the track, which takes the width */
  .narrow .tl-row {
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 2px 8px;
  }
  .narrow .tl-row > .tl-strip,
  .narrow .tl-row > .tl-track,
  .narrow .tl-row > .tl-axis {
    grid-row: 2;
    grid-column: 1 / 3;
  }
  .narrow .tl-axis-row > .tl-name,
  .narrow .tl-axis-row > .tl-energy {
    display: none;
  }
  .narrow .tl-rows {
    gap: 8px;
  }
  .narrow .tl-strip {
    height: 40px;
  }
  .narrow .tl-track {
    height: 16px;
  }
  .narrow .tl-block {
    top: 2px;
    bottom: 2px;
  }

  /* peaks */
  .peaks {
    list-style: none;
    margin: 0;
    padding: 0 0 8px;
  }
  .peak {
    display: block;
    width: 100%;
    text-align: start;
    font: inherit;
    color: inherit;
    background: none;
    border: none;
    border-top: 1px solid var(--divider-color);
    padding: 12px 16px;
    cursor: pointer;
  }
  .peak:hover,
  .peak:focus-visible {
    background: rgba(127, 127, 127, 0.08);
    outline: none;
  }
  .peak-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
  }
  .peak-power {
    font-size: var(--ha-font-size-l, 16px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .peak-time {
    color: var(--secondary-text-color);
  }
  .badge {
    font-size: var(--ha-font-size-xs, 10px);
    font-weight: var(--ha-font-weight-medium, 500);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    white-space: nowrap;
    flex: none;
    border-radius: var(--ha-border-radius-pill, 9999px);
    padding: 2px 8px;
    color: var(--text-primary-color, #fff);
    background: var(--error-color, #db4437);
  }
  .badge.neutral {
    color: var(--primary-text-color);
    background: var(--secondary-background-color, #e5e5e5);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-border-radius-pill, 9999px);
    padding: 2px 10px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .chip .chip-value {
    color: var(--secondary-text-color);
  }
  .peak-parts {
    margin-top: 4px;
    font-size: var(--ha-font-size-s, 13px);
    display: flex;
    flex-wrap: wrap;
    gap: 2px 12px;
  }
  .peak-part {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .peak-part .secondary {
    color: var(--secondary-text-color);
  }
  .peak-part.secondary {
    color: var(--secondary-text-color);
  }

  /* tables */
  .table-wrap {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--ha-font-size-m, 14px);
  }
  th {
    text-align: start;
    font-weight: var(--ha-font-weight-medium, 500);
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    padding: 8px;
    white-space: nowrap;
    border-bottom: 1px solid var(--divider-color);
  }
  td {
    padding: 8px;
    border-bottom: 1px solid var(--divider-color);
    vertical-align: middle;
    white-space: nowrap;
  }
  /* the name column wraps so that narrow tables fit their card; the other
     columns stay on one line (the table scrolls inside .table-wrap if needed) */
  td:first-child {
    white-space: normal;
    min-width: 120px;
  }
  tr:last-child td {
    border-bottom: none;
  }
  th:first-child,
  td:first-child {
    padding-inline-start: 16px;
  }
  th:last-child,
  td:last-child {
    padding-inline-end: 16px;
  }
  td.num,
  th.num {
    text-align: end;
  }
  .sig-name {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .secondary {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .sig-id {
    overflow-wrap: anywhere;
  }
  .hours {
    display: flex;
    align-items: flex-end;
    gap: 1px;
    height: 24px;
    width: 120px;
  }
  .hours > div {
    flex: 1;
    background: var(--primary-color);
    border-radius: 1px 1px 0 0;
    min-height: 1px;
  }
  .hours > div.zero {
    background: var(--divider-color);
  }
  .sig-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .sig-list li {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-top: 1px solid var(--divider-color);
  }
  .sig-list .sig-main {
    flex: 1;
    min-width: 0;
  }
  .sig-list .hours {
    width: 72px;
    flex: none;
  }
  .more {
    display: flex;
    justify-content: center;
    padding: 4px 0 8px;
  }

  .footer {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    text-align: center;
  }
  .findings {
    padding: 12px 20px;
    border: 1px solid var(--warning-color, #ffa600);
    background: rgba(var(--rgb-warning-color, 255, 166, 0), 0.1);
  }
  .findings summary {
    cursor: pointer;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .findings summary:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  .findings ul {
    margin: 8px 0 0;
    padding-inline-start: 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: var(--ha-font-size-s, 13px);
  }
  .findings strong {
    display: block;
    font-weight: var(--ha-font-weight-medium, 500);
  }

  .chip.link {
    font: inherit;
    font-size: var(--ha-font-size-s, 12px);
    color: var(--primary-text-color);
    background: none;
    cursor: pointer;
  }
  .chip.link:hover,
  .chip.link:focus-visible {
    border-color: var(--primary-color);
    outline: none;
  }
  .chip.link.selected {
    border-color: var(--primary-color);
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: var(--ha-font-size-s, 12px);
    color: var(--secondary-text-color);
  }
  .text-input {
    font: inherit;
    font-size: var(--ha-font-size-m, 14px);
    color: var(--primary-text-color);
    background: var(--card-background-color, #fff);
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-border-radius-sm, 4px);
    padding: 8px 10px;
    max-width: 420px;
  }
  .text-input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -1px;
  }
  .field-error {
    color: var(--error-color, #db4437);
    font-size: var(--ha-font-size-s, 12px);
  }

  /* To review, one signature at a time (#51) */
  .review {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .rq-progress {
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .rq-progress-head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: baseline;
    gap: 4px 16px;
  }
  .rq-progress-head strong {
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-bar {
    height: 8px;
    border-radius: 4px;
    background: var(--secondary-background-color, #eeeeee);
    overflow: hidden;
  }
  .rq-bar > div {
    height: 100%;
    background: var(--success-color, #43a047);
    transition: width 0.3s;
  }
  .rq-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .filter-chip {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
    padding: 0 12px;
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-border-radius-pill, 9999px);
    background: none;
    color: var(--primary-text-color);
    font: inherit;
    font-size: var(--ha-font-size-s, 13px);
    font-weight: var(--ha-font-weight-medium, 500);
    cursor: pointer;
    white-space: nowrap;
  }
  .filter-chip:hover {
    border-color: var(--primary-color);
  }
  .filter-chip:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .filter-chip.selected {
    border-color: var(--primary-color);
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
    color: var(--primary-color);
  }
  .rq-layout {
    container: rq / inline-size;
  }
  .rq-grid {
    display: flex;
    gap: 16px;
    align-items: flex-start;
  }
  .rq-queue {
    flex: 0 0 340px;
    overflow: hidden;
  }
  .rq-queue-head {
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 52px;
    padding: 4px 8px 4px 16px;
    box-sizing: border-box;
  }
  .rq-queue-title {
    flex: 1;
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 13px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rq-list > li {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 16px 8px 12px;
    border-top: 1px solid var(--divider-color);
  }
  .rq-list > li.current {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
    box-shadow: inset 3px 0 0 var(--primary-color);
  }
  .rq-list input[type="checkbox"] {
    flex: none;
    width: 18px;
    height: 18px;
    margin: 0;
    accent-color: var(--primary-color);
  }
  .rq-row {
    all: unset;
    box-sizing: border-box;
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 4px 0;
    cursor: pointer;
  }
  .rq-row:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  .rq-row-name {
    font-weight: var(--ha-font-weight-medium, 500);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rq-row-energy {
    flex: none;
    font-size: var(--ha-font-size-s, 13px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-queue-empty {
    padding: 12px 16px 16px;
    border-top: 1px solid var(--divider-color);
  }
  .rq-queue-more {
    padding: 8px 12px;
    border-top: 1px solid var(--divider-color);
  }
  .rq-detail {
    flex: 1;
    min-width: 0;
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .rq-stepper {
    display: none;
    align-items: center;
    gap: 4px;
    margin: -8px -8px 0;
  }
  .rq-stepper-pos {
    flex: 1;
    text-align: center;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-head {
    display: flex;
    align-items: flex-start;
    gap: 4px;
  }
  .rq-head-main {
    flex: 1;
    min-width: 0;
  }
  .rq-head h2 {
    margin: 4px 0 0;
    font-size: var(--ha-font-size-2xl, 24px);
    font-weight: var(--ha-font-weight-normal, 400);
    line-height: 1.25;
    overflow-wrap: anywhere;
  }
  .rq-head h2:focus {
    outline: none;
  }
  .rq-head h2:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  .rq-pos + .rq-note::before {
    content: " · ";
  }
  .rq-nav {
    display: inline-flex;
    flex: none;
    margin: -8px -12px 0 0;
  }
  .rq-facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 16px;
    margin: 0;
  }
  .rq-facts dt {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .rq-facts dd {
    margin: 2px 0 0;
    font-size: var(--ha-font-size-l, 18px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-facts .rq-phases {
    grid-column: 1 / -1;
  }
  .rq-facts .rq-phases dd {
    font-size: var(--ha-font-size-m, 14px);
    overflow-wrap: anywhere;
  }
  .rq-label {
    margin-bottom: 6px;
  }
  .rq-hist {
    display: grid;
    grid-template-columns: repeat(24, minmax(0, 1fr));
    gap: 2px;
    align-items: end;
    height: 48px;
  }
  .rq-hist > div {
    border-radius: 2px;
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.45);
  }
  .rq-hist > div.peak {
    background: var(--primary-color);
  }
  .rq-hist > div.zero {
    background: var(--divider-color);
  }
  .rq-hist-axis {
    display: flex;
    justify-content: space-between;
    margin-top: 2px;
    font-size: var(--ha-font-size-xs, 11px);
  }
  /* day raster (#65): one row per day, the most recent on top */
  .rq-raster {
    --rq-day-w: 64px;
    --rq-row-h: 14px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .rq-raster-row {
    display: grid;
    grid-template-columns: var(--rq-day-w) minmax(0, 1fr);
    align-items: center;
    column-gap: 8px;
  }
  .rq-strip {
    display: grid;
    grid-template-columns: repeat(24, minmax(0, 1fr));
    gap: 1px;
    height: 8px;
  }
  .rq-strip > div {
    border-radius: 1px;
    background: var(--primary-color);
  }
  .rq-strip > div.zero {
    background: var(--divider-color);
  }
  .rq-axis-track {
    position: relative;
    height: 14px;
    font-size: var(--ha-font-size-xs, 11px);
    line-height: 14px;
  }
  .rq-axis-track > span {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
    white-space: nowrap;
  }
  .rq-axis-track > span.first {
    transform: none;
  }
  .rq-axis-track > span.last {
    transform: translateX(-100%);
  }
  .rq-raster-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    /* about ten days visible, the older ones by scrolling */
    max-height: calc(10 * (var(--rq-row-h) + 2px));
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 2px 0;
  }
  .rq-raster-body > .rq-raster-row {
    min-height: var(--rq-row-h);
    border-radius: 3px;
  }
  .rq-raster-body > .rq-raster-row.weekend {
    background: color-mix(in srgb, var(--primary-text-color, #212121) 6%, transparent);
  }
  .rq-day {
    font-size: var(--ha-font-size-xs, 11px);
    line-height: var(--rq-row-h);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding-inline-start: 4px;
  }
  .rq-track {
    position: relative;
    height: var(--rq-row-h);
    margin-inline-end: 3px;
    background: linear-gradient(var(--divider-color), var(--divider-color)) center / 100% 1px no-repeat;
  }
  .rq-hour-line {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 1px;
    background: var(--divider-color);
  }
  .rq-block {
    position: absolute;
    top: 1px;
    bottom: 1px;
    min-width: 3px;
    max-width: 100%;
    padding: 0;
    margin: 0;
    border: 0;
    border-radius: 2px;
    background: var(--primary-color);
    cursor: pointer;
  }
  .rq-block.cut-before {
    border-start-start-radius: 0;
    border-end-start-radius: 0;
  }
  .rq-block.cut-after {
    border-start-end-radius: 0;
    border-end-end-radius: 0;
  }
  .rq-block[aria-disabled="true"] {
    cursor: default;
  }
  .rq-block:hover {
    filter: brightness(1.15);
  }
  .rq-block:focus-visible {
    outline: 2px solid var(--primary-text-color);
    outline-offset: 1px;
    z-index: 1;
    opacity: 1 !important;
  }
  .rq-examples {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
  }
  .rq-example {
    display: block;
    box-sizing: border-box;
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--divider-color);
    border-radius: 8px;
    background: none;
    color: inherit;
    font: inherit;
    text-align: start;
  }
  button.rq-example {
    cursor: pointer;
  }
  button.rq-example:hover,
  button.rq-example:focus-visible {
    border-color: var(--primary-color);
    outline: none;
  }
  .rq-example-curve {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 56px;
  }
  .rq-example-curve .use {
    fill: var(--primary-color);
    fill-opacity: 0.18;
  }
  .rq-example-curve .area {
    fill: var(--secondary-text-color);
    fill-opacity: 0.15;
  }
  .rq-example-curve .line {
    fill: none;
    stroke: var(--secondary-text-color);
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
  }
  .rq-example-label {
    display: block;
    margin-top: 6px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .rq-example-label strong {
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-suggestion {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
    padding: 12px 16px;
    border-radius: 8px;
    border: 1px solid rgba(var(--rgb-success-color, 67, 160, 71), 0.4);
    background: rgba(var(--rgb-success-color, 67, 160, 71), 0.1);
  }
  .rq-suggestion > svg {
    flex: none;
    width: 22px;
    height: 22px;
    fill: var(--success-color, #43a047);
  }
  .rq-suggestion-text {
    flex: 1 1 240px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: var(--ha-font-size-s, 13px);
  }
  .rq-suggestion-text strong {
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .rq-suggestion-actions {
    display: inline-flex;
    gap: 4px;
  }
  .rq-form {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding-top: 20px;
    border-top: 1px solid var(--divider-color);
  }
  .rq-form .chips {
    margin-top: 0;
  }
  .rq-how {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
  }
  .seg {
    display: inline-flex;
    flex-wrap: wrap;
    max-width: 100%;
    border: 1px solid var(--divider-color);
    border-radius: 20px;
    overflow: hidden;
  }
  .seg button {
    min-height: 40px;
    padding: 0 14px;
    border: 0;
    border-inline-end: 1px solid var(--divider-color);
    background: none;
    color: var(--primary-text-color);
    font: inherit;
    font-size: var(--ha-font-size-s, 13px);
    font-weight: var(--ha-font-weight-medium, 500);
    cursor: pointer;
  }
  .seg button:last-child {
    border-inline-end: 0;
  }
  .seg button.selected {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
    color: var(--primary-color);
  }
  .seg button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }
  .rq-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .rq-keys {
    margin-inline-start: auto;
  }
  kbd {
    font: 11px monospace;
    border: 1px solid var(--divider-color);
    border-bottom-width: 2px;
    border-radius: 4px;
    padding: 0 5px;
    color: var(--primary-text-color);
    background: var(--secondary-background-color, #fafafa);
  }
  /* phones: the queue becomes a "1 of 34" stepper above the detail */
  @container rq (max-width: 759px) {
    .rq-raster {
      --rq-day-w: 48px;
      --rq-row-h: 10px;
    }
    .rq-raster-row {
      column-gap: 6px;
    }
    .rq-axis-track > span.minor {
      display: none;
    }
    .rq-queue,
    .rq-nav,
    .rq-pos,
    .rq-keys {
      display: none;
    }
    .rq-stepper {
      display: flex;
    }
    .rq-detail {
      padding: 16px;
      gap: 16px;
    }
    .rq-head h2 {
      font-size: var(--ha-font-size-xl, 20px);
    }
    .rq-facts {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .seg {
      flex-direction: column;
      width: 100%;
      border-radius: 12px;
    }
    .seg button {
      border-inline-end: 0;
      border-bottom: 1px solid var(--divider-color);
      text-align: start;
    }
    .seg button:last-child {
      border-bottom: 0;
    }
  }

  /* Appliances tab (#54): one card per appliance */
  .appliances .card-header {
    flex-wrap: wrap;
  }
  .appliance-grid {
    list-style: none;
    margin: 0;
    padding: 8px 16px 16px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(240px, 100%), 1fr));
    gap: 12px;
  }
  .appliance-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-card-border-radius, 12px);
    overflow: hidden;
  }
  .appliance-card.selected {
    border-color: var(--primary-color);
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
  }
  .appliance-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
    padding: 12px 14px;
    font: inherit;
    color: inherit;
    text-align: start;
    background: none;
    border: none;
  }
  button.appliance-main {
    cursor: pointer;
  }
  button.appliance-main:hover {
    background: rgba(127, 127, 127, 0.08);
  }
  button.appliance-main:focus-visible,
  .appliance-settings:focus-visible,
  .all-signatures summary:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }
  .appliance-head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .appliance-name {
    font-weight: var(--ha-font-weight-medium, 500);
    min-width: 0;
  }
  .appliance-tracking,
  .appliance-last,
  .appliance-facts {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .appliance-figures {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
  }
  .appliance-energy {
    font-size: var(--ha-font-size-xl, 20px);
    line-height: 1.3;
  }
  .appliance-energy.none {
    font-size: var(--ha-font-size-m, 14px);
    color: var(--secondary-text-color);
  }
  .appliance-bars {
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 36px;
    padding-top: 2px;
  }
  .appliance-bars.month {
    gap: 1px;
  }
  .appliance-bar {
    flex: 1 1 0;
    min-width: 0;
    border-radius: 2px 2px 0 0;
    background: var(--primary-color);
    opacity: 0.4;
  }
  .appliance-bar.current {
    opacity: 1;
  }
  .appliance-bar.zero {
    background: var(--disabled-color, #bdbdbd);
  }
  .appliance-takeover {
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-top: 1px solid var(--divider-color);
    padding: 8px 12px;
    font-size: var(--ha-font-size-s, 13px);
    color: var(--secondary-text-color);
  }
  .takeover-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 4px 8px;
  }
  .takeover-line > span {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .takeover-confirm {
    display: flex;
    flex-direction: column;
    gap: 8px;
    color: var(--primary-text-color);
  }
  .takeover-confirm p {
    margin: 0;
  }
  .takeover-confirm select {
    width: 100%;
    max-width: 100%;
  }
  .takeover-error {
    color: var(--error-color);
  }
  .connected-action {
    display: flex;
    justify-content: flex-end;
    padding: 0 16px 8px;
  }
  .connected-new {
    margin: 0 16px 12px;
    padding: 12px;
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-card-border-radius, 12px);
  }
  .connected-states {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    margin: 0;
    padding: 4px 0;
    border: none;
    font-size: var(--ha-font-size-s, 12px);
    color: var(--secondary-text-color);
  }
  .connected-states legend {
    padding: 0 0 4px;
  }
  .check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--primary-text-color);
    overflow-wrap: anywhere;
  }
  .teach-status {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .teach-title {
    color: var(--primary-text-color);
  }
  .takeover-buttons {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }
  .appliance-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    border-top: 1px solid var(--divider-color);
    padding: 2px 6px;
  }
  .appliance-settings {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 36px;
    padding: 0 10px;
    border-radius: 18px;
    color: var(--primary-color);
    font-size: var(--ha-font-size-s, 13px);
    font-weight: var(--ha-font-weight-medium, 500);
    text-decoration: none;
  }
  .appliance-settings:hover {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
  }
  .appliance-settings svg {
    width: 18px;
    height: 18px;
    fill: currentColor;
  }
  .narrow .appliance-grid {
    padding: 8px 12px 12px;
    gap: 8px;
  }
  /* nightly self-test (#15) */
  .self-test-list {
    list-style: none;
    margin: 0;
    padding: 0 0 8px;
  }
  .self-test-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 16px;
    border-top: 1px solid var(--divider-color);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .self-test-main {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }
  .self-test-name {
    font-weight: var(--ha-font-weight-medium, 500);
    min-width: 0;
  }
  .self-test-rate {
    flex: none;
    font-size: var(--ha-font-size-l, 16px);
    font-weight: var(--ha-font-weight-medium, 500);
    font-variant-numeric: tabular-nums;
  }
  .self-test-rate.tone-good {
    color: var(--success-color, #43a047);
  }
  .self-test-rate.tone-fair {
    color: var(--warning-color, #ffa600);
  }
  .self-test-rate.tone-poor {
    color: var(--error-color, #db4437);
  }
  .self-test-headline {
    color: var(--primary-text-color);
  }
  .self-test-trend.trend-up {
    color: var(--success-color, #43a047);
  }
  .self-test-trend.trend-down {
    color: var(--error-color, #db4437);
  }
  .self-test-detail {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .all-signatures {
    padding: 0;
  }
  .all-signatures > summary {
    cursor: pointer;
    padding: 14px 16px;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .sub-section {
    border-top: 1px solid var(--divider-color);
    padding-top: 4px;
  }
  .sub-header {
    margin: 0;
    padding: 12px 16px 4px;
    font-size: var(--ha-font-size-l, 16px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
`;
