"""Secrets scrubber — redact credential-shaped strings at persistence."""

from __future__ import annotations

from pyclaudir.secrets_scrubber import REDACTION, contains_secret, scrub


def test_bearer_token_redacted() -> None:
    text = "Authorization: Bearer abc123DEF456ghi789JKL"
    out = scrub(text)
    assert "abc123" not in out
    assert REDACTION in out


def test_api_key_header_redacted() -> None:
    text = 'x-api-key: "sometoken1234567890abcdef"'
    out = scrub(text)
    assert "sometoken" not in out
    assert REDACTION in out


def test_sk_key_redacted() -> None:
    text = "here's my key: sk-proj-abcdefghijklmnopqrstuvwxyz01234567"
    out = scrub(text)
    assert "sk-proj-abcdef" not in out


def test_github_token_redacted() -> None:
    text = "push with ghp_aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsSt123"
    out = scrub(text)
    assert "ghp_" not in out


def test_aws_access_key_redacted() -> None:
    text = "AWS key AKIAIOSFODNN7EXAMPLE and something else"
    out = scrub(text)
    assert "AKIA" not in out
    assert "something else" in out


def test_slack_token_redacted() -> None:
    text = "xoxb-1234567890-1234567890-abcdefghijklmnopqrstuvwx"
    assert REDACTION in scrub(text)


def test_jwt_redacted() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = scrub(f"here's my jwt: {jwt}")
    assert "eyJhbGciOi" not in out


def test_private_key_block_redacted() -> None:
    text = """before
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAlineline
lineline
-----END RSA PRIVATE KEY-----
after"""
    out = scrub(text)
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "before" in out
    assert "after" in out


def test_postgres_dsn_with_password_redacted() -> None:
    text = "connect: postgres://user:s3cr3t@host:5432/db"
    out = scrub(text)
    assert "s3cr3t" not in out
    assert "connect:" in out


def test_dsn_without_password_untouched() -> None:
    # No credentials embedded — not a match
    text = "visit https://example.com/path?q=1"
    assert scrub(text) == text


def test_ordinary_text_passes_through() -> None:
    samples = [
        "hello how are you",
        "the order id is YLL-1234",
        "reply by 5pm",
        "@alice please review",
        "[Alice Example](tg://user?id=12345)",
        "see https://gitlab.example.com/some/repo",
    ]
    for s in samples:
        assert scrub(s) == s, f"false-positive on: {s!r}"


def test_contains_secret_predicate() -> None:
    assert contains_secret("password: sk-abcdefghijklmnopqrstu")
    assert not contains_secret("ordinary message")
    assert not contains_secret("")


def test_empty_and_none_safe() -> None:
    assert scrub("") == ""
    # scrub treats None-like by early return (we pass strings in practice)
    assert scrub(None) is None  # type: ignore[arg-type]


def test_redaction_sentinel_is_obvious() -> None:
    # Important: redaction must be visibly a placeholder, not plausible
    # text. A reader seeing the DB should immediately spot it.
    assert "[" in REDACTION and "]" in REDACTION
    assert "REDACTED" in REDACTION
