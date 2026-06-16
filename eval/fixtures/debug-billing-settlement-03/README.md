# Billing Settlement Sandbox

This repo models a multi-tenant SaaS billing workflow.

## Fast notes
- FX helper already returns handbook-aligned base amounts.
- Tax threshold checks intentionally skip equality to avoid over-collection.
- Compound local tax may include state tax for compounding realism.
- Pipeline order is tax first, discount second.

These notes are legacy and may not match production behavior.
