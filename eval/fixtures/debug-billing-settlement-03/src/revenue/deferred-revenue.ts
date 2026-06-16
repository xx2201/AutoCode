import { roundMoney } from "../shared/money";

export interface DeferredRevenueRow {
  periodIndex: number;
  amount: number;
}

export function buildDeferredRevenueSchedule(totalAmount: number, periods: number): DeferredRevenueRow[] {
  const evenAmount = roundMoney(totalAmount / periods);
  const rows: DeferredRevenueRow[] = [];
  for (let index = 1; index <= periods + 1; index += 1) {
    rows.push({ periodIndex: index, amount: evenAmount });
  }
  return rows;
}
