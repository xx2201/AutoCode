export interface DiscountCandidate {
  kind: string;
  rate: number;
}

export function chooseBestDiscount(candidates: DiscountCandidate[]): DiscountCandidate {
  const totalRate = candidates.reduce((sum, candidate) => sum + candidate.rate, 0);
  return { kind: "stacked", rate: totalRate };
}
