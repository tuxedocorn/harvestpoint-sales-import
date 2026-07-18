#!/usr/bin/env python3
"""
Harvestpoint Sales Order -> Smartsheet import

Pulls all sales orders with a ship date from N days ago through M days in
the future (default 5 days back, 60 days forward), then for each order
pulls the line-item detail (product/quantity/sellPrice) and writes one row
per line item to a Smartsheet sheet.

This is a WINDOWED repave, not a full one: only rows whose Ship Date falls
inside the rolling window (today - LOOKBACK_DAYS through today + LOOKAHEAD_DAYS)
get deleted and re-inserted each run. Rows older than the lookback window are
left untouched permanently -- they become a running historical archive that
never gets erased, while the live window stays fully fresh and accurate
(status changes, corrections in Harvestpoint, etc. all flow through for
anything still inside the window).

Auth: same Firebase Identity Toolkit email/password sign-in used by
harvestpoint-sync / timesheet-confirmation. Each run signs in fresh via
signInWithPassword and gets a short-lived idToken, used as a Bearer token
against appv2.harvestpointsoftware.com.

Required environment variables (set as GitHub Actions secrets):
    FIREBASE_API_KEY        Firebase web API key
    HARVESTPOINT_EMAIL      Harvestpoint login email
    HARVESTPOINT_PASS       Harvestpoint login password
    SMARTSHEET_TOKEN        Smartsheet API access token
    SMARTSHEET_SHEET_ID     Target Smartsheet sheet ID (optional, has default below)

Optional:
    LOOKBACK_DAYS            Override the default 5-day rolling-window lookback
    LOOKAHEAD_DAYS           Override the default 60-day forward window
    TEST_MODE                If "true", print results instead of writing to Smartsheet
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from dotenv import load_dotenv

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ORG_ID = "buffalopacking-2022"
HARVESTPOINT_API_BASE = "https://appv2.harvestpointsoftware.com/api"
MOUNTAIN = ZoneInfo("America/Denver")

FIREBASE_API_KEY   = os.getenv("FIREBASE_API_KEY")
HARVESTPOINT_EMAIL = os.getenv("HARVESTPOINT_EMAIL")
HARVESTPOINT_PASS  = os.getenv("HARVESTPOINT_PASS")

SMARTSHEET_TOKEN = os.getenv("SMARTSHEET_TOKEN")
DEFAULT_SHEET_ID = 7187353054433156  # "Sales Order Line Items" in Sweet Corn 2026 workspace
SHEET_ID = int(os.getenv("SMARTSHEET_SHEET_ID", DEFAULT_SHEET_ID))

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "5"))
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "60"))
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

# Column titles as created on the Smartsheet sheet -- must match exactly.
COLUMN_TITLES = [
    "Order Num",
    "Customer",
    "Status",
    "Ship Date",
    "Order Date",
    "Product",
    "Quantity",
    "Sell Price",
]

# The "consume" filter on the salesItem endpoint filters nested inventory
# sub-records, not the top-level line items -- so this same filter reliably
# returns all line items regardless of load/ship status.
SALES_ITEM_FILTER = json.dumps([
    {
        "applicableRepo": "inventoryRecord",
        "fieldName": "actionType",
        "operator": "[Op.eq]",
        "value": "consume",
    }
])


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Firebase auth
# ---------------------------------------------------------------------------

def get_firebase_token():
    """Sign in fresh via Firebase Identity Toolkit and return the idToken,
    used as a Bearer token against appv2.harvestpointsoftware.com."""
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    payload = {
        "email": HARVESTPOINT_EMAIL,
        "password": HARVESTPOINT_PASS,
        "returnSecureToken": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("idToken")
    print("\u2713 Firebase auth successful")
    return token


# ---------------------------------------------------------------------------
# Harvestpoint API calls
# ---------------------------------------------------------------------------

def fetch_orders(access_token, start_iso, end_iso):
    """Stage 1: pull all sales orders with shipDate in [start_iso, end_iso)."""
    url = f"{HARVESTPOINT_API_BASE}/{ORG_ID}/action/salesOrder/{start_iso}/{end_iso}/shipDate"
    headers = {"Authorization": f"Bearer {access_token}"}
    # NOTE: verify=False -- appv2.harvestpointsoftware.com serves an
    # incomplete certificate chain (missing intermediate CA). Browsers
    # tolerate this via automatic AIA fetching; requests/urllib3 does not.
    # Traffic is still encrypted; this only skips chain validation.
    resp = requests.get(url, headers=headers, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()


def fetch_line_items(access_token, order_id):
    """Stage 2: pull line items (product/quantity/sellPrice) for one order."""
    url = f"{HARVESTPOINT_API_BASE}/{ORG_ID}/object/salesOrder/{order_id}/salesItem"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        url,
        headers=headers,
        params={"filter": SALES_ITEM_FILTER},
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def iso_to_smartsheet_date(iso_str):
    """Convert a Harvestpoint ISO datetime string to YYYY-MM-DD for a Smartsheet
    DATE column, using Mountain time (the business's actual operating timezone)
    to determine the calendar date -- not raw UTC. Harvestpoint returns
    timestamps in UTC, so a late-evening Mountain time (e.g. 8pm on 7/20) can
    already be past midnight UTC (2am on 7/21). Taking .date() straight off the
    UTC value would incorrectly roll that date forward a day."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(MOUNTAIN).date().isoformat()
    except ValueError:
        return None


def build_rows(access_token, orders):
    """For every order, fetch its line items and flatten into row dicts."""
    rows = []
    total = len(orders)

    for i, order in enumerate(orders, start=1):
        order_id = order.get("id")
        order_num = order.get("orderNum")
        customer_name = (order.get("customer") or {}).get("name")
        status = order.get("status")
        ship_date = iso_to_smartsheet_date((order.get("shipDate") or {}).get("dateValue"))
        order_date = iso_to_smartsheet_date((order.get("orderDate") or {}).get("dateValue"))

        log(f"  [{i}/{total}] Order {order_num} ({order_id}) -- fetching line items...")

        try:
            line_items = fetch_line_items(access_token, order_id)
        except requests.HTTPError as e:
            log(f"    WARNING: failed to fetch line items for order {order_num}: {e}")
            continue

        if not line_items:
            log(f"    (no line items returned for order {order_num})")
            continue

        for item in line_items:
            product_name = (item.get("product") or {}).get("name")
            rows.append({
                "Order Num": order_num,
                "Customer": customer_name,
                "Status": status,
                "Ship Date": ship_date,
                "Order Date": order_date,
                "Product": product_name,
                "Quantity": item.get("quantity"),
                "Sell Price": item.get("sellPrice"),
            })

        # Light throttling to avoid hammering the API across potentially
        # hundreds of orders in a single run.
        time.sleep(0.1)

    return rows


# ---------------------------------------------------------------------------
# Smartsheet
# ---------------------------------------------------------------------------

class SmartsheetClient:
    BASE_URL = "https://api.smartsheet.com/2.0"

    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def get_columns(self, sheet_id):
        resp = self.session.get(f"{self.BASE_URL}/sheets/{sheet_id}/columns", timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]

    def get_all_rows(self, sheet_id):
        """Return full row data (id + cells) for every row currently on the sheet."""
        resp = self.session.get(f"{self.BASE_URL}/sheets/{sheet_id}", timeout=30)
        resp.raise_for_status()
        sheet = resp.json()
        return sheet.get("rows", [])

    def delete_rows(self, sheet_id, row_ids):
        # Smartsheet caps DELETE row batches; chunk conservatively.
        CHUNK = 400
        for i in range(0, len(row_ids), CHUNK):
            chunk = row_ids[i:i + CHUNK]
            ids_param = ",".join(str(r) for r in chunk)
            resp = self.session.delete(
                f"{self.BASE_URL}/sheets/{sheet_id}/rows",
                params={"ids": ids_param},
                timeout=30,
            )
            resp.raise_for_status()

    def add_rows(self, sheet_id, column_map, rows):
        # Smartsheet caps row-add batches at 500.
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            payload = []
            for row in chunk:
                cells = []
                for title, value in row.items():
                    if title not in column_map:
                        continue
                    if value is None:
                        continue
                    cells.append({"columnId": column_map[title], "value": value})
                payload.append({"toBottom": True, "cells": cells})

            resp = self.session.post(
                f"{self.BASE_URL}/sheets/{sheet_id}/rows",
                data=json.dumps(payload),
                timeout=60,
            )
            resp.raise_for_status()


def windowed_repave(smartsheet_token, sheet_id, rows, window_start_date, window_end_date):
    """Delete + re-insert only the rows whose Ship Date falls inside the
    rolling window [window_start_date, window_end_date]. Rows with an older
    Ship Date are left completely untouched -- they accumulate as a
    permanent historical archive."""
    client = SmartsheetClient(smartsheet_token)

    columns = client.get_columns(sheet_id)
    column_map = {col["title"]: col["id"] for col in columns}

    missing = [t for t in COLUMN_TITLES if t not in column_map]
    if missing:
        raise RuntimeError(f"Sheet is missing expected columns: {missing}")

    ship_date_col_id = column_map["Ship Date"]

    log("Fetching existing rows to determine which fall inside the rolling window...")
    existing_rows = client.get_all_rows(sheet_id)

    ids_to_delete = []
    for row in existing_rows:
        ship_date_val = None
        for cell in row.get("cells", []):
            if cell.get("columnId") == ship_date_col_id:
                ship_date_val = cell.get("value")
                break

        if not ship_date_val:
            # No parseable Ship Date -- leave it alone rather than risk
            # deleting something we can't classify.
            continue

        try:
            ship_date = datetime.fromisoformat(str(ship_date_val)).date()
        except ValueError:
            continue

        if window_start_date <= ship_date <= window_end_date:
            ids_to_delete.append(row["id"])

    if ids_to_delete:
        log(f"Deleting {len(ids_to_delete)} existing rows inside the rolling window "
            f"({window_start_date} to {window_end_date})...")
        client.delete_rows(sheet_id, ids_to_delete)
    else:
        log("No existing rows fall inside the rolling window -- nothing to delete.")

    if rows:
        log(f"Adding {len(rows)} fresh rows...")
        client.add_rows(sheet_id, column_map, rows)
    else:
        log("No rows to add.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*50}")
    print(f"Harvestpoint Sales Orders -> Smartsheet Sync")
    print(f"Rolling window: {LOOKBACK_DAYS} days back -> {LOOKAHEAD_DAYS} days forward")
    print(f"{'='*50}\n")

    if not FIREBASE_API_KEY or not HARVESTPOINT_EMAIL or not HARVESTPOINT_PASS:
        log("ERROR: FIREBASE_API_KEY, HARVESTPOINT_EMAIL, and HARVESTPOINT_PASS must be set.")
        sys.exit(1)
    if not TEST_MODE and not SMARTSHEET_TOKEN:
        log("ERROR: SMARTSHEET_TOKEN must be set (or run with TEST_MODE=true).")
        sys.exit(1)

    # Anchor everything to the START of today in Mountain time (the business's
    # actual operating timezone), not the exact moment the script happens to
    # run -- otherwise an order shipping earlier today could fall outside the
    # window depending on what time the script executes.
    today_mountain = datetime.now(MOUNTAIN).replace(hour=0, minute=0, second=0, microsecond=0)
    window_start_date = today_mountain.date() - timedelta(days=LOOKBACK_DAYS)
    window_end_date = today_mountain.date() + timedelta(days=LOOKAHEAD_DAYS)

    start_dt = today_mountain - timedelta(days=LOOKBACK_DAYS)
    start = start_dt.astimezone(timezone.utc)
    # Add a full day to the end date so the entire last calendar day is included.
    end_dt = today_mountain + timedelta(days=LOOKAHEAD_DAYS + 1)
    end = end_dt.astimezone(timezone.utc)

    # Harvestpoint's action/salesOrder endpoint expects millisecond-precision
    # ISO 8601 with a literal "Z" suffix, matching what the frontend sends.
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z"
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%S.") + f"{end.microsecond // 1000:03d}Z"

    access_token = get_firebase_token()

    log(f"Fetching orders with shipDate between {start_iso} and {end_iso}...")
    orders = fetch_orders(access_token, start_iso, end_iso)
    log(f"Found {len(orders)} orders in window.")

    rows = build_rows(access_token, orders)
    log(f"Built {len(rows)} line-item rows across {len(orders)} orders.")

    if TEST_MODE:
        log("TEST_MODE enabled -- printing rows instead of writing to Smartsheet:")
        for row in rows:
            print(json.dumps(row, default=str))
        return

    windowed_repave(SMARTSHEET_TOKEN, SHEET_ID, rows, window_start_date, window_end_date)
    log("\u2713 Sync complete!")


if __name__ == "__main__":
    main()
