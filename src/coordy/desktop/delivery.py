"""Explicit, durable summary delivery with no automatic retries after ambiguity."""
from email.message import EmailMessage
import hashlib
import json
import os
import smtplib
import sqlite3
import ssl
import time
from urllib.parse import urlparse

import httpx

from .settings import DesktopSettings


class Delivery:
    def __init__(self, root, preferences: DesktopSettings, transport=None):
        self.path = root / "delivery.db"
        self.preferences = preferences
        self.transport = transport
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, meeting TEXT, channel TEXT, state TEXT, error TEXT DEFAULT '', started REAL DEFAULT 0)''')
            if 'started' not in {row[1] for row in db.execute('PRAGMA table_info(deliveries)')}:
                db.execute('ALTER TABLE deliveries ADD COLUMN started REAL DEFAULT 0')

    def send(self, meeting_id: str, markdown: str, channel: str, *, retry=False) -> str:
        p = self.preferences
        if channel not in {"teams", "email"}:
            raise ValueError("Choose teams or email")
        # Destination and content form the identity; changing recipients permits a new send.
        destination = os.getenv("COORDY_TEAMS_WEBHOOK", "") if channel == "teams" else f"{p.smtp_host}|{p.sender}|{p.recipients}"
        key = hashlib.sha256(json.dumps([meeting_id, channel, destination, markdown]).encode()).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state, started FROM deliveries WHERE id=?", (key,)).fetchone()
            if row and row[0] == "sent":
                return "Already sent"
            if row and (not retry or row[0] == "sending" and time.time() - row[1] < 600):
                raise RuntimeError("Delivery needs review. Check the destination before retrying; a previous attempt may have arrived. An interrupted send can be retried after 10 minutes.")
            db.execute("INSERT OR REPLACE INTO deliveries (id, meeting, channel, state, error, started) VALUES (?, ?, ?, 'sending', '', ?)", (key, meeting_id, channel, time.time()))
        try:
            if channel == "teams":
                self._teams(markdown)
            else:
                self._email(meeting_id, markdown)
        except Exception:
            # Do not persist exception strings: providers can echo webhook credentials.
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE deliveries SET state='failed', error='Delivery failed; inspect destination before retry' WHERE id=?", (key,))
            raise RuntimeError("Delivery failed. Check configuration and destination before retrying.") from None
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE deliveries SET state='sent' WHERE id=?", (key,))
        return "Sent"

    def _teams(self, markdown):
        url = os.getenv("COORDY_TEAMS_WEBHOOK", "")
        parsed = urlparse(url)
        # Teams Workflows accepts Adaptive Cards; legacy Office connector URLs are excluded.
        host = parsed.hostname or ""
        allowed = host.endswith(".logic.azure.com") or host.endswith(".environment.api.powerplatform.com")
        if parsed.scheme != "https" or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Set COORDY_TEAMS_WEBHOOK to a Teams Workflow HTTPS webhook")
        if len(markdown.encode()) > 22000:
            raise ValueError("Summary exceeds Teams card size. Export the full minutes locally.")
        payload = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                        "body": [{"type": "TextBlock", "text": markdown, "wrap": True}]}}]}
        with httpx.Client(timeout=30, transport=self.transport, follow_redirects=False) as client:
            client.post(url, json=payload).raise_for_status()

    def _email(self, meeting_id, markdown):
        p = self.preferences
        recipients = [s.strip() for s in p.recipients.split(",") if s.strip()]
        if not p.smtp_host or not p.sender or not recipients:
            raise ValueError("Configure SMTP host, sender and recipients in settings")
        message = EmailMessage()
        message["Subject"] = f"Coord meeting minutes — {meeting_id}"
        message["From"] = p.sender
        message["To"] = ", ".join(recipients)
        message.set_content(markdown)
        context = ssl.create_default_context()
        with smtplib.SMTP(p.smtp_host, p.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            if p.smtp_user:
                password = os.getenv("COORDY_SMTP_PASSWORD")
                if not password:
                    raise ValueError("Set COORDY_SMTP_PASSWORD securely before starting Coord")
                smtp.login(p.smtp_user, password)
            smtp.send_message(message)
