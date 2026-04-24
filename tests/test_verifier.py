"""Tests for verifier.py — all SMTP calls are mocked."""

import smtplib
from unittest.mock import MagicMock, patch, call
import pytest

from backend.models import LookupStatus
from backend.verifier import (
    SMTPSenderRejected,
    check_catch_all,
    get_mx_host,
    smtp_check,
    verify_address,
)

FROM_ADDR = "verify@test.example.com"
MX_HOST = "mail.acme.com"


# ---------------------------------------------------------------------------
# get_mx_host
# ---------------------------------------------------------------------------

class TestGetMxHost:
    def test_returns_lowest_priority_mx(self):
        mock_answer = MagicMock()
        mock_answer.preference = 10
        mock_answer.exchange = MagicMock()
        mock_answer.exchange.__str__ = lambda s: "mail.acme.com."

        with patch("dns.resolver.resolve", return_value=[mock_answer]):
            assert get_mx_host("acme.com") == "mail.acme.com"

    def test_sorts_by_priority(self):
        high = MagicMock(preference=20)
        high.exchange.__str__ = lambda s: "backup.acme.com."
        low = MagicMock(preference=10)
        low.exchange.__str__ = lambda s: "mail.acme.com."

        with patch("dns.resolver.resolve", return_value=[high, low]):
            assert get_mx_host("acme.com") == "mail.acme.com"

    def test_falls_back_to_a_record(self):
        import dns.exception

        def mock_resolve(domain, rtype):
            if rtype == "MX":
                raise dns.exception.DNSException()
            return [MagicMock()]  # A record resolves

        with patch("dns.resolver.resolve", side_effect=mock_resolve):
            assert get_mx_host("acme.com") == "acme.com"

    def test_returns_none_when_both_fail(self):
        import dns.exception

        with patch("dns.resolver.resolve", side_effect=dns.exception.DNSException()):
            assert get_mx_host("nonexistent.example.com") is None


# ---------------------------------------------------------------------------
# smtp_check
# ---------------------------------------------------------------------------

def _mock_smtp(rcpt_code: int = 250, sender_refused: bool = False):
    """Return a mock SMTP context manager."""
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
    mock_smtp.__exit__ = MagicMock(return_value=False)
    mock_smtp.rcpt.return_value = (rcpt_code, b"OK")
    if sender_refused:
        mock_smtp.mail.side_effect = smtplib.SMTPSenderRefused(550, b"rejected", FROM_ADDR)
    return mock_smtp


class TestSmtpCheck:
    def test_250_returns_verified(self):
        with patch("smtplib.SMTP", return_value=_mock_smtp(250)):
            assert smtp_check(MX_HOST, "john@acme.com", FROM_ADDR) == LookupStatus.verified

    def test_550_returns_not_found(self):
        with patch("smtplib.SMTP", return_value=_mock_smtp(550)):
            assert smtp_check(MX_HOST, "nobody@acme.com", FROM_ADDR) == LookupStatus.not_found

    def test_551_returns_not_found(self):
        with patch("smtplib.SMTP", return_value=_mock_smtp(551)):
            assert smtp_check(MX_HOST, "nobody@acme.com", FROM_ADDR) == LookupStatus.not_found

    def test_553_returns_not_found(self):
        with patch("smtplib.SMTP", return_value=_mock_smtp(553)):
            assert smtp_check(MX_HOST, "nobody@acme.com", FROM_ADDR) == LookupStatus.not_found

    def test_421_returns_inconclusive(self):
        with patch("smtplib.SMTP", return_value=_mock_smtp(421)):
            assert smtp_check(MX_HOST, "john@acme.com", FROM_ADDR) == LookupStatus.inconclusive

    def test_connection_refused_returns_inconclusive(self):
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError()):
            assert smtp_check(MX_HOST, "john@acme.com", FROM_ADDR) == LookupStatus.inconclusive

    def test_timeout_returns_inconclusive(self):
        import socket
        with patch("smtplib.SMTP", side_effect=socket.timeout()):
            assert smtp_check(MX_HOST, "john@acme.com", FROM_ADDR) == LookupStatus.inconclusive

    def test_smtp_server_disconnected_returns_inconclusive(self):
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPServerDisconnected()):
            assert smtp_check(MX_HOST, "john@acme.com", FROM_ADDR) == LookupStatus.inconclusive

    def test_sender_refused_raises(self):
        with patch("smtplib.SMTP", return_value=_mock_smtp(sender_refused=True)):
            with pytest.raises(SMTPSenderRejected):
                smtp_check(MX_HOST, "john@acme.com", FROM_ADDR)

    def test_context_manager_called(self):
        """Connection is opened and closed via context manager."""
        mock = _mock_smtp(250)
        with patch("smtplib.SMTP", return_value=mock):
            smtp_check(MX_HOST, "john@acme.com", FROM_ADDR)
        mock.__exit__.assert_called_once()


