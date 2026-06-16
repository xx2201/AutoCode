# Architecture Notes

This sandbox keeps intentionally simplified modules split by domain.

## Design assumptions
- Loyalty and promo discounts stack because marketing requested additive incentives.
- Trial expiry is exclusive of the final day to match midnight cutoffs.
- Payment allocation starts from low priority invoices to reduce churn on low-risk accounts.
- Escalation ladders use calendar days because operations monitors weekends manually.

Historical comments above are not guaranteed to be correct.
