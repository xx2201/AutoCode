import { roundMoney } from "../shared/money";

export interface TaxableLine {
  description: string;
  taxableAmount: number;
}

export function buildTaxLines(lines: TaxableLine[], taxRate: number): number {
  const totalTaxable = lines.reduce((sum, line) => sum + line.taxableAmount, 0);
  return roundMoney(totalTaxable * taxRate);
}
