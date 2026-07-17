# harvestpoint-sales-import

Pulls all Harvestpoint sales orders with a ship date in the next 60 days
(rolling window), then for each order pulls its line-item detail (product,
quantity, sell price) and writes one row per line item to a Smartsheet
sheet: **Sales Order Line Items** (Sweet Corn 2026 workspace, sheet ID
`7187353054433156`).

## Why two stages?

Harvestpoint's sales order report gives one row per *order*, with a single
summed `quantity` across all SKUs on that order -- not broken out by
product. To get a real per-product breakdown, each order has to be opened
individually to pull its line items. So the pipeline runs in two stages:

1. **Order list** -- `GET /api/{org}/action/salesOrder/{start}/{end}/shipDate`
   returns all orders with `shipDate` in the window, including `id`,
   `orderNum`, `status`, `customer.name`, `shipDate`, `orderDate`.
2. **Line items** -- for each order's `id`,
   `GET /api/{org}/object/salesOrder/{id}/salesItem?filter=...` returns an
   array of line items with `product.name`, `quantity`, `sellPrice`.

Every run **fully repaves** the Smartsheet sheet (deletes all rows, inserts
fresh ones) since this is a read-only overview sourced from Harvestpoint --
no editing happens directly in Smartsheet.

## Columns

`Order Num | Customer | Status | Ship Date | Order Date | Product | Quantity | Sell Price`

Pulling `status` as its own column (rather than filtering it out) is
intentional -- it lets you slice the sheet after the fact (e.g. a "Loading"
view for what's on trucks today) without needing separate pipelines per
status.

## Auth

Same Firebase Identity Toolkit email/password sign-in as `harvestpoint-sync`
/ `timesheet-confirmation`. Each run signs in fresh via `signInWithPassword`
and gets a short-lived `idToken`, used as a Bearer token against
`appv2.harvestpointsoftware.com`.

## Required secrets

- `FIREBASE_API_KEY` -- Firebase web API key
- `HARVESTPOINT_EMAIL` -- Harvestpoint login email
- `HARVESTPOINT_PASS` -- Harvestpoint login password
- `SMARTSHEET_TOKEN` -- Smartsheet API access token
- `SMARTSHEET_SHEET_ID` -- (optional) overrides the default sheet ID hardcoded in the script

## Schedule

Runs every 4 hours via GitHub Actions (`.github/workflows/sync.yml`), plus
manual `workflow_dispatch` with optional `test_mode` and `lookahead_days`
overrides.

## Local / manual test run

\`\`\`bash
export FIREBASE_API_KEY=...
export HARVESTPOINT_EMAIL=...
export HARVESTPOINT_PASS=...
export TEST_MODE=true
python sales_order_import.py
\`\`\`

`TEST_MODE=true` prints the rows it would write instead of touching
Smartsheet, so you can sanity-check output without a `SMARTSHEET_TOKEN`.
