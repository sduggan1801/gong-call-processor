"""
Salesforce REST API client (minimal surface for this function).

Handles opportunity updates and task creation.
Set SALESFORCE_MOCK=true for local dev / tests.
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)


class SalesforceClient:
    TIMEOUT = 10

    def __init__(self, instance_url: str, access_token: str) -> None:
        self._instance_url = instance_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._mock = os.environ.get("SALESFORCE_MOCK", "false").lower() == "true"

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def update_opportunity(
        self,
        opportunity_id: str,
        notes: str,
        next_steps: list[dict],
        stage_signal: str,
    ) -> None:
        """Patch the Opportunity record with call notes and next steps."""
        next_steps_text = "\n".join(
            f"[{s['owner']}] {s['action']}" + (f" — due {s['due_date']}" if s.get("due_date") else "")
            for s in next_steps
        )

        payload = {
            "Call_Notes__c": notes,
            "Next_Steps__c": next_steps_text,
            "Deal_Stage_Signal__c": stage_signal,
        }

        if self._mock:
            logger.info(
                "SALESFORCE_MOCK=true — would PATCH opportunity %s with: %s",
                opportunity_id,
                json.dumps(payload, indent=2),
            )
            return

        url = f"{self._instance_url}/services/data/v58.0/sobjects/Opportunity/{opportunity_id}"
        resp = requests.patch(url, headers=self._headers, json=payload, timeout=self.TIMEOUT)
        resp.raise_for_status()
        logger.info("Opportunity %s updated", opportunity_id)

    def create_task(self, opportunity_id: str, subject: str, owner_email: str) -> None:
        """Create a follow-up Task linked to an Opportunity."""
        payload = {
            "WhatId": opportunity_id,
            "Subject": subject,
            "Status": "Not Started",
            "OwnerId": self._resolve_user_id(owner_email),
        }

        if self._mock:
            logger.info(
                "SALESFORCE_MOCK=true — would create Task for opportunity %s: %s",
                opportunity_id,
                json.dumps(payload, indent=2),
            )
            return

        url = f"{self._instance_url}/services/data/v58.0/sobjects/Task"
        resp = requests.post(url, headers=self._headers, json=payload, timeout=self.TIMEOUT)
        resp.raise_for_status()
        logger.info("Task created for opportunity %s", opportunity_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_user_id(self, email: str) -> str:
        """Look up a Salesforce User ID by email. Falls back to a placeholder."""
        if self._mock:
            return "005MOCKUSERID"

        url = f"{self._instance_url}/services/data/v58.0/query"
        params = {"q": f"SELECT Id FROM User WHERE Email = '{email}' LIMIT 1"}
        resp = requests.get(url, headers=self._headers, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if records:
            return records[0]["Id"]
        logger.warning("No Salesforce user found for email %s; using fallback", email)
        return "005UNKNOWN"
