"""Email Integration — IMAP/SMTP email skill.

Provides:
  - Read emails (inbox, sent, specific folder)
  - Send emails (plain text, HTML)
  - Search emails by subject/sender/date
  - Reply to emails
  - Delete/move emails
"""

from __future__ import annotations

import asyncio
import email
import email.mime.text
import email.mime.multipart
import imaplib
import logging
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    """Parsed email message."""
    uid: str = ""
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date: str = ""
    body: str = ""
    html_body: str = ""
    has_attachments: bool = False
    flags: List[str] = field(default_factory=list)


class EmailSkill:
    """IMAP/SMTP email skill for the agent."""

    name = "email"
    description = "Read and send emails via IMAP/SMTP"

    def __init__(self):
        self._imap_host: str = ""
        self._imap_port: int = 993
        self._smtp_host: str = ""
        self._smtp_port: int = 587
        self._username: str = ""
        self._password: str = ""
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._configured = False

    def configure(self, **kwargs) -> None:
        """Configure email settings.

        Args:
            imap_host: IMAP server hostname
            imap_port: IMAP port (default 993)
            smtp_host: SMTP server hostname
            smtp_port: SMTP port (default 587)
            username: Email username
            password: Email password or app password
        """
        self._imap_host = kwargs.get("imap_host", "")
        self._imap_port = int(kwargs.get("imap_port", 993))
        self._smtp_host = kwargs.get("smtp_host", "")
        self._smtp_port = int(kwargs.get("smtp_port", 587))
        self._username = kwargs.get("username", "")
        self._password = kwargs.get("password", "")
        self._configured = bool(self._imap_host and self._username)

    # --------------------------------------------------- read

    async def read_inbox(
        self, limit: int = 20, unread_only: bool = False,
    ) -> List[EmailMessage]:
        """Read recent emails from inbox."""
        return await self._read_folder("INBOX", limit, unread_only)

    async def read_sent(self, limit: int = 20) -> List[EmailMessage]:
        """Read recent sent emails."""
        return await self._read_folder("[Gmail]/Sent Mail", limit)

    async def search(
        self, query: str, folder: str = "INBOX", limit: int = 20,
    ) -> List[EmailMessage]:
        """Search emails by IMAP query.

        Examples:
          - 'FROM "sender@example.com"'
          - 'SUBJECT "meeting"'
          - 'SINCE "01-Jan-2024"'
          - 'UNSEEN'
        """
        return await self._read_folder(folder, limit, query=query)

    async def _read_folder(
        self, folder: str, limit: int = 20,
        unread_only: bool = False, query: Optional[str] = None,
    ) -> List[EmailMessage]:
        if not self._configured:
            return []

        imap = await self._connect_imap()
        if not imap:
            return []

        try:
            await asyncio.to_thread(imap.select, folder)

            # Build search criteria
            criteria = []
            if unread_only:
                criteria.append("UNSEEN")
            if query:
                criteria.append(query)
            if not criteria:
                criteria.append("ALL")

            search_criteria = " ".join(criteria)
            _, data = await asyncio.to_thread(imap.search, None, search_criteria)
            uids = data[0].split() if data[0] else []

            # Get most recent first
            uids = uids[-limit:] if len(uids) > limit else uids

            messages = []
            for uid in reversed(uids):
                try:
                    _, msg_data = await asyncio.to_thread(
                        imap.fetch, uid, "(RFC822 FLAGS)",
                    )
                    msg = self._parse_email(msg_data, uid.decode())
                    if msg:
                        messages.append(msg)
                except Exception as exc:
                    logger.debug("email parse error for uid %s: %s", uid, exc)

            return messages
        finally:
            try:
                await asyncio.to_thread(imap.logout)
            except Exception:
                pass

    def _parse_email(self, raw_data: list, uid: str) -> Optional[EmailMessage]:
        """Parse raw IMAP fetch response into EmailMessage."""
        if not raw_data or not raw_data[0]:
            return None

        try:
            # Handle different response formats
            if isinstance(raw_data[0], tuple):
                raw_email = raw_data[0][1]
                flags_raw = raw_data[0][0] if len(raw_data) > 0 else b""
            else:
                # When there are multiple parts
                for part in raw_data:
                    if isinstance(part, tuple):
                        raw_email = part[1]
                        break
                else:
                    return None

            parsed = email.message_from_bytes(raw_email)

            # Extract body
            body = ""
            html_body = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    content_type = part.get_content_type()
                    disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" in disposition:
                        continue
                    try:
                        payload = part.get_payload(decode=True)
                        if payload is None:
                            continue
                        text = payload.decode("utf-8", errors="replace")
                        if content_type == "text/plain":
                            body += text
                        elif content_type == "text/html":
                            html_body += text
                    except Exception:
                        pass
            else:
                try:
                    payload = parsed.get_payload(decode=True)
                    if payload:
                        content_type = parsed.get_content_type()
                        text = payload.decode("utf-8", errors="replace")
                        if content_type == "text/html":
                            html_body = text
                        else:
                            body = text
                except Exception:
                    pass

            return EmailMessage(
                uid=uid,
                subject=str(parsed.get("Subject", "")),
                sender=str(parsed.get("From", "")),
                recipients=str(parsed.get("To", "")),
                date=str(parsed.get("Date", "")),
                body=body[:2000],
                html_body=html_body[:5000],
            )
        except Exception as exc:
            logger.debug("email parse failed: %s", exc)
            return None

    # --------------------------------------------------- send

    async def send(
        self, to: str, subject: str, body: str,
        cc: str = "", bcc: str = "", html: bool = False,
    ) -> Dict[str, Any]:
        """Send an email.

        Args:
            to: recipient email address(es), comma-separated
            subject: email subject
            body: email body (plain text or HTML)
            cc: CC recipients
            bcc: BCC recipients
            html: if True, body is treated as HTML
        """
        if not self._configured:
            return {"ok": False, "error": "email not configured"}

        try:
            msg = email.mime.multipart.MIMEMultipart()
            msg["From"] = self._username
            msg["To"] = to
            msg["Subject"] = subject
            if cc:
                msg["Cc"] = cc

            subtype = "html" if html else "plain"
            msg.attach(email.mime.text.MIMEText(body, subtype, "utf-8"))

            recipients = [r.strip() for r in to.split(",") if r.strip()]
            if cc:
                recipients += [r.strip() for r in cc.split(",") if r.strip()]
            if bcc:
                recipients += [r.strip() for r in bcc.split(",") if r.strip()]

            await asyncio.to_thread(self._send_smtp, msg, recipients)
            return {"ok": True, "to": to, "subject": subject}
        except Exception as exc:
            logger.error("email send failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def _send_smtp(
        self, msg: email.mime.multipart.MIMEMultipart, recipients: List[str],
    ) -> None:
        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port)
        smtp.starttls()
        smtp.login(self._username, self._password)
        smtp.sendmail(self._username, recipients, msg.as_string())
        smtp.quit()

    async def reply(
        self, uid: str, body: str, folder: str = "INBOX", html: bool = False,
    ) -> Dict[str, Any]:
        """Reply to a specific email by UID."""
        # Get the original email
        imap = await self._connect_imap()
        if not imap:
            return {"ok": False, "error": "IMAP connection failed"}

        try:
            await asyncio.to_thread(imap.select, folder)
            _, msg_data = await asyncio.to_thread(
                imap.fetch, uid.encode(), "(RFC822)",
            )
            parsed = email.message_from_bytes(msg_data[0][1])
            original_subject = str(parsed.get("Subject", ""))
            original_from = str(parsed.get("From", ""))
            original_message_id = str(parsed.get("Message-ID", ""))

            # Extract reply-to address
            reply_to = original_from
            if "<" in reply_to:
                reply_to = reply_to.split("<")[1].split(">")[0]

            return await self.send(
                to=reply_to,
                subject=f"Re: {original_subject}",
                body=body,
                html=html,
            )
        finally:
            try:
                await asyncio.to_thread(imap.logout)
            except Exception:
                pass

    # --------------------------------------------------- utilities

    async def _connect_imap(self) -> Optional[imaplib.IMAP4_SSL]:
        try:
            imap = await asyncio.to_thread(
                imaplib.IMAP4_SSL, self._imap_host, self._imap_port,
            )
            await asyncio.to_thread(imap.login, self._username, self._password)
            return imap
        except Exception as exc:
            logger.warning("IMAP connection failed: %s", exc)
            return None

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": "email",
            "configured": self._configured,
            "imap_host": self._imap_host,
            "smtp_host": self._smtp_host,
            "username": self._username,
        }

    # --------------------------------------------------- skill interface

    def get_skill_schema(self) -> Dict[str, Any]:
        return {
            "name": "email",
            "description": "Read and send emails via IMAP/SMTP",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "send", "search", "reply"],
                        "description": "action: read inbox, send email, search, or reply",
                    },
                    "to": {"type": "string", "description": "recipient(s) for send"},
                    "subject": {"type": "string", "description": "email subject"},
                    "body": {"type": "string", "description": "email body"},
                    "query": {"type": "string", "description": "IMAP search query"},
                    "limit": {"type": "integer", "description": "max emails to return (default 20)"},
                    "unread_only": {"type": "boolean", "description": "only unread emails"},
                },
                "required": ["action"],
            },
        }

    async def run(self, args: Dict[str, Any]) -> str:
        """Execute email skill."""
        action = args.get("action", "read")

        if action == "read":
            limit = int(args.get("limit", 20))
            unread = bool(args.get("unread_only", False))
            msgs = await self.read_inbox(limit, unread)
            if not msgs:
                return "收件箱为空" if not unread else "没有未读邮件"
            result = []
            for m in msgs:
                result.append(f"[{m.date}] {m.sender} → {m.subject}\n{m.body[:200]}")
            return "\n\n---\n\n".join(result)

        elif action == "send":
            result = await self.send(
                to=args.get("to", ""),
                subject=args.get("subject", "No subject"),
                body=args.get("body", ""),
            )
            return "发送成功" if result.get("ok") else f"发送失败: {result.get('error')}"

        elif action == "search":
            msgs = await self.search(
                args.get("query", ""),
                limit=int(args.get("limit", 20)),
            )
            if not msgs:
                return "未找到匹配的邮件"
            return "\n\n---\n\n".join(
                f"[{m.date}] {m.sender} → {m.subject}\n{m.body[:200]}"
                for m in msgs
            )

        elif action == "reply":
            result = await self.reply(
                uid=args.get("uid", ""),
                body=args.get("body", ""),
            )
            return "回复成功" if result.get("ok") else f"回复失败: {result.get('error')}"

        return "未知操作"


# Singleton
_email_skill: Optional[EmailSkill] = None


def get_email_skill() -> EmailSkill:
    global _email_skill
    if _email_skill is None:
        _email_skill = EmailSkill()
    return _email_skill