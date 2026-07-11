"""
Message routing for CCB Mail.

Routes incoming emails to the appropriate AI provider based on:
- Plus-alias: user+claude@gmail.com -> claude
- Subject prefix: [codex] message -> codex
- V2: Thread-ID based routing for pane replies
"""

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from email.message import EmailMessage

from .config import MailConfig, SUPPORTED_PROVIDERS


# Default subject prefixes for v2 mode
DEFAULT_SUBJECT_PREFIXES = {
    "claude": "[claude]",
    "codex": "[codex]",
    "gemini": "[gemini]",
    "opencode": "[opencode]",
}


@dataclass
class RoutedMessage:
    """A routed email message."""
    provider: str
    subject: str
    body: str
    from_addr: str
    message_id: str
    thread_id: Optional[str] = None
    references: Optional[str] = None
    attachments: list = field(default_factory=list)
    imap_msg_id: Optional[bytes] = None  # IMAP message ID for marking as read


class MessageRouter:
    """Routes incoming emails to AI providers."""

    def __init__(self, config: MailConfig):
        self.config = config
        # Support both v1 (routing) and v2 (pane_hooks) config
        self._has_routing = hasattr(config, 'routing')

    def _get_routing_mode(self) -> str:
        """Get routing mode (v1 only)."""
        if self._has_routing:
            return self.config.routing.mode
        return "subject_prefix"

    def _get_default_provider(self) -> str:
        """Get default provider."""
        if self._has_routing:
            return self.config.routing.default_provider
        return "claude"

    def _get_subject_prefixes(self) -> dict:
        """Get subject prefixes."""
        if self._has_routing:
            return self.config.routing.subject_prefixes
        return DEFAULT_SUBJECT_PREFIXES

    def _get_allowed_senders(self) -> List[str]:
        """Get allowed senders list."""
        if self._has_routing:
            return self.config.routing.allowed_senders
        # V2: use target_email as the only allowed sender
        if hasattr(self.config, 'target_email') and self.config.target_email:
            return [self.config.target_email]
        return []

    def _get_reply_to_address(self) -> str:
        """Get reply-to address."""
        if self._has_routing:
            return self.config.routing.reply_to_address
        # V2: use target_email
        if hasattr(self.config, 'target_email'):
            return self.config.target_email
        return ""

    def is_sender_allowed(self, from_addr: str) -> bool:
        """
        Check if sender is in the allowed list.

        Returns True if:
        - allowed_senders list is empty (accept all)
        - sender email is in the allowed list
        """
        allowed = self._get_allowed_senders()
        if not allowed:
            return True  # Empty list = accept all

        # Extract email from "Name <email>" format
        email = from_addr
        if "<" in from_addr and ">" in from_addr:
            email = from_addr.split("<")[1].split(">")[0]

        return email.lower() in [a.lower() for a in allowed]

    def get_reply_address(self, original_from: str) -> str:
        """
        Get the address to send reply to.

        Returns:
        - reply_to_address if configured
        - otherwise the original sender address
        """
        reply_to = self._get_reply_to_address()
        if reply_to:
            return reply_to
        return original_from

    def extract_provider_from_plus_alias(self, to_addr: str) -> Optional[str]:
        """
        Extract provider from plus-alias format.

        Example: user+claude@gmail.com -> claude
        """
        match = re.match(r"([^+]+)\+([^@]+)@(.+)", to_addr)
        if match:
            alias = match.group(2).lower()
            # Check if it's a known provider
            if alias in SUPPORTED_PROVIDERS:
                return alias
        return None

    def extract_provider_from_subject(self, subject: str) -> Tuple[Optional[str], str]:
        """
        Extract provider from subject prefix.

        Example: [codex] Help me analyze -> (codex, "Help me analyze")

        Returns:
            Tuple of (provider, cleaned_subject)
        """
        prefixes = self._get_subject_prefixes()
        subject_lower = subject.lower().strip()

        for provider, prefix in prefixes.items():
            prefix_lower = prefix.lower()
            if subject_lower.startswith(prefix_lower):
                # Remove prefix and clean up
                cleaned = subject[len(prefix):].strip()
                return provider, cleaned

        return None, subject

    def extract_provider_from_thread_id(self, thread_id: str) -> Optional[str]:
        """
        Extract provider from CCB thread ID.

        Example: ccb-claude-1234567890 -> claude
        """
        if not thread_id:
            return None
        match = re.search(r"ccb-(\w+)-\d+", thread_id.lower())
        if match:
            provider = match.group(1)
            if provider in SUPPORTED_PROVIDERS:
                return provider
        return None

    def route_message(
        self,
        to_addr: str,
        from_addr: str,
        subject: str,
        body: str,
        message_id: str,
        references: Optional[str] = None,
        attachments: Optional[list] = None,
    ) -> RoutedMessage:
        """
        Route an incoming email to the appropriate provider.

        Args:
            to_addr: Recipient email address
            from_addr: Sender email address
            subject: Email subject
            body: Email body text
            message_id: Email Message-ID header
            references: Email References header (for threading)
            attachments: List of attachment info dicts

        Returns:
            RoutedMessage with provider and cleaned content
        """
        provider = None
        cleaned_subject = subject

        # Extract thread ID from References header
        thread_id = None
        if references:
            # Use first reference as thread ID
            refs = references.strip().split()
            if refs:
                thread_id = refs[0].strip("<>")
                # V2: Try to extract provider from thread ID first
                provider = self.extract_provider_from_thread_id(thread_id)

        # If no provider from thread ID, try routing mode
        if not provider:
            routing_mode = self._get_routing_mode()
            if routing_mode == "plus_alias":
                provider = self.extract_provider_from_plus_alias(to_addr)
            elif routing_mode == "subject_prefix":
                provider, cleaned_subject = self.extract_provider_from_subject(subject)

        # Fall back to default provider
        if not provider:
            provider = self._get_default_provider()

        return RoutedMessage(
            provider=provider,
            subject=cleaned_subject,
            body=body,
            from_addr=from_addr,
            message_id=message_id,
            thread_id=thread_id,
            references=references,
            attachments=attachments or [],
        )

    def route_email_message(self, msg: EmailMessage) -> RoutedMessage:
        """
        Route an EmailMessage object.

        Args:
            msg: email.message.EmailMessage object

        Returns:
            RoutedMessage
        """
        to_addr = msg.get("To", "")
        from_addr = msg.get("From", "")
        subject_raw = msg.get("Subject", "")
        # Convert Header object to string if needed
        subject = str(subject_raw) if subject_raw else ""
        message_id = msg.get("Message-ID", "")
        references = msg.get("References", "")

        def _decode_payload(part: EmailMessage) -> str:
            payload = part.get_payload(decode=True)
            if not payload:
                return ""
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")

        def _strip_html(text: str) -> str:
            # Remove script/style blocks
            cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", "", text)
            # Convert common line breaks to newlines
            cleaned = re.sub(r"(?i)<br\\s*/?>", "\n", cleaned)
            cleaned = re.sub(r"(?i)</p\\s*>", "\n", cleaned)
            # Remove remaining tags
            cleaned = re.sub(r"(?s)<[^>]+>", "", cleaned)
            return html_lib.unescape(cleaned).strip()

        # Extract body (prefer text/plain; fallback to text/html)
        body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.is_multipart():
                    continue
                if part.get_content_type() == "text/plain":
                    body = _decode_payload(part)
                    break
            if not body:
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    if part.get_content_type() == "text/html":
                        html_body = _decode_payload(part)
                        break
        else:
            content_type = msg.get_content_type()
            if content_type == "text/plain":
                body = _decode_payload(msg)
            elif content_type == "text/html":
                html_body = _decode_payload(msg)
            else:
                body = _decode_payload(msg)

        if not body and html_body:
            body = _strip_html(html_body)

        # Extract attachments info
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                content_disposition = part.get("Content-Disposition", "")
                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        attachments.append({
                            "filename": filename,
                            "content_type": part.get_content_type(),
                            "size": len(part.get_payload(decode=True) or b""),
                        })

        return self.route_message(
            to_addr=to_addr,
            from_addr=from_addr,
            subject=subject,
            body=body,
            message_id=message_id,
            references=references,
            attachments=attachments,
        )
