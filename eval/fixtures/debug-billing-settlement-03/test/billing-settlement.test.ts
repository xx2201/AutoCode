import { describe, expect, it } from "vitest";
import { convertToBase } from "../src/currency/currency-converter";
import { isTaxableSubtotal } from "../src/tax/jurisdiction-resolver";
import { calculateCompoundTax } from "../src/tax/compound-tax-engine";
import { pickVolumeTier } from "../src/pricing/volume-tier";
import { chooseBestDiscount } from "../src/pricing/loyalty-discount";
import { calculateDailyProration } from "../src/proration/daily-proration";
import { calculateUpgradeCredit } from "../src/proration/upgrade-proration";
import { countBillingCycleDays } from "../src/subscription/billing-cycle";
import { isTrialActive } from "../src/subscription/trial-manager";
import { aggregateInvoiceLines } from "../src/invoice/line-item-aggregator";
import { buildTaxLines } from "../src/invoice/tax-line-builder";
import { allocatePayment } from "../src/payment/allocation-engine";
import { applyCredits } from "../src/credit/credit-applicator";
import { buildDeferredRevenueSchedule } from "../src/revenue/deferred-revenue";
import { spreadRecognition } from "../src/revenue/recognition-schedule";
import { matchLedgerEntries } from "../src/reconciliation/ledger-matcher";
import { nextEscalationDate } from "../src/dunning/escalation-ladder";
import { IdempotencyGuard } from "../src/idempotency/idempotency-guard";
import { runBillingPipeline } from "../src/pipeline/billing-orchestrator";

describe("billing settlement regression probes", () => {
  it("convertToBase follows handbook direction", () => {
    expect(convertToBase(120, 1.2)).toBe(100);
  });

  it("tax threshold is inclusive", () => {
    expect(isTaxableSubtotal(100, { region: "CA", thresholdAmount: 100 })).toBe(true);
  });

  it("compound local tax uses subtotal only", () => {
    expect(calculateCompoundTax(100, 0.05, 0.02)).toEqual({
      subtotal: 100,
      stateTax: 5,
      localTax: 2,
      totalTax: 7,
    });
  });

  it("top pricing tier matches exact minimum quantity", () => {
    const tier = pickVolumeTier(100, [
      { minQuantity: 1, unitPrice: 10 },
      { minQuantity: 10, unitPrice: 9 },
      { minQuantity: 100, unitPrice: 7 },
    ]);
    expect(tier.unitPrice).toBe(7);
  });

  it("discount engine selects best single incentive", () => {
    expect(chooseBestDiscount([
      { kind: "loyalty", rate: 0.1 },
      { kind: "promo", rate: 0.15 },
    ])).toEqual({ kind: "promo", rate: 0.15 });
  });

  it("daily proration uses actual days in billing period", () => {
    expect(calculateDailyProration(310, 15, 31)).toBe(150);
  });

  it("unused upgrade credit is negative", () => {
    expect(calculateUpgradeCredit({ unusedDays: 10, daysInPeriod: 20, currentPlanAmount: 100 })).toBe(-50);
  });

  it("billing cycle counts the last day", () => {
    expect(countBillingCycleDays("2026-01-01", "2026-01-31")).toBe(31);
  });

  it("trial is active on its final day", () => {
    expect(isTrialActive("2026-01-31", "2026-01-31")).toBe(true);
  });

  it("upgrade aggregation does not duplicate setup fee", () => {
    const lines = aggregateInvoiceLines([
      { sku: "base", amount: 100, kind: "subscription" },
      { sku: "setup", amount: 30, kind: "setup" },
    ], true);
    expect(lines.filter((line) => line.kind === "setup")).toHaveLength(1);
  });

  it("tax builder rounds line-by-line before summing", () => {
    expect(buildTaxLines([
      { description: "A", taxableAmount: 0.15 },
      { description: "B", taxableAmount: 0.15 },
    ], 0.1)).toBe(0.04);
  });

  it("payments allocate to high priority invoices first", () => {
    expect(allocatePayment(90, [
      { invoiceId: "low", priority: 1, amountDue: 50 },
      { invoiceId: "high", priority: 9, amountDue: 80 },
    ])).toEqual([
      { invoiceId: "high", amount: 80 },
      { invoiceId: "low", amount: 10 },
    ]);
  });

  it("credit application floors amount due at zero", () => {
    expect(applyCredits(40, 60)).toBe(0);
  });

  it("deferred revenue schedule has one row per period", () => {
    expect(buildDeferredRevenueSchedule(120, 3)).toHaveLength(3);
  });

  it("recognition cents distribute from the front, not all to the final period", () => {
    expect(spreadRecognition(10.02, 4)).toEqual([2.51, 2.51, 2.5, 2.5]);
  });

  it("ledger matching respects currency", () => {
    expect(matchLedgerEntries(
      [{ invoiceId: "INV-1", currency: "USD", amount: 10 }],
      [{ invoiceId: "INV-1", currency: "EUR", amount: 10 }],
    )).toBe(false);
  });

  it("escalation ladder counts business days", () => {
    expect(nextEscalationDate("2026-01-02", 1)).toBe("2026-01-05");
  });

  it("idempotency key includes entity id", () => {
    const guard = new IdempotencyGuard();
    guard.markSeen({ tenantId: "t1", entityId: "north", commandId: "cmd-1" });
    expect(guard.hasSeen({ tenantId: "t1", entityId: "south", commandId: "cmd-1" })).toBe(false);
  });

  it("billing pipeline applies discount before tax", () => {
    expect(runBillingPipeline({
      subtotal: 100,
      discountCandidates: [{ kind: "promo", rate: 0.1 }],
      stateRate: 0.1,
      localRate: 0,
    })).toEqual({
      discountedSubtotal: 90,
      totalTax: 9,
      grandTotal: 99,
    });
  });
});
