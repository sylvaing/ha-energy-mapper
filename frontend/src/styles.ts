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

  /* summary tiles */
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--ha-space-2, 8px);
  }
  .narrow .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .tile {
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .tile .label {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .tile .value {
    font-size: var(--ha-font-size-xl, 20px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .tile .sub {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .tile.main .value {
    color: var(--primary-color);
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

  .columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--ha-space-4, 16px);
    align-items: start;
  }
  .narrow .columns,
  .single .columns {
    grid-template-columns: minmax(0, 1fr);
    gap: var(--ha-space-2, 8px);
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
  .chip.unexplained {
    border-style: dashed;
  }
  .share {
    margin-top: 8px;
    height: 4px;
    border-radius: 2px;
    background: var(--secondary-background-color, #e5e5e5);
    overflow: hidden;
  }
  .share > div {
    height: 100%;
    background: var(--disabled-color, #bdbdbd);
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
  .narrow .tile {
    padding: 8px 12px;
  }
  .narrow .tile .value {
    font-size: var(--ha-font-size-l, 16px);
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
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* review of the signatures */
  .review .card-header .count {
    margin-inline-start: 8px;
    min-width: 20px;
    padding: 0 6px;
    border-radius: var(--ha-border-radius-pill, 9999px);
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
    font-size: var(--ha-font-size-s, 12px);
    line-height: 20px;
    text-align: center;
  }
  .review-list,
  .review-compact {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .review-list > li {
    padding: 12px 16px;
    border-top: 1px solid var(--divider-color);
  }
  .review-head {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .review-main {
    flex: 1;
    min-width: 0;
  }
  .review-head .hours {
    flex: none;
  }
  .narrow .review-head .hours {
    width: 72px;
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
  .probably {
    margin-top: 2px;
    color: var(--primary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .review-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 8px;
  }
  .review-form {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 12px;
    padding: 12px;
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-card-border-radius, 12px);
  }
  .review-form fieldset {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
    margin: 0;
    padding: 0;
    border: none;
  }
  .review-form legend {
    padding: 0;
    margin-bottom: 4px;
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .radio {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  .radio input {
    accent-color: var(--primary-color);
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
  .review-more {
    padding: 8px 16px;
    border-top: 1px solid var(--divider-color);
  }
  .review-compact > li {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
  }
  .review-compact .review-main {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 8px;
    align-items: baseline;
  }
`;
