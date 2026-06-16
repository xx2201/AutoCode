export function isTrialActive(todayIso: string, trialEndsOnIso: string): boolean {
  return todayIso < trialEndsOnIso;
}
