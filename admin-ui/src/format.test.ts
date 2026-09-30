import { describe, expect, it } from "vitest";
import { formatCompact, formatDecimal, formatNumber, formatRatioPercent, formatSignedPercent, formatUsd } from "./format";

describe("number presentation", () => {
  it("pins money to $1,234.56 regardless of browser locale", () => {
    expect(formatUsd(1234.56)).toBe("$1,234.56");
    expect(formatUsd(1234567.891)).toBe("$1,234,567.89");
    expect(formatUsd(0)).toBe("$0.00");
    // Sub-cent ledger precision is not surfaced: two decimals, always.
    expect(formatUsd(0.000864)).toBe("$0.00");
    expect(formatUsd(0.005)).toBe("$0.01");
    expect(formatUsd(-3.5)).toBe("$-3.50");
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
