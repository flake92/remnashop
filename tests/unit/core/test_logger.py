from src.core.logger import sanitize_log_text


def test_sanitize_log_text_redacts_email_and_credentials() -> None:
    message = (
        "delivery to person@example.com failed; "
        "password=hunter2 token='abc.def' Authorization: Bearer bearer-secret "
        'signature="signed-value"'
    )

    sanitized = sanitize_log_text(message)

    assert "person@example.com" not in sanitized
    assert "hunter2" not in sanitized
    assert "abc.def" not in sanitized
    assert "bearer-secret" not in sanitized
    assert "signed-value" not in sanitized
    assert sanitized.count("[REDACTED]") >= 4


def test_sanitize_log_text_keeps_human_readable_context() -> None:
    assert sanitize_log_text("Payment processing failed for gateway yookassa") == (
        "Payment processing failed for gateway yookassa"
    )
