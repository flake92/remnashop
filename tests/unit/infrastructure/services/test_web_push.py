import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.infrastructure.services.web_push import (
    PushResult,
    WebPushClient,
    b64url_decode,
    b64url_encode,
    decrypt,
    encrypt,
    generate_vapid_keys,
    is_allowed_endpoint,
    vapid_authorization,
    vapid_private_key,
)

# RFC 8291, Appendix A.
RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
    "WAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexS"
    "gSxsj_Qulcy4a-fN"
)


def _private(raw: str) -> ec.EllipticCurvePrivateKey:
    return vapid_private_key(raw)


def test_encrypt_matches_rfc8291_example() -> None:
    body = encrypt(
        RFC_PLAINTEXT,
        ua_public=b64url_decode(RFC_UA_PUBLIC),
        auth_secret=b64url_decode(RFC_AUTH),
        as_private=_private(RFC_AS_PRIVATE),
        salt=b64url_decode(RFC_SALT),
    )

    assert b64url_encode(body) == RFC_BODY


def test_receiver_decrypts_a_random_message() -> None:
    receiver = ec.generate_private_key(ec.SECP256R1())
    ua_public = receiver.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    payload = '{"title":"Подписка","body":"скоро закончится"}'.encode()

    body = encrypt(payload, ua_public=ua_public, auth_secret=auth)

    assert decrypt(body, ua_private=receiver, auth_secret=auth) == payload


def test_rfc_example_decrypts_with_receiver_key() -> None:
    assert (
        decrypt(
            b64url_decode(RFC_BODY),
            ua_private=_private(RFC_UA_PRIVATE),
            auth_secret=b64url_decode(RFC_AUTH),
        )
        == RFC_PLAINTEXT
    )


def test_oversized_payload_is_rejected() -> None:
    with pytest.raises(ValueError):
        encrypt(
            b"x" * 3001,
            ua_public=b64url_decode(RFC_UA_PUBLIC),
            auth_secret=b64url_decode(RFC_AUTH),
        )


def test_vapid_token_is_es256_for_the_push_service_origin() -> None:
    key = _private(RFC_AS_PRIVATE)
    public = b64url_encode(
        key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )

    header = vapid_authorization(
        "https://fcm.googleapis.com/fcm/send/abc",
        private_key=key,
        public_key_b64url=public,
        subject="mailto:support@example.com",
        now=1_700_000_000,
    )

    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert header.endswith(f"k={public}")
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["ES256"],
        audience="https://fcm.googleapis.com",
        options={"verify_exp": False},
    )
    assert claims["sub"] == "mailto:support@example.com"
    assert claims["exp"] == 1_700_000_000 + 12 * 3600


@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    [
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
        ("https://web.push.apple.com/QAbc", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=abc", True),
        ("http://fcm.googleapis.com/fcm/send/abc", False),
        ("https://fcm.googleapis.com.evil.example/x", False),
        ("https://evilfcm.googleapis.com.example/x", False),
        ("https://127.0.0.1/x", False),
        ("https://user@fcm.googleapis.com/x", False),
        ("https://fcm.googleapis.com:8443/x", False),
        ("https://remnashop:5000/api", False),
    ],
)
def test_only_known_push_services_are_contacted(endpoint: str, allowed: bool) -> None:
    assert is_allowed_endpoint(endpoint) is allowed


def test_push_result_classification() -> None:
    assert PushResult(201).delivered
    assert PushResult(410).gone and PushResult(404).gone
    assert PushResult(429).retryable and PushResult(503).retryable and PushResult(0).retryable
    assert not PushResult(400).retryable and not PushResult(413).retryable


def test_generated_vapid_pair_is_consistent() -> None:
    public_key, private_key = generate_vapid_keys()

    assert len(b64url_decode(public_key)) == 65
    WebPushClient(
        public_key_b64url=public_key,
        private_key_b64url=private_key,
        subject="mailto:support@example.com",
    )
    with pytest.raises(ValueError):
        WebPushClient(
            public_key_b64url=generate_vapid_keys()[0],
            private_key_b64url=private_key,
            subject="mailto:support@example.com",
        )
