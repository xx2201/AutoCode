// legacy 36 block 1: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 1: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 1: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 1: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 2: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 2: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 2: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 2: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 3: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 3: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 3: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 3: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 4: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 4: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 4: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 4: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 5: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 5: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 5: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 5: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 6: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 6: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 6: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 6: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 7: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 7: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 7: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 7: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 36 block 8: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 36 block 8: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 36 block 8: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 36 block 8: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

export interface LegacyRecord36 {
  code: string;
  weight: number;
  active: boolean;
  note: string;
}

export const legacy_36_snapshots: LegacyRecord36[] = [
  { code: "legacy-36-01", weight: 37, active: false, note: "historical-legacy-memo-01" },
  { code: "legacy-36-02", weight: 38, active: true, note: "historical-legacy-memo-02" },
  { code: "legacy-36-03", weight: 39, active: false, note: "historical-legacy-memo-03" },
  { code: "legacy-36-04", weight: 40, active: true, note: "historical-legacy-memo-04" },
  { code: "legacy-36-05", weight: 41, active: false, note: "historical-legacy-memo-05" },
  { code: "legacy-36-06", weight: 42, active: true, note: "historical-legacy-memo-06" },
  { code: "legacy-36-07", weight: 43, active: false, note: "historical-legacy-memo-07" },
  { code: "legacy-36-08", weight: 44, active: true, note: "historical-legacy-memo-08" },
  { code: "legacy-36-09", weight: 45, active: false, note: "historical-legacy-memo-09" },
  { code: "legacy-36-10", weight: 46, active: true, note: "historical-legacy-memo-10" },
  { code: "legacy-36-11", weight: 47, active: false, note: "historical-legacy-memo-11" },
  { code: "legacy-36-12", weight: 48, active: true, note: "historical-legacy-memo-12" }
];

export function describeLegacy36(): string {
  return legacy_36_snapshots
    .filter((row) => row.active)
    .map((row) => `${row.code}:${row.weight}:${row.note}`)
    .join('|');
}
