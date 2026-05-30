"""
Gong Post-Call Processor — Cloud Function (Python 3.11)

Triggered by Pub/Sub after Cloud Run validates and forwards
a Gong call-completed webhook. Fetches transcript, calls Claude,
then writes structured results to Salesforce and queues a Gmail draft.
"""

import base64
import json
import logging
import os
import re
import time
from typing import Any

import anthropic
import functions_framework
from google.cloud import secretmanager

from gong import GongClient
from salesforce import SalesforceClient
from gmail import GmailClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret loading (Secret Manager in prod; env vars locally / in tests)
# ---------------------------------------------------------------------------

_secret_cache: dict[str, str] = {}


def get_secret(name: str) -> str:
    """Fetch a secret from GCP Secret Manager, with in-process caching."""
    if name in _secret_cache:
        return _secret_cache[name]

    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        # Fallback to env var (local dev / tests)
        value = os.environ.get(name)
        if not value:
            raise EnvironmentError(f"Secret '{name}' not found in env or Secret Manager")
        return value

    client = secretmanager.SecretManagerServiceClient()
    secret_path = f"projects/{project_id}/secrets/{name}/versions/latest"
    response = client.access_secret_version(name=secret_path)
    value = response.payload.data.decode("utf-8").strip()
    _secret_cache[name] = value
    return value


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

_PII_PATTERNS = [
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "[CREDIT_CARD]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b\d{3}[- ]?\d{3}[- ]?\d{4}\b"), "[PHONE]"),
]


def redact_pii(text: str) -> str:
    """Apply regex-based PII masking before sending to Claude."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Claude integration
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 1024
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds


SYSTEM_PROMPT = """You are an AI assistant that helps sales teams at a B2B SaaS company.
You will receive a cleaned sales call transcript and metadata.
Your job is to extract structured information to automate post-call follow-up.

Always respond with valid JSON matching the schema below. No markdown, no preamble.

{
  "summary": "<2–4 sentence plain-English summary of the call for the prospect follow-up email>",
  "next_steps": [
    {"owner": "AE|PROSPECT|SOLUTIONS|LEGAL", "action": "<specific action>", "due_date": "<YYYY-MM-DD or null>"}
  ],
  "technical_questions": ["<verbatim question that requires Solutions team input>"],
  "deal_stage_signal": "PROGRESSING|STALLED|AT_RISK|UNCLEAR",
  "salesforce_notes": "<internal CRM note, 3–6 sentences, includes objections and key context>",
  "requires_solutions_flag": true|false
}

Rules:
- If next-step ownership is ambiguous, default to "AE".
- If the transcript is too short or garbled to extract meaningful signal, set all string fields
  to "TRANSCRIPT_INSUFFICIENT" and return an empty list for arrays.
- technical_questions should only include questions that genuinely require expert input;
  leave empty if none.
