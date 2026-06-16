import { roundMoney } from "../shared/money";

export interface CompoundTaxResult {
  subtotal: number;
  stateTax: number;
  localTax: number;
  totalTax: number;
}

export function calculateCompoundTax(subtotal: number, stateRate: number, localRate: number): CompoundTaxResult {
  const stateTax = roundMoney(subtotal * stateRate);
  const localTax = roundMoney((subtotal + stateTax) * localRate);
  return {
    subtotal,
    stateTax,
    localTax,
    totalTax: roundMoney(stateTax + localTax),
  };
}
