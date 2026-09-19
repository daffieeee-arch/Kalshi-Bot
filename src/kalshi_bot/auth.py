"""RSA-PSS request signing for Kalshi Trade API (official API key scheme)."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def load_private_key(path: Path) -> RSAPrivateKey:
    """Load an RSA private key from a PEM `.key` / `.pem` file."""
    pem = path.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None, backend=default_backend())
    if not isinstance(key, RSAPrivateKey):
        raise ValueError(f"Expected an RSA private key at {path}")
    return key


def sign_pss_text(private_key: RSAPrivateKey, text: str) -> str:
    """Sign `text` with RSA-PSS (SHA-256, salt_length=DIGEST_LENGTH); return base64."""
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def signing_path(url_or_path: str) -> str:
    """Path used for signatures: full URL path from root, without query string."""
    if url_or_path.startswith("http"):
        return urlparse(url_or_path).path
    return url_or_path.split("?", 1)[0]


def auth_headers(
    *,
    api_key_id: str,
    private_key: RSAPrivateKey,
    method: str,
    url_or_path: str,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Build KALSHI-ACCESS-* headers for an authenticated request."""
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    path = signing_path(url_or_path)
    message = f"{ts}{method.upper()}{path}"
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign_pss_text(private_key, message),
    }
