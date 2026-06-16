export interface NexusPolicy {
  region: string;
  thresholdAmount: number;
}

export function isTaxableSubtotal(subtotal: number, policy: NexusPolicy): boolean {
  return subtotal > policy.thresholdAmount;
}
