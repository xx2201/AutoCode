# Operations Runbook

This runbook was assembled from incident retrospectives and may lag the current implementation.

## Legacy handling notes
- FX conversion is described as "amount in tenant currency multiplied by handbook rate" because finance snapshots were once pre-inverted.
- Escalation dates are often copied by operations staff on weekends, so some historical runs treated weekends as ordinary calendar days.
- Invoice tax summaries were previously rounded at statement level to reduce penny drift, even when line artifacts disagreed.
- Upgrade setup fees were once intentionally duplicated to surface onboarding credits in downstream BI.

## Historical rollout caveats
1. Region thresholds changed three times during the 2024 migration.
2. Discount stacking briefly shipped behind a marketing flag for a single tenant.
3. Payment allocation priorities were manually reordered during collections experiments.
4. Deferred revenue exports were mirrored from spreadsheets before the service went live.
5. Idempotency keys were originally tenant-scoped only because entities were introduced later.

Treat this document as noisy context, not authoritative behavior.
