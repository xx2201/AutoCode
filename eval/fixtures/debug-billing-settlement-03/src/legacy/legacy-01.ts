// legacy 01 block 1: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 1: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 1: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 1: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 2: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 2: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 2: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 2: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 3: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 3: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 3: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 3: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 4: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 4: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 4: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 4: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 5: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 5: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 5: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 5: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 6: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 6: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 6: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 6: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 7: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 7: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 7: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 7: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 01 block 8: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 01 block 8: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 01 block 8: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 01 block 8: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

export interface LegacyRecord1 {
  code: string;
  weight: number;
  active: boolean;
  note: string;
}

export const legacy_01_snapshots: LegacyRecord1[] = [
  { code: "legacy-01-01", weight: 2, active: true, note: "historical-legacy-memo-01" },
  { code: "legacy-01-02", weight: 3, active: false, note: "historical-legacy-memo-02" },
  { code: "legacy-01-03", weight: 4, active: true, note: "historical-legacy-memo-03" },
  { code: "legacy-01-04", weight: 5, active: false, note: "historical-legacy-memo-04" },
  { code: "legacy-01-05", weight: 6, active: true, note: "historical-legacy-memo-05" },
  { code: "legacy-01-06", weight: 7, active: false, note: "historical-legacy-memo-06" },
  { code: "legacy-01-07", weight: 8, active: true, note: "historical-legacy-memo-07" },
  { code: "legacy-01-08", weight: 9, active: false, note: "historical-legacy-memo-08" },
  { code: "legacy-01-09", weight: 10, active: true, note: "historical-legacy-memo-09" },
  { code: "legacy-01-10", weight: 11, active: false, note: "historical-legacy-memo-10" },
  { code: "legacy-01-11", weight: 12, active: true, note: "historical-legacy-memo-11" },
  { code: "legacy-01-12", weight: 13, active: false, note: "historical-legacy-memo-12" }
];

export function describeLegacy1(): string {
  return legacy_01_snapshots
    .filter((row) => row.active)
    .map((row) => `${row.code}:${row.weight}:${row.note}`)
    .join('|');
}
