"""
Gong API client.

In production: calls Gong's REST API using an API key.
In tests / local dev: GONG_MOCK=true returns fixture data.
"""

import json
import os
import time
import logging

import requests

logger = logging.getLogger(__name__)

_MOCK_TRANSCRIPT = """
Alex (Anrok AE): Thanks for joining today. Can you walk me through your current sales tax setup?

Jamie (Acme Corp): Sure. We're selling SaaS across 15 states right now and handling everything manually in spreadsheets. It's becoming a real pain as we scale.

Alex: Totally understandable. Are you currently registered in those states or still assessing nexus?

Jamie: We think we have nexus in about eight of them but honestly we're not sure. Our head of finance flagged it last quarter after we crossed some revenue thresholds.

Alex: That's exactly the situation Anrok is built for. We automatically calculate tax at checkout, handle registration recommendations, and sync everything to your billing system. What's your billing platform?

Jamie: We're on Stripe. We also use Salesforce for CRM and we're worried about the integration complexity.

Alex: We have a native Stripe integration — setup is usually under a day. On the Salesforce side, we'd sync tax exposure data directly. Can you share roughly what your ARR looks like so I can confirm which plan makes sense?

Jamie: We're at about eight million ARR and growing around 80% year over year.

Alex: Perfect — that puts you squarely in our growth tier. One thing to flag: do you have a tax advisor engaged? There are some retroactive exposure questions we'd want them involved on.

Jamie: We have a CPA firm but they don't specialize in SaaS tax. Is that something Anrok can help with?

Alex: We can refer you to a few firms we work with regularly. I'll include a couple of names in the follow-up. What's your timeline for a decision?

Jamie: We're trying to be live before Q3. So probably need to sign by end of next month.

Alex: That's very doable. I'll send over a summary and proposed next steps — can you loop in your CFO for the next call?

Jamie: Yes, I'll make that happen.
"""


class GongClient:
    BASE_URL = "https://api.gong.io/v2"
    TIMEOUT = 10  # seconds

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._mock = os.environ.get("GONG_MOCK", "false").lower() == "true"

    def get_transcript(self, call_id: str) -> str:
        """Return the full plain-text transcript for a call."""
        if self._mock:
            logger.info("GONG_MOCK=true — returning fixture transcript for call_id=%s", call_id)
            return _MOCK_TRANSCRIPT.strip()

        url = f"{self.BASE_URL}/calls/{call_id}/transcript"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # Flatten speaker turns into a single plain-text string
        turns = data.get("transcript", [])
        lines = [f"{turn['speaker']}: {turn['text']}" for turn in turns]
        return "\n".join(lines)
