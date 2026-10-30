"""Web Push: message encryption (RFC 8291) and VAPID (RFC 8292)."""

import base64
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import aiohttp
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
# Push services accept at least 4096 bytes of encrypted body.
MAX_PAYLOAD_BYTES = 3000
VAPID_TOKEN_TTL_SECONDS = 12 * 60 * 60

# Subscription endpoints come from the browser via the user. Only the major
# push services are contacted, so an endpoint cannot point the server at an
# internal address.
ALLOWED_PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def is_allowed_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and any(
            host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_PUSH_HOST_SUFFIXES
        )
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def encrypt(
    payload: bytes,
    *,
    ua_public: bytes,
    auth_secret: bytes,
    as_private: Optional[ec.EllipticCurvePrivateKey] = None,
    salt: Optional[bytes] = None,
) -> bytes:
    """Encrypt one push message as a single aes128gcm record (RFC 8291)."""

    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("push payload is too large")
    if len(auth_secret) != 16:
        raise ValueError("auth secret must be 16 bytes")
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    sender = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt if salt is not None else os.urandom(16)
    as_public = _public_bytes(sender.public_key())

    ecdh_secret = sender.exchange(ec.ECDH(), receiver)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hkdf(auth_secret, ecdh_secret, key_info, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # 0x02 marks the last (and only) record; no padding.
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt(
    body: bytes,
    *,
    ua_private: ec.EllipticCurvePrivateKey,
    auth_secret: bytes,
) -> bytes:
    """Receiver side of :func:`encrypt`; used by tests only."""

    salt, idlen = body[:16], body[20]
    as_public = body[21 : 21 + idlen]
    ciphertext = body[21 + idlen :]
    ua_public = _public_bytes(ua_private.public_key())
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh_secret = ua_private.exchange(ec.ECDH(), sender)
    ikm = _hkdf(auth_secret, ecdh_secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plaintext = AESGCM(cek).decrypt(nonce, ciphertext, None)
    return plaintext.rstrip(b"\x00")[:-1]


def vapid_private_key(raw_b64url: str) -> ec.EllipticCurvePrivateKey:
    secret = int.from_bytes(b64url_decode(raw_b64url.strip()), "big")
    return ec.derive_private_key(secret, ec.SECP256R1())


def vapid_authorization(
    endpoint: str,
    *,
    private_key: ec.EllipticCurvePrivateKey,
    public_key_b64url: str,
    subject: str,
    now: Optional[float] = None,
) -> str:
    parsed = urlsplit(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    issued = int(now if now is not None else time.time())
    token = jwt.encode(
        {"aud": audience, "exp": issued + VAPID_TOKEN_TTL_SECONDS, "sub": subject},
        private_key,
        algorithm="ES256",
        headers={"typ": "JWT"},
    )
    return f"vapid t={token}, k={public_key_b64url}"


@dataclass(frozen=True)
class PushTarget:
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class PushResult:
    status: int

    @property
    def delivered(self) -> bool:
        return self.status in (200, 201, 202)

    @property
    def gone(self) -> bool:
        # The browser unsubscribed or the subscription expired.
        return self.status in (404, 410)

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500


class WebPushClient:
    def __init__(
        self,
        *,
        public_key_b64url: str,
        private_key_b64url: str,
        subject: str,
        timeout_seconds: float = 15,
    ) -> None:
        self._public_key = public_key_b64url
        self._private_key = vapid_private_key(private_key_b64url)
        if b64url_encode(_public_bytes(self._private_key.public_key())) != public_key_b64url:
            raise ValueError("WEB_PUSH_VAPID_PUBLIC_KEY does not match the private key")
        self._subject = subject
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def send(
        self,
        session: aiohttp.ClientSession,
        target: PushTarget,
        payload: bytes,
        *,
        ttl_seconds: int,
        urgency: str,
        topic: Optional[str] = None,
    ) -> PushResult:
        if not is_allowed_endpoint(target.endpoint):
            return PushResult(status=410)
        body = encrypt(
            payload,
            ua_public=b64url_decode(target.p256dh),
            auth_secret=b64url_decode(target.auth),
        )
        headers = {
            "TTL": str(ttl_seconds),
            "Urgency": urgency,
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Authorization": vapid_authorization(
                target.endpoint,
                private_key=self._private_key,
                public_key_b64url=self._public_key,
                subject=self._subject,
            ),
        }
        if topic:
            headers["Topic"] = topic
        try:
            async with session.post(
                target.endpoint,
                data=body,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                await response.read()
                return PushResult(status=response.status)
        except (aiohttp.ClientError, TimeoutError):
            return PushResult(status=0)


__all__ = [
    "ALLOWED_PUSH_HOST_SUFFIXES",
    "MAX_PAYLOAD_BYTES",
    "PushResult",
    "PushTarget",
    "WebPushClient",
    "b64url_decode",
    "b64url_encode",
    "decrypt",
    "encrypt",
    "generate_vapid_keys",
    "is_allowed_endpoint",
    "vapid_authorization",
    "vapid_private_key",
]


def generate_vapid_keys() -> tuple[str, str]:
    """A new VAPID pair as base64url (public uncompressed point, private scalar)."""

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return b64url_encode(_public_bytes(key.public_key())), b64url_encode(private)


if __name__ == "__main__":
    public_key, private_key = generate_vapid_keys()
    sys.stdout.write(f"WEB_PUSH_VAPID_PUBLIC_KEY={public_key}\n")
    sys.stdout.write(f"WEB_PUSH_VAPID_PRIVATE_KEY={private_key}\n")
