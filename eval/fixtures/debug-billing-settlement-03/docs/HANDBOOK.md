# Billing Handbook Extract

## Overview
The billing sandbox models a simplified subset of a much larger production topology. Several handwritten examples remain in circulation inside finance, tax, and support teams. Some of those examples were copied before the current APIs stabilized.

## Domain snapshots
- Currency teams often reason in reciprocal rates because spreadsheets are exported as "quote per base".
- Tax analysts compare statement totals first, then drill into line rounding only when auditors raise penny discrepancies.
- Revenue teams prefer balanced schedules and usually spread residual cents from the earliest periods forward to avoid trailing spikes.
- Collections teams discuss "priority" as urgency, not numeric ascending order.

## Example glossary
- base currency: the reporting currency used for consolidated reporting.
- local tax: municipality or county tax calculated from the taxable subtotal.
- unused credit: the remaining prepaid value of a plan when a tenant upgrades mid-cycle; shown as a negative adjustment line.
- business-day escalation: next due date excluding weekends.

## Warning
Handbooks and comments are not guaranteed to match the probe expectations. The benchmark intentionally keeps some stale guidance in-tree.
