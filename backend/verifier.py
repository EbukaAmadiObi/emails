"""
SMTP-based email verification.

All functions are synchronous (use asyncio.to_thread from worker.py).
No module-level state — callers manage caching.
"""

import logging
import smtplib
import socket
import time
from typing import Optional

import dns.exception
import dns.resolver

from .models import LookupStatus

logger = logging.getLogger(__name__)


class SMTPSenderRejected(Exception):
    """MAIL FROM was rejected by the remote server (5xx on MAIL FROM stage)."""


def get_mx_host(domain: str) -> Optional[str]:
    """Return the hostname to connect to for SMTP verification.

    Tries MX records (lowest priority first), then falls back to A record.
    Returns None if neither resolves.
    """
    try:
        answers = dns.resolver.resolve(domain, "MX")
        sorted_mx = sorted(answers, key=lambda r: r.preference)
        return str(sorted_mx[0].exchange).rstrip(".")
    except (dns.exception.DNSException, IndexError):
        pass

    try:
        dns.resolver.resolve(domain, "A")
        return domain
    except dns.exception.DNSException:
        return None


def smtp_check(mx_host: str, address: str, from_addr: str) -> LookupStatus:
    """Single SMTP handshake to check if an address exists.

    Makes exactly one attempt. Raises SMTPSenderRejected if MAIL FROM
    is rejected — the caller must not retry in that case.

    Uses context manager to guarantee connection cleanup on all paths.
    """
    try:
        with smtplib.SMTP(mx_host, port=25, timeout=10) as smtp:
            smtp.ehlo()
            try:
                smtp.mail(from_addr)
            except smtplib.SMTPSenderRefused as e:
                raise SMTPSenderRejected(
                    f"MAIL FROM <{from_addr}> rejected by {mx_host}: {e}"
                ) from e
            code, _ = smtp.rcpt(address)
    except SMTPSenderRejected:
        raise
    except (
        smtplib.SMTPConnectError,
        smtplib.SMTPServerDisconnected,
        smtplib.SMTPException,
        socket.timeout,
        ConnectionRefusedError,
        OSError,
    ) as e:
        logger.debug("SMTP error for %s: %s", address, e)
        return LookupStatus.inconclusive

    if code == 250:
        return LookupStatus.verified
    if code in (550, 551, 553):
        return LookupStatus.not_found
    # 4xx or other unexpected code
    return LookupStatus.inconclusive


def verify_address(mx_host: str, address: str, from_addr: str) -> LookupStatus:
    """SMTP check with retry logic for inconclusive results.

    Retries up to 2 times (3 total attempts) with 3s backoff on inconclusive.
    Does NOT retry not_found or verified — those are definitive.
    Propagates SMTPSenderRejected immediately without retry.
    """
    for attempt in range(3):
        status = smtp_check(mx_host, address, from_addr)
        if status != LookupStatus.inconclusive:
            return status
        if attempt < 2:
            time.sleep(1)
    return LookupStatus.inconclusive


def check_catch_all(mx_host: str, domain: str, from_addr: str) -> Optional[bool]:
    """Detect whether a domain accepts all email addresses (catch-all).

    Tests two clearly fake addresses. Both returning 250 → catch-all.
    Either returning 550 → domain validates individual mailboxes.
    Returns None if MAIL FROM was rejected (can't determine catch-all status).
    """
    fake_addresses = [
        f"xjkqwerty_notreal_99@{domain}",
        f"zzz_fakeemail_000@{domain}",
    ]
    results: list[LookupStatus] = []
    for fake in fake_addresses:
        try:
            result = smtp_check(mx_host, fake, from_addr)
            results.append(result)
        except SMTPSenderRejected:
            logger.warning(
                "MAIL FROM rejected by %s — cannot determine catch-all status for %s",
                mx_host,
                domain,
            )
            return None

    return all(r == LookupStatus.verified for r in results)
