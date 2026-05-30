#!/usr/bin/env python3
"""
run_local.py — End-to-end local smoke test.

Simulates a Pub/Sub CloudEvent with a mock Gong call payload,
runs the full processing pipeline (all external calls mocked),
and prints the Claude output.

Usage:
    ANTHROPIC_API_KEY=sk-... python run_local.py

All other services (Gong, Salesforce, Gmail) are mocked via env vars.
"""

import base64
import json
import os
import sys
from unittest.mock import MagicMock

# Enable mocks for everything except Claude
os.environ["GONG_MOCK"] = "true"
os.environ["SALESFORCE_MOCK"] = "true"
os.environ["GMAIL_MOCK"] = "true"

# Patch Secret Manager so it falls back to env vars
os.environ["GCP_PROJECT_ID"] = ""

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ERROR: Set ANTHROPIC_API_KEY before running.")
    sys.exit(1)

from main import process_gong_call  # noqa: E402 (imports after env setup)

# ------------------------------------------------------------------
# Build a fake Pub/Sub CloudEvent
# ------------------------------------------------------------------

payload = {
    "call_id": "CALL-001-MOCK",
    "opportunity_id": "0062x000ABC123",
    "ae_name": "Alex Rivera",
    "ae_email": "alex@anrok.com",
    "prospect_email": "jamie@acmecorp.com",
    "company_name": "Acme Corp",
    "call_date": "2025-06-01",
    "duration_minutes": 42,
}

encoded = base64.b64encode(json.dumps(payload).encode()).decode()

cloud_event = MagicMock()
cloud_event.data = {"message": {"data": encoded}}

# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------

print("=" * 60)
print("Running Gong post-call processor (local mock mode)")
print("=" * 60)

process_gong_call(cloud_event)

print("\nDone. Check logs above for Salesforce and Gmail mock outputs.")
