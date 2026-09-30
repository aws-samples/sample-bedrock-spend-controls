/** Number presentation for the console.
 *
 * One fixed convention regardless of browser locale: `,` groups thousands and
 * `.` separates decimals (1,234.56), money always carries exactly two
 * decimals, and integers carry none. Dates keep the browser locale; only
 * numbers are pinned, so a spend figure reads the same on every operator's
 * machine.
 */

const GROUPING_LOCALE = "en-US";

const INTEGER = new Intl.NumberFormat(GROUPING_LOCALE, { maximumFractionDigits: 0 });
const MONEY = new Intl.NumberFormat(GROUPING_LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const ONE_DECIMAL = new Intl.NumberFormat(GROUPING_LOCALE, { maximumFractionDigits: 1 });
const TWO_DECIMALS = new Intl.NumberFormat(GROUPING_LOCALE, { maximumFractionDigits: 2 });

/** `$1,234.56`: USD with exactly two decimals. Sub-cent amounts round to
 *  `$0.00`; the ledger keeps micro-dollar precision, the console does not
 *  show it. */
export function formatUsd(value: number): string {
  return `$${MONEY.format(value)}`;
}

/** `1,234`: integer with thousands grouping. Non-integers are rounded. */
export function formatNumber(value: number): string {
  return INTEGER.format(value);
}

/** `12.5`: up to two decimals, trailing zeros dropped. For ratios, seconds,
 *  and other non-money decimals. */
export function formatDecimal(value: number): string {
  return TWO_DECIMALS.format(value);
}

/** `21.6M`: abbreviated token counts. Below 1,000 the plain integer. */
export function formatCompact(value: number): string {
  const magnitude = Math.abs(value);
  if (magnitude < 1_000) return INTEGER.format(value);
  const units: Array<[number, string]> = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, suffix] of units) {
    if (magnitude >= size) return `${ONE_DECIMAL.format(value / size)}${suffix}`;
  }
  return INTEGER.format(value);
}

/** `+1.90%` / `-12.00%`, always within +/-100 %; `null` means neither the
 *  ledger nor the bill saw anything that day. */
export function formatSignedPercent(value: number | null): string {
  if (value === null) return "n/a (no activity)";
  const sign = value > 0 ? "+" : "";
  return `${sign}${MONEY.format(value)}%`;
}

/** `80%` / `12.5%`: a ratio (0..1) as a percentage. */
export function formatRatioPercent(ratio: number): string {
  return `${TWO_DECIMALS.format(ratio * 100)}%`;
}
