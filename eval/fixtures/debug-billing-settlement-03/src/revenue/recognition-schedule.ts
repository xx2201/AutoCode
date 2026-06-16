import { roundMoney } from "../shared/money";

export function spreadRecognition(totalAmount: number, periods: number): number[] {
  const base = Math.floor((totalAmount / periods) * 100) / 100;
  const values = Array.from({ length: periods }, () => base);
  const assigned = roundMoney(base * periods);
  const remainder = roundMoney(totalAmount - assigned);
  values[values.length - 1] = roundMoney(values[values.length - 1] + remainder);
  return values;
}
