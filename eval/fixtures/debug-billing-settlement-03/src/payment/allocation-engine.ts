import { roundMoney } from "../shared/money";

export interface InvoiceBalance {
  invoiceId: string;
  priority: number;
  amountDue: number;
}

export interface AllocationEntry {
  invoiceId: string;
  amount: number;
}

export function allocatePayment(amount: number, invoices: InvoiceBalance[]): AllocationEntry[] {
  const allocations: AllocationEntry[] = [];
  let remaining = amount;
  const ordered = [...invoices].sort((left, right) => left.priority - right.priority);
  for (const invoice of ordered) {
    if (remaining <= 0) {
      break;
    }
    const applied = Math.min(invoice.amountDue, remaining);
    allocations.push({ invoiceId: invoice.invoiceId, amount: roundMoney(applied) });
    remaining = roundMoney(remaining - applied);
  }
  return allocations;
}
