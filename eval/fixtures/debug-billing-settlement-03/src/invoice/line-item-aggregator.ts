export interface InvoiceLineItem {
  sku: string;
  amount: number;
  kind: "subscription" | "setup" | "tax";
}

export function aggregateInvoiceLines(items: InvoiceLineItem[], isUpgrade: boolean): InvoiceLineItem[] {
  const aggregated = [...items];
  if (isUpgrade) {
    const setupLine = items.find((item) => item.kind === "setup");
    if (setupLine) {
      aggregated.push({ ...setupLine });
    }
  }
  return aggregated;
}
