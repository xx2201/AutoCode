// legacy 20 block 1: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 1: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 1: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 1: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 2: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 2: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 2: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 2: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 3: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 3: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 3: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 3: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 4: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 4: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 4: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 4: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 5: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 5: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 5: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 5: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 6: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 6: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 6: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 6: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 7: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 7: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 7: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 7: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 20 block 8: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 20 block 8: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 20 block 8: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 20 block 8: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

export interface LegacyRecord20 {
  code: string;
  weight: number;
  active: boolean;
  note: string;
}

export const legacy_20_snapshots: LegacyRecord20[] = [
  { code: "legacy-20-01", weight: 21, active: false, note: "historical-legacy-memo-01" },
  { code: "legacy-20-02", weight: 22, active: true, note: "historical-legacy-memo-02" },
  { code: "legacy-20-03", weight: 23, active: false, note: "historical-legacy-memo-03" },
  { code: "legacy-20-04", weight: 24, active: true, note: "historical-legacy-memo-04" },
  { code: "legacy-20-05", weight: 25, active: false, note: "historical-legacy-memo-05" },
  { code: "legacy-20-06", weight: 26, active: true, note: "historical-legacy-memo-06" },
  { code: "legacy-20-07", weight: 27, active: false, note: "historical-legacy-memo-07" },
  { code: "legacy-20-08", weight: 28, active: true, note: "historical-legacy-memo-08" },
  { code: "legacy-20-09", weight: 29, active: false, note: "historical-legacy-memo-09" },
  { code: "legacy-20-10", weight: 30, active: true, note: "historical-legacy-memo-10" },
  { code: "legacy-20-11", weight: 31, active: false, note: "historical-legacy-memo-11" },
  { code: "legacy-20-12", weight: 32, active: true, note: "historical-legacy-memo-12" }
];

export function describeLegacy20(): string {
  return legacy_20_snapshots
    .filter((row) => row.active)
    .map((row) => `${row.code}:${row.weight}:${row.note}`)
    .join('|');
}
