#!/usr/bin/env python3
"""
One-time backfill: populate "Qty Shipped" for existing rows in the Sales
Order Line Items sheet that predate the column's introduction.

Those rows sit outside the pipeline's rolling live window (older than
LOOKBACK_DAYS) and will never be touched by the regular sync again -- the
regular sync only deletes/re-inserts rows inside the rolling window. This
script instead UPDATES rows in place (no delete/insert), so it's safe to
run without disturbing anything else on the sheet.

Run once. Safe to re-run -- it only targets rows currently missing a
Qty Shipped value, so already-backfilled rows are skipped automatically.

Requires the same environment variables as sales_order_import.py:
    FIREBASE_API_KEY, HARVESTPOINT_EMAIL, HARVESTPOINT_PASS, SMARTSHEET_TOKEN
"""

import json
from datetime import datetime

import requests

from sales_order_import import (
    get_firebase_token,
    fetch_orders,
    fetch_line_items,
    SmartsheetClient,
    SMARTSHEET_TOKEN,
    SHEET_ID,
    log,
)


def compute_qty_shipped(item):
    """Same logic as the main sync: sum abs(quantity) across nested
    inventory[] records with actionType == 'consume'."""
    return sum(
        abs(inv.get("quantity") or 0)
        for inv in (item.get("inventory") or [])
        if inv.get("actionType") == "consume"
    )


def main():
    client = SmartsheetClient(SMARTSHEET_TOKEN)
    columns = client.get_columns(SHEET_ID)
    column_map = {col["title"]: col["id"] for col in columns}

    qty_shipped_col_id = column_map["Qty Shipped"]
    order_num_col_id = column_map["Order Num"]
    product_col_id = column_map["Product"]
    ship_date_col_id = column_map["Ship Date"]

    log("Fetching all existing rows from the sheet...")
    all_rows = client.get_all_rows(SHEET_ID)

    # Smartsheet's raw API includes a cell entry for every column on every
    # row, even blank ones -- just with no "value" key inside it. So we
    # check whether the VALUE is None, not whether the column ID merely
    # appears in the cell list (it always will).
    rows_needing_backfill = []
    for row in all_rows:
        cells_by_col = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        if cells_by_col.get(qty_shipped_col_id) is None:
            rows_needing_backfill.append(row)

    log(f"Found {len(rows_needing_backfill)} rows missing Qty Shipped.")
    if not rows_needing_backfill:
        log("Nothing to backfill -- done.")
        return

    # Figure out the date range these rows span so we can pull historical
    # orders in one efficient Stage 1 call rather than one call per order.
    ship_dates = []
    for row in rows_needing_backfill:
        cells_by_col = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        sd = cells_by_col.get(ship_date_col_id)
        if sd:
            ship_dates.append(datetime.fromisoformat(str(sd)).date())

    if not ship_dates:
        log("None of the rows needing backfill have a parseable Ship Date -- aborting.")
        return

    earliest = min(ship_dates)
    latest = max(ship_dates)
    log(f"Rows needing backfill span ship dates {earliest} through {latest}.")

    start_iso = f"{earliest.isoformat()}T00:00:00.000Z"
    end_iso = f"{latest.isoformat()}T23:59:59.999Z"

    access_token = get_firebase_token()

    log(f"Fetching historical orders between {start_iso} and {end_iso}...")
    orders = fetch_orders(access_token, start_iso, end_iso)
    log(f"Found {len(orders)} historical orders in that range.")

    # Build a (orderNum, productName) -> qty_shipped lookup from fresh
    # Harvestpoint data.
    lookup = {}
    for i, order in enumerate(orders, start=1):
        order_num = order.get("orderNum")
        order_id = order.get("id")
        log(f"  [{i}/{len(orders)}] Order {order_num} -- fetching line items...")
        try:
            line_items = fetch_line_items(access_token, order_id)
        except requests.HTTPError as e:
            log(f"    WARNING: failed to fetch line items for order {order_num}: {e}")
            continue
        for item in line_items:
            product_name = (item.get("product") or {}).get("name")
            lookup[(order_num, product_name)] = compute_qty_shipped(item)

    # Match each row needing backfill to the lookup and build the update payload.
    updates = []
    unmatched = 0
    for row in rows_needing_backfill:
        cells_by_col = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        order_num = cells_by_col.get(order_num_col_id)
        product_name = cells_by_col.get(product_col_id)
        key = (order_num, product_name)
        if key in lookup:
            updates.append({
                "id": row["id"],
                "cells": [{"columnId": qty_shipped_col_id, "value": lookup[key]}],
            })
        else:
            unmatched += 1
            log(f"  No match found for Order {order_num} / {product_name} -- skipping.")

    log(f"Matched {len(updates)} rows to update; {unmatched} unmatched.")
    if not updates:
        log("Nothing to update -- done.")
        return

    CHUNK = 500
    for i in range(0, len(updates), CHUNK):
        chunk = updates[i:i + CHUNK]
        resp = client.session.put(
            f"{client.BASE_URL}/sheets/{SHEET_ID}/rows",
            data=json.dumps(chunk),
            timeout=60,
        )
        resp.raise_for_status()

    log(f"\u2713 Backfill complete -- updated {len(updates)} rows.")


if __name__ == "__main__":
    main()
