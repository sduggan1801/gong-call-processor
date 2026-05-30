"""
Unit tests for core logic in main.py.

Run with: pytest tests/

These tests mock all external dependencies (Claude, Gong, Salesforce, Gmail)
so no real API keys or network access are needed.
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch

# Set mock flags before importing main
os.environ["GONG_MOCK"] = "true"
os.environ["SALESFORCE_MOCK"] = "true"
os.environ["GMAIL_MOCK"] = "true"
os.environ["ANTHROPIC_API_KEY"] = "sk-mock-key"

from main import redact_pii, is_usable_transcript, call_claude, _build_email_body


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

class TestRedactPII:
    def test_masks_credit_card(self):
        text = "Card number 4111 1111 1111 1111 was used"
        assert "[CREDIT_CARD]" in redact_pii(text)
        assert "4111" not in redact_pii(text)

    def test_masks_ssn(self):
        text = "SSN is 123-45-6789"
        assert "[SSN]" in redact_pii(text)
        assert "123-45-6789" not in redact_pii(text)

    def test_masks_phone(self):
        text = "Call me at 555-867-5309"
        assert "[PHONE]" in redact_pii(text)

    def test_leaves_clean_text_unchanged(self):
        text = "We're a SaaS company growing at 80% YoY with $8M ARR"
        assert redact_pii(text) == text


# ---------------------------------------------------------------------------
# Transcript quality gate
# ---------------------------------------------------------------------------

class TestTranscriptQuality:
    def test_rejects_short_transcript(self):
        assert not is_usable_transcript("Hi. Bye.")

    def test_accepts_normal_transcript(self):
        long_text = " ".join(["word"] * 100)
        assert is_usable_transcript(long_text)

    def test_boundary_exactly_50_words(self):
        text = " ".join(["word"] * 50)
        assert is_usable_transcript(text)

    def test_boundary_49_words(self):
        text = " ".join(["word"] * 49)
        assert not is_usable_transcript(text)


# ---------------------------------------------------------------------------
# Email body builder
# ---------------------------------------------------------------------------

class TestBuildEmailBody:
    def test_includes_summary(self):
        summary = "We discussed sales tax automation."
        body = _build_email_body(summary, [])
        assert summary in body

    def test_prospect_next_step_labeled_you(self):
        next_steps = [{"owner": "PROSPECT", "action": "Send contract", "due_date": "2025-07-01"}]
        body = _build_email_body("Summary.", next_steps)
        assert "You: Send contract" in body

    def test_ae_next_step_labeled_us(self):
        next_steps = [{"owner": "AE", "action": "Send proposal", "due_date": None}]
        body = _build_email_body("Summary.", next_steps)
        assert "Us: Send proposal" in body

    def test_no_next_steps(self):
        body = _build_email_body("Summary.", [])
        assert "Next steps:" in body  # header still present


# ---------------------------------------------------------------------------
# Claude integration (mocked)
# ---------------------------------------------------------------------------

MOCK_CLAUDE_RESPONSE = {
    "summary": "Acme Corp is evaluating Anrok to handle multi-state SaaS tax compliance.",
    "next_steps": [
        {"owner": "AE", "action": "Send tax advisor referrals", "due_date": "2025-06-05"},
        {"owner": "PROSPECT", "action": "Loop in CFO for next call", "due_date": None},
    ],
    "technical_questions": [],
    "deal_stage_signal": "PROGRESSING",
    "salesforce_notes": "Prospect at $8M ARR, 80% YoY growth, using Stripe. Nexus in ~8 states.",
    "requires_solutions_flag": False,
}


class TestCallClaude:
    @patch("main.anthropic.Anthropic")
    def test_returns_parsed_json(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text=json.dumps(MOCK_CLAUDE_RESPONSE))
        ]

        transcript = " ".join(["word"] * 100)
        result = call_claude(transcript, {"company_name": "Acme Corp", "call_date": "2025-06-01"})

        assert result["deal_stage_signal"] == "PROGRESSING"
        assert len(result["next_steps"]) == 2

    @patch("main.anthropic.Anthropic")
    def test_fallback_on_malformed_json(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text="This is not JSON at all.")
        ]

        result = call_claude(" ".join(["w"] * 100), {})
        assert result["deal_stage_signal"] == "UNCLEAR"
        assert "PARSE_ERROR" in result["summary"] or "FLAGGED" in result["salesforce_notes"]

    @patch("main.time.sleep")
    @patch("main.anthropic.Anthropic")
    def test_retries_on_rate_limit(self, mock_anthropic_cls, mock_sleep):
        import anthropic as _anthropic
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Fail twice, succeed third time
        mock_client.messages.create.side_effect = [
            _anthropic.RateLimitError("rate limit", response=MagicMock(status_code=429), body={}),
            _anthropic.RateLimitError("rate limit", response=MagicMock(status_code=429), body={}),
            MagicMock(content=[MagicMock(text=json.dumps(MOCK_CLAUDE_RESPONSE))]),
        ]

        result = call_claude(" ".join(["w"] * 100), {})
        assert mock_client.messages.create.call_count == 3
        assert result["deal_stage_signal"] == "PROGRESSING"
