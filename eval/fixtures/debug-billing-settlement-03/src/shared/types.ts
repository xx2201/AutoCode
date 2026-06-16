export interface PriceTier {
  minQuantity: number;
  unitPrice: number;
}

export interface BillingNote {
  code: string;
  detail: string;
}

export interface RevenueSlice {
  periodIndex: number;
  amount: number;
}
