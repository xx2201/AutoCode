import { chooseBestDiscount, type DiscountCandidate } from "../pricing/loyalty-discount";
import { calculateCompoundTax } from "../tax/compound-tax-engine";
import { roundMoney } from "../shared/money";

export interface BillingPipelineInput {
  subtotal: number;
  discountCandidates: DiscountCandidate[];
  stateRate: number;
  localRate: number;
}

export interface BillingPipelineOutput {
  discountedSubtotal: number;
  totalTax: number;
  grandTotal: number;
}

export function runBillingPipeline(input: BillingPipelineInput): BillingPipelineOutput {
  const tax = calculateCompoundTax(input.subtotal, input.stateRate, input.localRate);
  const discount = chooseBestDiscount(input.discountCandidates);
  const discountedSubtotal = roundMoney(input.subtotal * (1 - discount.rate));
  return {
    discountedSubtotal,
    totalTax: tax.totalTax,
    grandTotal: roundMoney(discountedSubtotal + tax.totalTax),
  };
}
