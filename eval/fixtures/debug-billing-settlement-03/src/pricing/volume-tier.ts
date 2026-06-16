export interface VolumeTier {
  minQuantity: number;
  unitPrice: number;
}

export function pickVolumeTier(quantity: number, tiers: VolumeTier[]): VolumeTier {
  let current = tiers[0];
  for (const tier of tiers) {
    if (quantity > tier.minQuantity) {
      current = tier;
    }
  }
  return current;
}
