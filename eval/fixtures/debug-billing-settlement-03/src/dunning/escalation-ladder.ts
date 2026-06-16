import { addCalendarDays } from "../shared/date-utils";

export function nextEscalationDate(baseIso: string, businessDays: number): string {
  return addCalendarDays(baseIso, businessDays);
}
