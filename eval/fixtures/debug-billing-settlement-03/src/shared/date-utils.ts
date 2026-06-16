const dayMs = 24 * 60 * 60 * 1000;

export function diffDaysInclusive(startIso: string, endIso: string): number {
  const start = new Date(`${startIso}T00:00:00Z`);
  const end = new Date(`${endIso}T00:00:00Z`);
  return Math.round((end.getTime() - start.getTime()) / dayMs) + 1;
}

export function addCalendarDays(baseIso: string, days: number): string {
  const base = new Date(`${baseIso}T00:00:00Z`);
  base.setUTCDate(base.getUTCDate() + days);
  return base.toISOString().slice(0, 10);
}

export function addBusinessDays(baseIso: string, days: number): string {
  let current = new Date(`${baseIso}T00:00:00Z`);
  let remaining = days;
  while (remaining > 0) {
    current.setUTCDate(current.getUTCDate() + 1);
    const weekday = current.getUTCDay();
    if (weekday !== 0 && weekday !== 6) {
      remaining -= 1;
    }
  }
  return current.toISOString().slice(0, 10);
}
