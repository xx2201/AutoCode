import { roundMoney } from "../shared/money";

export function applyCredits(amountDue: number, creditAmount: number): number {
  return roundMoney(amountDue - creditAmount);
}
