"""
ASK handler for CCB Mail v3.

Handles incoming emails by routing them to the ASK system.
Sets CCB_CALLER=email so completion hook knows to send email reply.
"""

import os
import subprocess
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .body_parser import BodyParser, ParsedMessage
from .router import RoutedMessage
from .filters import clean_email_body

if TYPE_CHECKING:
    from .config import MailConfig


# Directory for storing email context for completion hook
EMAIL_CONTEXT_DIR = Path.home() / ".ccb" / "mail" / "pending"


@dataclass
class AskResult:
    """Result of sending to ASK system."""
    success: bool
    message: str
    request_id: Optional[str] = None


class AskHandler:
    """Handles email messages by sending to ASK system."""

    def __init__(self, config: "MailConfig"):
        """
        Initialize ASK handler.

        Args:
            config: Mail configuration (v3)
        """
        self.config = config

        # Get v3 config values with defaults
        self.default_provider = getattr(config, 'default_provider', 'claude')
        self.default_work_dir = getattr(config, 'default_work_dir', '')

        # Get parser settings
        pattern = getattr(config, 'provider_prefix_pattern', r"^(\w+)")
        case_insensitive = getattr(config, 'case_insensitive', True)

        # Import here to avoid circular import
        from .config import SUPPORTED_PROVIDERS
        self.parser = BodyParser(
            pattern=pattern,
            case_insensitive=case_insensitive,
            valid_providers=SUPPORTED_PROVIDERS,
        )

    def _get_work_dir(self) -> str:
        """Get working directory for ASK command."""
        if self.default_work_dir:
            return self.default_work_dir
        return os.getcwd()

    def _save_email_context(self, msg: RoutedMessage, request_id: str) -> bool:
        """
        Save email context for completion hook to use.

        Args:
            msg: Original email message
            request_id: Unique request ID

        Returns:
            True if saved successfully
        """
        try:
            EMAIL_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
            context_file = EMAIL_CONTEXT_DIR / f"{request_id}.json"

            context = {
                "request_id": request_id,
                "message_id": msg.message_id,
                "from_addr": msg.from_addr,
                "subject": msg.subject,
                "thread_id": msg.thread_id,
                "references": msg.references,
                "timestamp": time.time(),
            }

            with open(context_file, "w", encoding="utf-8") as f:
                json.dump(context, f, indent=2)
            context_file.chmod(0o600)
            return True
        except Exception as e:
            print(f"[maild] Failed to save email context: {e}")
            return False

    def _find_ask_command(self) -> Optional[str]:
        """Find the ask command."""
        script_dir = Path(__file__).resolve().parent.parent.parent / "bin"
        ask_paths = [
            script_dir / "ask",
            Path.home() / ".local" / "bin" / "ask",
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "ask",
        ]
        for p in ask_paths:
            if p.exists():
                return str(p)
        return None

    def _send_email_reply(self, msg: RoutedMessage, provider: str, reply: str) -> bool:
        """Send email reply directly."""
        try:
            from .sender import SmtpSender
            sender = SmtpSender(self.config)

            # Build unified subject with project name only
            work_dir = self._get_work_dir()
            project_name = os.path.basename(work_dir.rstrip('/')) if work_dir else "unknown"
            subject = f"[CCB] {project_name}"

            body = f"[{provider.capitalize()}] {reply}\n\n---\nSent via CCB ({provider})"

            success, result = sender.send_reply(
                to_addr=msg.from_addr,
                subject=subject,
                body=body,
                in_reply_to=msg.message_id,
                provider=provider,
            )
            if success:
                print(f"[maild] Email reply sent to {msg.from_addr}")
            else:
                print(f"[maild] Failed to send email: {result}")
            return success
        except Exception as e:
            print(f"[maild] Error sending email reply: {e}")
            return False

    def handle_email(self, msg: RoutedMessage) -> AskResult:
        """
        Handle incoming email by sending to ASK system.

        1. Clean email body (remove quotes, signatures)
        2. Parse provider from body prefix
        3. Save email context for completion hook
        4. Async call ask command with CCB_CALLER=email

        Args:
            msg: Routed email message

        Returns:
            AskResult indicating success/failure
        """
        # Debug: log raw body
        print(f"[maild] DEBUG raw body ({len(msg.body)} chars): {repr(msg.body[:500])}")

        # Clean email body first (remove quoted replies, signatures)
        clean_body = clean_email_body(msg.body)
        print(f"[maild] DEBUG clean body ({len(clean_body)} chars): {repr(clean_body[:200])}")

        if not clean_body:
            return AskResult(
                success=False,
                message="Empty content after cleaning",
            )

        # Parse provider from body
        parsed = self.parser.parse(clean_body)
        provider = parsed.provider or self.default_provider
        message = parsed.message or clean_body

        # Include subject in message if present
        if msg.subject:
            # Strip common prefixes
            subject = msg.subject
            for prefix in ["Re:", "RE:", "Fwd:", "FW:"]:
                if subject.startswith(prefix):
                    subject = subject[len(prefix):].strip()
            if subject and not subject.startswith("[CCB]"):
                message = f"Subject: {subject}\n\n{message}"

        # Generate request ID
        request_id = f"email-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

        # Save context for completion hook
        if not self._save_email_context(msg, request_id):
            print(f"[maild] Warning: failed to save email context (req={request_id})")

        # Find ask command
        ask_cmd = self._find_ask_command()
        if not ask_cmd:
            return AskResult(
                success=False,
                message="ask command not found",
            )

        # Prepare environment
        work_dir = self._get_work_dir()
        env = os.environ.copy()
        # Email ingress is always an EXTERNAL sender for the role delivery
        # policy: it must never inherit a launch credential or a carrying
        # route from whatever shell the mail daemon happens to run under.
        for leaked in (
            "CCB_MEMBER_ID",
            "CCB_MEMBER_TOKEN",
            "CCB_LIVE_ID",
            "CCB_LIVE_TOKEN",
            "CCB_ROUTE_LIVE_ID",
            "CCB_ROUTE_LAUNCH_ID",
            "CCB_ROUTE_CALLER_LIVE_ID",
            "CCB_ROUTE_CALLER_TOKEN",
            "CCB_ROUTE_CALLER_PANE_ID",
            "CCB_ROUTE_CALLER_TERMINAL",
            "CCB_ROUTE_PANE_ID",
            "CCB_ROUTE_TERMINAL",
            "CCB_ROUTE_SESSION_FILE",
            "CCB_ROUTE_PROJECT_ID",
            "CCB_ROUTE_TASK_ID",
        ):
            env.pop(leaked, None)
        env["CCB_CALLER"] = "email"
        env["CCB_EMAIL_REQ_ID"] = request_id
        env["CCB_EMAIL_MSG_ID"] = msg.message_id or ""
        env["CCB_EMAIL_FROM"] = msg.from_addr or ""
        env["CCB_WORK_DIR"] = work_dir
        if "CCB_RUN_DIR" not in env and work_dir:
            env["CCB_RUN_DIR"] = work_dir

        # Call ask command in background mode (default)
        # Reply will be sent via completion hook with CCB_CALLER=email
        try:
            result = subprocess.run(
                [ask_cmd, provider, "-t", "3600"],
                cwd=work_dir,
                env=env,
                input=message,
                capture_output=True,
                text=True,
                timeout=60,  # Short timeout for submission
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                if not err:
                    err = f"ask exited with code {result.returncode}"
                return AskResult(
                    success=False,
                    message=err,
                    request_id=request_id,
                )
            print(f"[maild] Submitted to {provider} (req={request_id})")

            return AskResult(
                success=True,
                message=f"Submitted to {provider}",
                request_id=request_id,
            )
        except subprocess.TimeoutExpired:
            return AskResult(
                success=False,
                message=f"Timeout submitting to {provider}",
            )
        except Exception as e:
            return AskResult(
                success=False,
                message=f"Failed to call ask: {e}",
            )


def load_email_context(request_id: str) -> Optional[dict]:
    """
    Load saved email context by request ID.

    Args:
        request_id: Request ID from CCB_EMAIL_REQ_ID

    Returns:
        Context dict or None if not found
    """
    context_file = EMAIL_CONTEXT_DIR / f"{request_id}.json"
    if not context_file.exists():
        return None
    try:
        with open(context_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cleanup_email_context(request_id: str) -> bool:
    """
    Remove email context file after processing.

    Args:
        request_id: Request ID

    Returns:
        True if removed
    """
    context_file = EMAIL_CONTEXT_DIR / f"{request_id}.json"
    try:
        if context_file.exists():
            context_file.unlink()
            return True
    except Exception:
        pass
    return False
