"""
BigQuery logger for Claude API calls.

Every call to Claude is streaming-inserted here for prompt iteration
and quality review. In local/mock mode (no GCP_PROJECT_ID), rows are
printed to stdout instead.

Table DDL (run once to create the destination table):

    CREATE TABLE IF NOT EXISTS `<project>.claude_logs.claude_calls` (
        logged_at       TIMESTAMP   NOT NULL,
        call_id         STRING      NOT NULL,
        opportunity_id  STRING,
        model           STRING      NOT NULL,
        system_prompt   STRING      NOT NULL,
        user_message    STRING      NOT NULL,
        raw_response    STRING,
        input_tokens    INT64,
        output_tokens   INT64,
        latency_ms      INT64,
        success         BOOL        NOT NULL,
        error_type      STRING,
        deal_stage_signal STRING,
        parse_error     BOOL        NOT NULL,
        attempt_count   INT64       NOT NULL
    )
    PARTITION BY DATE(logged_at)
    OPTIONS (partition_expiration_days = 365);

Configure via env vars:
    BQ_PROJECT_ID  (default: falls back to GCP_PROJECT_ID)
    BQ_DATASET     (default: claude_logs)
    BQ_TABLE       (default: claude_calls)
    BQ_MOCK        set to "true" to skip all BQ writes (e.g. in tests)
"""

import datetime
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_bq_client = None
_bq_table_ref: str | None = None


def _bq_project() -> str | None:
    return os.environ.get("BQ_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID") or None


def _get_bq() -> tuple[Any, str]:
    global _bq_client, _bq_table_ref
    if _bq_client is None:
        from google.cloud import bigquery  # lazy import — not needed in mock mode

        project = _bq_project()
        dataset = os.environ.get("BQ_DATASET", "claude_logs")
        table = os.environ.get("BQ_TABLE", "claude_calls")
        _bq_client = bigquery.Client(project=project)
        _bq_table_ref = f"{project}.{dataset}.{table}"
    return _bq_client, _bq_table_ref


def log_claude_call(
    *,
    call_id: str,
    opportunity_id: str | None,
    model: str,
    system_prompt: str,
    user_message: str,
    raw_response: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    success: bool,
    error_type: str | None,
    deal_stage_signal: str | None,
    parse_error: bool,
    attempt_count: int,
) -> None:
    row = {
        "logged_at": datetime.datetime.utcnow().isoformat() + "Z",
        "call_id": call_id,
        "opportunity_id": opportunity_id,
        "model": model,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "raw_response": raw_response,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "success": success,
        "error_type": error_type,
        "deal_stage_signal": deal_stage_signal,
        "parse_error": parse_error,
        "attempt_count": attempt_count,
    }

    if os.environ.get("BQ_MOCK", "").lower() == "true" or not _bq_project():
        logger.info("[BQ_DRY_RUN] %s", json.dumps(row, default=str))
        return

    try:
        client, table_ref = _get_bq()
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            logger.error("BigQuery insert errors (non-fatal): %s", errors)
        else:
            logger.debug("BigQuery log written for call_id=%s", call_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("BigQuery logging failed (non-fatal): %s", exc)
