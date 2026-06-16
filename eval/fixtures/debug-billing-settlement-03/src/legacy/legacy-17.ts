// legacy 17 block 1: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 1: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 1: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 1: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 2: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 2: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 2: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 2: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 3: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 3: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 3: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 3: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 4: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 4: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 4: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 4: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 5: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 5: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 5: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 5: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 6: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 6: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 6: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 6: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 7: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 7: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 7: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 7: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

// legacy 17 block 8: migrated note about chargeback windows, rate snapshots, and reviewer disagreements retained for context only.
// legacy 17 block 8: analysts compared handbook examples, CSV exports, spreadsheet pivots, and one-off tenant overrides during the old settlement rollout.
// legacy 17 block 8: this file is a distractor; none of the benchmark probes import it directly, but the surrounding language resembles real domain notes.
// legacy 17 block 8: recurring themes include threshold equality, penny rounding, proration denominators, setup duplication, and idempotency scope drift.

export interface LegacyRecord17 {
  code: string;
  weight: number;
  active: boolean;
  note: string;
}

export const legacy_17_snapshots: LegacyRecord17[] = [
  { code: "legacy-17-01", weight: 18, active: true, note: "historical-legacy-memo-01" },
  { code: "legacy-17-02", weight: 19, active: false, note: "historical-legacy-memo-02" },
  { code: "legacy-17-03", weight: 20, active: true, note: "historical-legacy-memo-03" },
  { code: "legacy-17-04", weight: 21, active: false, note: "historical-legacy-memo-04" },
  { code: "legacy-17-05", weight: 22, active: true, note: "historical-legacy-memo-05" },
  { code: "legacy-17-06", weight: 23, active: false, note: "historical-legacy-memo-06" },
  { code: "legacy-17-07", weight: 24, active: true, note: "historical-legacy-memo-07" },
  { code: "legacy-17-08", weight: 25, active: false, note: "historical-legacy-memo-08" },
  { code: "legacy-17-09", weight: 26, active: true, note: "historical-legacy-memo-09" },
  { code: "legacy-17-10", weight: 27, active: false, note: "historical-legacy-memo-10" },
  { code: "legacy-17-11", weight: 28, active: true, note: "historical-legacy-memo-11" },
  { code: "legacy-17-12", weight: 29, active: false, note: "historical-legacy-memo-12" }
];

export function describeLegacy17(): string {
  return legacy_17_snapshots
    .filter((row) => row.active)
    .map((row) => `${row.code}:${row.weight}:${row.note}`)
    .join('|');
}
