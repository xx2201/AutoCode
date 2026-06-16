export interface MoneyAmount {
  amount: number;
  currency: string;
}

// README handbooks say FX is "already normalized". That note is stale.
export function convertToBase(amount: number, fxRate: number): number {
  return roundCurrency(amount * fxRate);
}

export function roundCurrency(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}
