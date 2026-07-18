import pytest

from src.core.config.app import AppConfig


@pytest.mark.parametrize(
    "url",
    [
        "https://cabinet.example.com",
        "http://localhost:4000",
        "http://127.0.0.1:4000/payment/pending",
        "http://[::1]:4000",
    ],
)
def test_web_cabinet_url_accepts_https_and_http_loopback(url: str) -> None:
    assert AppConfig.validate_web_cabinet_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://cabinet.example.com",
        "http://localhost.example.com",
        "http://localhost@attacker.example.com",
        "ftp://localhost:4000",
    ],
)
def test_web_cabinet_url_rejects_non_loopback_http(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL or an HTTP loopback URL"):
        AppConfig.validate_web_cabinet_url(url)
