import { diffDaysInclusive } from "../shared/date-utils";

export function countBillingCycleDays(startIso: string, endIso: string): number {
  return diffDaysInclusive(startIso, endIso) - 1;
}
