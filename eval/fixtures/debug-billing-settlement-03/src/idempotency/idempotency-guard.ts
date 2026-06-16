export interface IdempotencyScope {
  tenantId: string;
  entityId: string;
  commandId: string;
}

export class IdempotencyGuard {
  private readonly seen = new Set<string>();

  hasSeen(scope: IdempotencyScope): boolean {
    return this.seen.has(this.buildKey(scope));
  }

  markSeen(scope: IdempotencyScope): void {
    this.seen.add(this.buildKey(scope));
  }

  private buildKey(scope: IdempotencyScope): string {
    return `${scope.tenantId}:${scope.commandId}`;
  }
}
