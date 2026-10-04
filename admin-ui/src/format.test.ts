import { describe, expect, it } from "vitest";
import { formatCompact, formatDecimal, formatNumber, formatRatioPercent, formatSignedPercent, formatUsd, formatUtcTimestamp } from "./format";

describe("number presentation", () => {
  it("pins money to $1,234.56 regardless of browser locale", () => {
    expect(formatUsd(1234.56)).toBe("$1,234.56");
    expect(formatUsd(1234567.891)).toBe("$1,234,567.89");
    expect(formatUsd(0)).toBe("$0.00");
    // Sub-cent ledger precision is not surfaced: two decimals, always.
    expect(formatUsd(0.000864)).toBe("$0.00");
    expect(formatUsd(0.005)).toBe("$0.01");
  });

  it("puts the sign ahead of the currency symbol for negative deltas", () => {
    expect(formatUsd(-3.5)).toBe("-$3.50");
    expect(formatUsd(-1.23)).toBe("-$1.23");
    expect(formatUsd(-1234.5)).toBe("-$1,234.50");
    // A delta that rounds to zero is not shown as negative.
    expect(formatUsd(-0.001)).toBe("$0.00");
  });

  it("groups integers with commas and never shows decimals", () => {
    expect(formatNumber(0)).toBe("0");
    expect(formatNumber(999)).toBe("999");
    expect(formatNumber(1000)).toBe("1,000");
    expect(formatNumber(31_000_000)).toBe("31,000,000");
    expect(formatNumber(12.7)).toBe("13");
  });

  it("abbreviates token counts from a thousand upwards", () => {
    expect(formatCompact(999)).toBe("999");
    expect(formatCompact(1_000)).toBe("1K");
    expect(formatCompact(21_600_000)).toBe("21.6M");
    expect(formatCompact(4_100_000)).toBe("4.1M");
    expect(formatCompact(2_500_000_000)).toBe("2.5B");
  });

  it("rolls over to the next unit instead of printing 1,000K", () => {
    expect(formatCompact(999_950)).toBe("1M");
    expect(formatCompact(999_949)).toBe("999.9K");
    expect(formatCompact(950_000)).toBe("950K");
    expect(formatCompact(999_950_000)).toBe("1B");
    expect(formatCompact(999_950_000_000)).toBe("1T");
    expect(formatCompact(-999_950)).toBe("-1M");
  });

  it("renders calendar boundaries in UTC with an explicit suffix", () => {
    expect(formatUtcTimestamp("2026-09-03T00:00:00+00:00")).toBe("2026-09-03 00:00 UTC");
    // A zoned input is normalised to UTC rather than to the browser zone.
    expect(formatUtcTimestamp("2026-09-02T20:30:00-04:00")).toBe("2026-09-03 00:30 UTC");
    expect(formatUtcTimestamp(null)).toBe("Not available");
    expect(formatUtcTimestamp("not a date")).toBe("Invalid timestamp");
  });

  it("formats decimals and percentages with a point", () => {
    expect(formatDecimal(1.234)).toBe("1.23");
    expect(formatDecimal(2)).toBe("2");
    expect(formatSignedPercent(9.091)).toBe("+9.09%");
    expect(formatSignedPercent(-868.4)).toBe("-868.40%");
    expect(formatSignedPercent(0)).toBe("0.00%");
    expect(formatSignedPercent(null)).toBe("n/a (no activity)");
    expect(formatRatioPercent(0.8)).toBe("80%");
    expect(formatRatioPercent(0.125)).toBe("12.5%");
  });
});