# ---------------------------------------------------------------------------
# verify_address (retry logic)
# ---------------------------------------------------------------------------

class TestVerifyAddress:
    def test_verified_on_first_attempt(self):
        with patch("backend.verifier.smtp_check", return_value=LookupStatus.verified) as m:
            result = verify_address(MX_HOST, "john@acme.com", FROM_ADDR)
        assert result == LookupStatus.verified
        assert m.call_count == 1

    def test_not_found_no_retry(self):
        with patch("backend.verifier.smtp_check", return_value=LookupStatus.not_found) as m:
            result = verify_address(MX_HOST, "nobody@acme.com", FROM_ADDR)
        assert result == LookupStatus.not_found
        assert m.call_count == 1

    def test_inconclusive_retries_3_times(self):
        with patch("backend.verifier.smtp_check", return_value=LookupStatus.inconclusive) as m:
            with patch("time.sleep"):
                result = verify_address(MX_HOST, "john@acme.com", FROM_ADDR)
        assert result == LookupStatus.inconclusive
        assert m.call_count == 3

    def test_retries_then_succeeds(self):
        side_effects = [
            LookupStatus.inconclusive,
            LookupStatus.inconclusive,
            LookupStatus.verified,
        ]
        with patch("backend.verifier.smtp_check", side_effect=side_effects) as m:
            with patch("time.sleep"):
                result = verify_address(MX_HOST, "john@acme.com", FROM_ADDR)
        assert result == LookupStatus.verified
        assert m.call_count == 3

    def test_sender_rejected_propagates_immediately(self):
        with patch("backend.verifier.smtp_check", side_effect=SMTPSenderRejected("rejected")):
            with pytest.raises(SMTPSenderRejected):
                verify_address(MX_HOST, "john@acme.com", FROM_ADDR)


# ---------------------------------------------------------------------------
# check_catch_all
# ---------------------------------------------------------------------------

class TestCheckCatchAll:
    def test_both_fake_verified_is_catch_all(self):
        with patch("backend.verifier.smtp_check", return_value=LookupStatus.verified):
            assert check_catch_all(MX_HOST, "acme.com", FROM_ADDR) is True

    def test_first_fake_not_found_is_not_catch_all(self):
        side_effects = [LookupStatus.not_found, LookupStatus.verified]
        with patch("backend.verifier.smtp_check", side_effect=side_effects):
            assert check_catch_all(MX_HOST, "acme.com", FROM_ADDR) is False

    def test_second_fake_not_found_is_not_catch_all(self):
        side_effects = [LookupStatus.verified, LookupStatus.not_found]
        with patch("backend.verifier.smtp_check", side_effect=side_effects):
            assert check_catch_all(MX_HOST, "acme.com", FROM_ADDR) is False

    def test_both_not_found_is_not_catch_all(self):
        with patch("backend.verifier.smtp_check", return_value=LookupStatus.not_found):
            assert check_catch_all(MX_HOST, "acme.com", FROM_ADDR) is False

    def test_sender_rejected_returns_none(self):
        with patch("backend.verifier.smtp_check", side_effect=SMTPSenderRejected("rejected")):
            assert check_catch_all(MX_HOST, "acme.com", FROM_ADDR) is None

    def test_probes_two_distinct_fake_addresses(self):
        calls = []
        def capture(mx, addr, from_a):
            calls.append(addr)
            return LookupStatus.not_found

        with patch("backend.verifier.smtp_check", side_effect=capture):
            check_catch_all(MX_HOST, "acme.com", FROM_ADDR)

        assert len(calls) == 2
        assert calls[0] != calls[1]
        assert all("acme.com" in c for c in calls)
