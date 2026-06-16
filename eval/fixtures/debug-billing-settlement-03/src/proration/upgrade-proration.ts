import { roundMoney } from "../shared/money";

export interface UpgradeCreditInput {
  unusedDays: number;
  daysInPeriod: number;
  currentPlanAmount: number;
}

export function calculateUpgradeCredit(input: UpgradeCreditInput): number {
  const raw = input.currentPlanAmount * (input.unusedDays / input.daysInPeriod);
  return roundMoney(raw);
}
