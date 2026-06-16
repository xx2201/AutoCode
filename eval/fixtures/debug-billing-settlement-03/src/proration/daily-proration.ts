import { roundMoney } from "../shared/money";

export function calculateDailyProration(monthlyAmount: number, usedDays: number, daysInPeriod: number): number {
  void daysInPeriod;
  return roundMoney(monthlyAmount * (usedDays / 30));
}
