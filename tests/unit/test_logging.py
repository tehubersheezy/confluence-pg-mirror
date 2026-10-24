import logging

from confluence_mirror.logging_utils import REDACTED, RedactingFilter, redact, register_secret


def test_redacts_registered_secret_and_auth_headers():
    register_secret("s3cr3t-token-value")
    assert "s3cr3t" not in redact("token s3cr3t-token-value leaked")
    assert redact("Authorization: Bearer abc.def") == f"Authorization: Bearer {REDACTED}"
    assert redact("{'authorization': 'Basic Zm9v'}") == f"{{'authorization': 'Basic {REDACTED}'}}"


def test_filter_redacts_args_and_exceptions(caplog):
    register_secret("another-secret-123")
    logger = logging.getLogger("cpm.test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="cpm.test"):
        logger.info("value=%s", "another-secret-123")
        try:
            raise RuntimeError("boom another-secret-123")
        except RuntimeError:
            logger.exception("failed")
    text = caplog.text
    assert "another-secret-123" not in text
    assert REDACTED in text
