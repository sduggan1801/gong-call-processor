# Gong Post-Call Processor

A GCP Cloud Function that triggers automatically after each Gong sales call, processes the transcript through Claude, and writes structured outputs to Salesforce and Gmail — with no manual work from the AE.

## What It Does

1. **Receives** a Pub/Sub message from the Cloud Run webhook receiver (which validated the Gong HMAC signature)
2. **Fetches** the call transcript from Gong's API
3. **Quality-gates** short/garbled transcripts — creates a manual-review Salesforce task instead of calling Claude
4. **Redacts PII** (credit cards, SSNs, phone numbers) before sending anything to Claude
5. **Calls Claude** to extract a structured JSON payload: call summary, next steps, technical questions, deal stage signal, and CRM notes
6. **Writes to Salesforce** — patches the Opportunity with notes and next steps
7. **Creates a Gmail draft** in the AE's mailbox — one click to review and send

Salesforce and Gmail writes are independent; a failure in one doesn't block the other.

---

## Project Structure

```
anrok_code/
├── main.py              # Cloud Function entrypoint
├── gong.py              # Gong API client
├── salesforce.py        # Salesforce REST client
├── gmail.py             # Gmail API client (domain-wide delegation)
├── test_main.py         # Unit tests (no real API keys needed)
├── run_local.py         # Local smoke test (uses real Claude, mocks everything else)
├── requirements.txt
└── README.md
```

---

## Local Development

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run the unit tests

No API keys needed — all external calls are mocked.

```bash
pytest test_main.py -v
```

### 3. Run an end-to-end smoke test

This uses the **real Claude API** and mocks Gong, Salesforce, and Gmail. Only `ANTHROPIC_API_KEY` is required.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python run_local.py
```

The mock flags (`GONG_MOCK`, `SALESFORCE_MOCK`, `GMAIL_MOCK`) are set automatically by `run_local.py` — no secrets needed for those services locally.

**Sample output:**

```
============================================================
Running Gong post-call processor (local mock mode)
============================================================
INFO:main:Processing call_id=CALL-001-MOCK for opportunity=0062x000ABC123
INFO:gong:GONG_MOCK=true — returning fixture transcript for call_id=CALL-001-MOCK
INFO:httpx:HTTP Request: POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"
INFO:main:Claude result for call_id=CALL-001-MOCK: stage_signal=PROGRESSING
INFO:salesforce:SALESFORCE_MOCK=true — would PATCH opportunity 0062x000ABC123 with: {
  "Call_Notes__c": "Strong initial call with Acme Corp (8M ARR, 80% YoY growth). Currently manual tax process across 15 states with nexus uncertainty in 8 states. Uses Stripe billing, concerned about Salesforce integration complexity. Timeline to go live before Q3, decision needed by end of June. CFO involvement secured for next call. Growth tier candidate, will need tax advisor referrals.",
  "Next_Steps__c": "[AE] Send follow-up with summary, tax advisor referrals, and meeting invite for CFO call — due 2025-06-02\n[PROSPECT] Loop in CFO for next call — due 2025-06-15",
  "Deal_Stage_Signal__c": "PROGRESSING"
}
INFO:main:Salesforce updated for opportunity=0062x000ABC123
INFO:gmail:GMAIL_MOCK=true — would create draft:
  From: alex@anrok.com
  To: jamie@acmecorp.com
  Subject: Follow-up: Acme Corp / 2025-06-01

Hi,

Thank you for the time today. Here's a quick summary of our conversation:

Initial discovery call with Acme Corp, an 8M ARR SaaS company manually handling sales tax
across 15 states. They have potential nexus issues in 8 states and need automated tax
calculation with Stripe integration. Timeline is to go live before Q3 with decision by end
of next month.

Next steps:
  • Us: Send follow-up with summary, tax advisor referrals, and meeting invite for CFO call (by 2025-06-02)
  • You: Loop in CFO for next call (by 2025-06-15)

Please let me know if anything looks off.

Best,
INFO:main:Gmail draft created for ae=alex@anrok.com
INFO:main:call_id=CALL-001-MOCK processed successfully
```

---

## Secrets

In production, all secrets live in **GCP Secret Manager**. Locally, the function falls back to environment variables when `GCP_PROJECT_ID` is unset.

| Secret Manager key             | Description                              |
|-------------------------------|------------------------------------------|
| `ANTHROPIC_API_KEY`            | Anthropic API key                        |
| `GONG_API_KEY`                 | Gong API key                             |
| `SALESFORCE_INSTANCE_URL`      | e.g. `https://anrok.my.salesforce.com`   |
| `SALESFORCE_ACCESS_TOKEN`      | Short-lived OAuth token (refreshed via Cloud Scheduler) |
| `GMAIL_SERVICE_ACCOUNT_KEY`    | JSON key for service account with domain-wide delegation |

---

## GCP Deployment

### Prerequisites

- GCP project with APIs enabled: Cloud Functions, Pub/Sub, Secret Manager
- Service account `gtm-processor-sa` with roles:
  - `secretmanager.secretAccessor`
  - `pubsub.subscriber`

### Deploy

```bash
gcloud functions deploy gong-call-processor \
  --gen2 \
  --runtime=python311 \
  --trigger-topic=gong-call-events \
  --entry-point=process_gong_call \
  --service-account=gtm-processor-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID \
  --region=us-central1 \
  --memory=512MB \
  --timeout=120s
```

The function is triggered by Pub/Sub, so it gets automatic retries (up to 7 days) if it crashes before acknowledging. Retry logic for Claude rate limits is also handled in-process (3 attempts, exponential backoff).

### Cloud Run (webhook receiver, separate service)

The Cloud Run service upstream of this function is responsible for:
- Validating the Gong HMAC-SHA256 signature
- Parsing the webhook and publishing the normalized payload to the `gong-call-events` Pub/Sub topic

That service is out of scope for this repo but is the expected entry point in the full architecture.

---

## Design Decisions

**Cloud Function (not Cloud Run) for transcript processing**
The processing job is event-driven, stateless, and runs in under 30 seconds. Cloud Functions with Pub/Sub trigger give us free retries and simpler ops vs. maintaining an always-on Cloud Run service.

**Independent downstream writes**
Salesforce and Gmail writes are wrapped in separate try/except blocks. A transient Gmail API failure doesn't roll back a successful CRM update. The function only raises if *both* writes fail.

**Transcript quality gate**
Transcripts under 50 words skip Claude entirely and create a manual-review Salesforce task. This avoids wasting API calls on dropped calls or connection tests while still surfacing them to the AE.

**PII redaction before Claude**
Credit card numbers, SSNs, and phone numbers are regex-masked before the transcript reaches the Anthropic API. Gong's own redaction runs first on their side; this is a belt-and-suspenders layer.

**Fallback on malformed JSON**
If Claude returns non-JSON, the raw output is truncated and written to Salesforce as a plain-text note flagged for cleanup. The function doesn't crash — it degrades gracefully.

**Mock mode skips secret fetches**
When `*_MOCK` env vars are set, the function skips calling Secret Manager for those clients entirely — no dummy secrets needed for local development.

---

## What I'd Add for a Production v2

- **Slack notification** to AE after Gmail draft is created (the "one click to send" UX in the design doc)
- **Solutions team Slack alert** when `requires_solutions_flag` is true
- **BigQuery logging** of every Claude call for prompt iteration and quality review