- deal_stage_signal must be one of the four values above.
"""


def call_claude(transcript: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """
    Send transcript + metadata to Claude and return parsed JSON.

    Retries on rate-limit errors with exponential backoff.
    Falls back gracefully if the response is not valid JSON.
    """
    api_key = get_secret("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"Call metadata:\n{json.dumps(metadata, indent=2)}\n\n"
        f"Transcript:\n{redact_pii(transcript)}"
    )

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = response.content[0].text
            return json.loads(raw)

        except anthropic.RateLimitError as exc:
            last_error = exc
            wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning("Claude rate limit hit (attempt %d/%d); retrying in %.1fs", attempt, MAX_RETRIES, wait)
            time.sleep(wait)

        except json.JSONDecodeError:
            # Malformed JSON — return a flagged fallback so Salesforce still gets a note
            logger.error("Claude returned non-JSON; using plain-text fallback")
            return {
                "summary": "PARSE_ERROR",
                "next_steps": [],
                "technical_questions": [],
                "deal_stage_signal": "UNCLEAR",
                "salesforce_notes": f"[AUTO-FLAGGED FOR CLEANUP] Raw Claude output:\n{raw[:2000]}",
                "requires_solutions_flag": False,
            }

        except anthropic.APIError as exc:
            last_error = exc
            logger.error("Claude API error on attempt %d: %s", attempt, exc)

    raise RuntimeError(f"Claude API failed after {MAX_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Transcript quality gate
# ---------------------------------------------------------------------------

MIN_TRANSCRIPT_WORDS = 50


def is_usable_transcript(transcript: str) -> bool:
    return len(transcript.split()) >= MIN_TRANSCRIPT_WORDS


# ---------------------------------------------------------------------------
# Cloud Function entrypoint
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def process_gong_call(cloud_event):  # noqa: ANN001
    """
    Entry point triggered by a Pub/Sub message.

    Expected Pub/Sub message data (base64-encoded JSON):
    {
        "call_id": "abc123",
        "opportunity_id": "0062x000...",
        "ae_email": "alex@anrok.com",
        "prospect_email": "buyer@example.com",
        "company_name": "Acme Corp",
        "call_date": "2025-06-01",
        "duration_minutes": 45
    }
    """
    # 1. Decode Pub/Sub payload
    raw_data = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
    event = json.loads(raw_data)
    logger.info("Processing call_id=%s for opportunity=%s", event.get("call_id"), event.get("opportunity_id"))

    call_id = event["call_id"]
    opportunity_id = event["opportunity_id"]
    ae_email = event["ae_email"]

    # 2. Fetch transcript from Gong
    gong_mock = os.environ.get("GONG_MOCK", "").lower() == "true"
    sf_mock = os.environ.get("SALESFORCE_MOCK", "").lower() == "true"
    gmail_mock = os.environ.get("GMAIL_MOCK", "").lower() == "true"

    gong = GongClient(api_key="" if gong_mock else get_secret("GONG_API_KEY"))
    transcript = gong.get_transcript(call_id)

    # 3. Quality gate — skip Claude for bad transcripts
    if not is_usable_transcript(transcript):
        logger.warning("Transcript too short for call_id=%s; creating manual-review task", call_id)
        sf = SalesforceClient(
            instance_url="" if sf_mock else get_secret("SALESFORCE_INSTANCE_URL"),
            access_token="" if sf_mock else get_secret("SALESFORCE_ACCESS_TOKEN"),
        )
        sf.create_task(
            opportunity_id=opportunity_id,
            subject="Manual review needed — transcript too short",
            owner_email=ae_email,
        )
        return

    # 4. Build metadata (only non-sensitive fields go to Claude)
    metadata = {
        "ae_name": event.get("ae_name", ae_email.split("@")[0]),
        "company_name": event["company_name"],
        "call_date": event["call_date"],
        "duration_minutes": event["duration_minutes"],
    }

    # 5. Call Claude
    result = call_claude(transcript, metadata)
    logger.info("Claude result for call_id=%s: stage_signal=%s", call_id, result.get("deal_stage_signal"))

    # 6. Write to Salesforce (independent of Gmail)
    sf_error: Exception | None = None
    try:
        sf = SalesforceClient(
            instance_url="" if sf_mock else get_secret("SALESFORCE_INSTANCE_URL"),
            access_token="" if sf_mock else get_secret("SALESFORCE_ACCESS_TOKEN"),
        )
        sf.update_opportunity(
            opportunity_id=opportunity_id,
            notes=result["salesforce_notes"],
            next_steps=result["next_steps"],
            stage_signal=result["deal_stage_signal"],
        )
        logger.info("Salesforce updated for opportunity=%s", opportunity_id)
    except Exception as exc:  # noqa: BLE001
        sf_error = exc
        logger.error("Salesforce write failed: %s", exc)

    # 7. Queue Gmail draft (independent of Salesforce)
    gmail_error: Exception | None = None
    try:
        gmail = GmailClient(service_account_key="" if gmail_mock else get_secret("GMAIL_SERVICE_ACCOUNT_KEY"))
        gmail.create_draft(
            sender=ae_email,
            to=event["prospect_email"],
            subject=f"Follow-up: {event['company_name']} / {event['call_date']}",
            body=_build_email_body(result["summary"], result["next_steps"]),
        )
        logger.info("Gmail draft created for ae=%s", ae_email)
    except Exception as exc:  # noqa: BLE001
        gmail_error = exc
        logger.error("Gmail draft failed: %s", exc)

    # 8. Surface errors — but don't fail the function if one downstream write succeeded
    if sf_error and gmail_error:
        raise RuntimeError(f"Both downstream writes failed. SF: {sf_error} | Gmail: {gmail_error}")

    logger.info("call_id=%s processed successfully", call_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_email_body(summary: str, next_steps: list[dict]) -> str:
    lines = [
        "Hi,",
        "",
        "Thank you for the time today. Here's a quick summary of our conversation:",
        "",
        summary,
        "",
        "Next steps:",
    ]
    for step in next_steps:
        owner_label = "You" if step["owner"] == "PROSPECT" else "Us"
        due = f" (by {step['due_date']})" if step.get("due_date") else ""
        lines.append(f"  • {owner_label}: {step['action']}{due}")

    lines += ["", "Please let me know if anything looks off.", "", "Best,"]
    return "\n".join(lines)
