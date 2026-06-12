import asyncio
import email as _email_lib
import imaplib
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header

logger = logging.getLogger(__name__)


def _decode_header_str(raw: str) -> str:
    parts = decode_header(raw or "")
    decoded = []
    for b, enc in parts:
        if isinstance(b, bytes):
            decoded.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(b)
    return "".join(decoded)


def _send_sync(to: str, subject: str, body: str, cfg: dict) -> None:
    host = cfg["smtp_host"]
    port = int(cfg.get("smtp_port", 587))
    user = cfg["smtp_user"]
    password = cfg["smtp_password"]
    from_addr = cfg.get("from_address", user)
    use_tls = cfg.get("smtp_tls", True)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=context)
            s.login(user, password)
            s.sendmail(from_addr, [to], msg.as_string())
    else:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, password)
            s.sendmail(from_addr, [to], msg.as_string())


async def send_email(to: str, subject: str, body: str, cfg: dict) -> None:
    await asyncio.get_event_loop().run_in_executor(None, _send_sync, to, subject, body, cfg)


def _read_sync(folder: str, count: int, cfg: dict) -> list[dict]:
    host = cfg["imap_host"]
    port = int(cfg.get("imap_port", 993))
    user = cfg["imap_user"]
    password = cfg["imap_password"]

    with imaplib.IMAP4_SSL(host, port) as m:
        m.login(user, password)
        m.select(folder)
        _, ids = m.search(None, "ALL")
        mail_ids = ids[0].split()
        recent_ids = mail_ids[-count:] if len(mail_ids) >= count else mail_ids
        results = []
        for mid in reversed(recent_ids):
            _, data = m.fetch(mid, "(RFC822.HEADER)")
            raw = data[0][1]
            msg = _email_lib.message_from_bytes(raw)
            results.append({
                "from": _decode_header_str(msg.get("From", "")),
                "subject": _decode_header_str(msg.get("Subject", "(no subject)")),
                "date": msg.get("Date", ""),
                "id": mid.decode(),
            })
        return results


async def read_emails(folder: str, count: int, cfg: dict) -> list[dict]:
    return await asyncio.get_event_loop().run_in_executor(None, _read_sync, folder, count, cfg)


def _extract_snippet(msg: "_email_lib.message.Message", limit: int) -> str:
    """Best-effort plain-text snippet from a parsed message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    body = " ".join(body.split())  # collapse whitespace
    return body[:limit]


def _read_new_sync(folder: str, since_uid: int, max_count: int, snippet_chars: int, cfg: dict) -> dict:
    """Fetch messages with UID > since_uid (stable across sessions, unlike sequence ids).

    Returns {"max_uid": int, "messages": [{uid, from, subject, date, snippet}, ...]}.
    On first run (since_uid == 0) no messages are returned — only the current max
    UID, so enabling the watcher never floods with the existing backlog.
    """
    host = cfg["imap_host"]
    port = int(cfg.get("imap_port", 993))
    user = cfg["imap_user"]
    password = cfg["imap_password"]

    with imaplib.IMAP4_SSL(host, port) as m:
        m.login(user, password)
        m.select(folder)
        _, data = m.uid("search", None, "ALL")
        uids = [int(x) for x in data[0].split()] if data and data[0] else []
        if not uids:
            return {"max_uid": since_uid, "messages": []}

        max_uid = max(uids)
        if since_uid <= 0:
            # Baseline only — do not pull the existing backlog.
            return {"max_uid": max_uid, "messages": []}

        new_uids = sorted(u for u in uids if u > since_uid)
        # Cap work per tick: keep the most recent ones, but never lose ground —
        # max_uid still advances to the true maximum so skipped older mails are
        # not re-evaluated forever.
        if len(new_uids) > max_count:
            new_uids = new_uids[-max_count:]

        messages = []
        for uid in new_uids:
            _, fetched = m.uid("fetch", str(uid), "(RFC822)")
            if not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            msg = _email_lib.message_from_bytes(raw)
            messages.append({
                "uid": uid,
                "from": _decode_header_str(msg.get("From", "")),
                "subject": _decode_header_str(msg.get("Subject", "(no subject)")),
                "date": msg.get("Date", ""),
                "snippet": _extract_snippet(msg, snippet_chars),
            })
        return {"max_uid": max_uid, "messages": messages}


async def read_new_emails(folder: str, since_uid: int, max_count: int, snippet_chars: int, cfg: dict) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, _read_new_sync, folder, since_uid, max_count, snippet_chars, cfg
    )
