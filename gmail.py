"""
Gmail API client (service-account / domain-wide delegation).

Creates draft emails in the AE's mailbox for review before sending.
Set GMAIL_MOCK=true for local dev / tests.
"""

import base64
import json
import logging
import os
from email.mime.text import MIMEText

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


class GmailClient:
    def __init__(self, service_account_key: str) -> None:
        """
        Args:
            service_account_key: JSON string of the GCP service account key
                                 (fetched from Secret Manager in production).
        """
        self._mock = os.environ.get("GMAIL_MOCK", "false").lower() == "true"
        self._key_data = {} if self._mock else json.loads(service_account_key)

    def create_draft(self, sender: str, to: str, subject: str, body: str) -> None:
        """Create a Gmail draft in the sender's mailbox."""
        if self._mock:
            logger.info(
                "GMAIL_MOCK=true — would create draft:\n  From: %s\n  To: %s\n  Subject: %s\n\n%s",
                sender, to, subject, body,
            )
            return

        service = self._build_service(sender)
        message = MIMEText(body)
        message["to"] = to
        message["from"] = sender
        message["subject"] = subject

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft_body = {"message": {"raw": encoded}}
        service.users().drafts().create(userId="me", body=draft_body).execute()
        logger.info("Gmail draft created for %s → %s", sender, to)

    def _build_service(self, impersonate_email: str):
        """Build a Gmail API service client impersonating the AE's account."""
        credentials = service_account.Credentials.from_service_account_info(
            self._key_data,
            scopes=SCOPES,
        ).with_subject(impersonate_email)
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)
