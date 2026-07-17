#!/usr/bin/env python3
"""
Harvestpoint Sales Order -> Smartsheet import

Pulls all sales orders with a ship date in the next N days (default 60),
then for each order pulls the line-item detail (product/quantity/sellPrice)
and writes one row per line item to a Smartsheet sheet. Each run fully
repaves the sheet (deletes all existing rows, inserts fresh rows) since
this is a point-in-time overview sourced entirely from Harvestpoint -- no
editing happens in Smartsheet itself.

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
    LOOKAHEAD_DAYS           Override the default 60-day forward window
    TEST_MODE                If "true", print results instead of writing to Smartsheet
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ORG_ID = "buffalopacking-2022"
HARVESTPOINT_API_BASE = "https://appv2.harvestpointsoftware.com/api"

FIREBASE_API_KEY   = os.getenv("FIREBASE_API_KEY")
HARVESTPOINT_EMAIL = os.getenv("HARVESTPOINT_EMAIL")
HARVESTPOINT_PASS  = os.getenv("HARVESTPOINT_PASS")

SMARTSHEET_TOKEN = os.getenv("SMARTSHEET_TOKEN")
DEFAULT_SHEET_ID = 7187353054433156  # "Sales Order Line Items" in Sweet Corn 2026 workspace
SHEET_ID = int(os.getenv("SMARTSHEET_SHEET_ID", DEFAULT_SHEET_ID))

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
    resp = requests.get(url, headers=headers, timeout=30)
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
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def iso_to_smartsheet_date(iso_str):
    """Convert a Harvestpoint ISO datetime string to YYYY-MM-DD for a Smartsheet DATE column."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.date().isoformat()
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

    def get_all_row_ids(self, sheet_id):
        resp = self.session.get(f"{self.BASE_URL}/sheets/{sheet_id}", timeout=30)
        resp.raise_for_status()
        sheet = resp.json()
        return [row["id"] for row in sheet.get("rows", [])]

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


def repave_sheet(smartsheet_token, sheet_id, rows):
    client = SmartsheetClient(smartsheet_token)

    columns = client.get_columns(sheet_id)
    column_map = {col["title"]: col["id"] for col in columns}

    missing = [t for t in COLUMN_TITLES if t not in column_map]
    if missing:
        raise RuntimeError(f"Sheet is missing expected columns: {missing}")

    log("Fetching existing row IDs for full repave...")
    existing_ids = client.get_all_row_ids(sheet_id)
    if existing_ids:
        log(f"Deleting {len(existing_ids)} existing rows...")
        client.delete_rows(sheet_id, existing_ids)

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
    print(f"Pulling orders with ship date in next {LOOKAHEAD_DAYS} days")
    print(f"{'='*50}\n")

    if not FIREBASE_API_KEY or not HARVESTPOINT_EMAIL or not HARVESTPOINT_PASS:
        log("ERROR: FIREBASE_API_KEY, HARVESTPOINT_EMAIL, and HARVESTPOINT_PASS must be set.")
        sys.exit(1)
    if not TEST_MODE and not SMARTSHEET_TOKEN:
        log("ERROR: SMARTSHEET_TOKEN must be set (or run with TEST_MODE=true).")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    start = now
    end = now + timedelta(days=LOOKAHEAD_DAYS)

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

    repave_sheet(SMARTSHEET_TOKEN, SHEET_ID, rows)
    log("\u2713 Sync complete!")


if __name__ == "__main__":
    main()
