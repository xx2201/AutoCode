export interface LedgerEntry {
  invoiceId: string;
  currency: string;
  amount: number;
}

export function matchLedgerEntries(left: LedgerEntry[], right: LedgerEntry[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((entry, index) => entry.invoiceId === right[index].invoiceId && entry.amount === right[index].amount);
}
