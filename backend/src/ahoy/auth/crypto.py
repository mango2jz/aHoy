"""AES-256-GCM for GitHub user tokens at rest, and HKDF subkeys (SPEC -> Token storage).

The key is TOKEN_ENCRYPTION_KEY: 32 random bytes, base64url (padding optional). The app
parses it once at startup (`load_key`), so a missing or malformed key fails there, not at the
first sign-in. Each encryption uses a fresh random 12-byte nonce and binds the ciphertext to
associated data naming its owner and field (`token_aad`), so a ciphertext copied to another
user, or swapped between the access and refresh columns, does not decrypt.
"""

import base64
import binascii
import os
import secrets
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from ahoy.config import ConfigError

KEY_BYTES = 32
NONCE_BYTES = 12


class CryptoError(Exception):
    """A ciphertext did not decrypt (wrong key, wrong associated data, or tampered)."""


def load_key(key: SecretStr | None) -> bytes:
    """The raw 32-byte key. Raises ConfigError (never showing the value) if unusable."""
    raw = key.get_secret_value().strip() if key is not None else ""
    if not raw:
        raise ConfigError("Missing required setting TOKEN_ENCRYPTION_KEY")
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError("TOKEN_ENCRYPTION_KEY is not valid base64url") from None
    if len(decoded) != KEY_BYTES:
        raise ConfigError(
            f"TOKEN_ENCRYPTION_KEY must decode to {KEY_BYTES} bytes (got {len(decoded)})"
        )
    return decoded


def generate_key() -> SecretStr:
    """A fresh random key in the TOKEN_ENCRYPTION_KEY format."""
    return SecretStr(base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode())


TokenField = Literal["access", "refresh"]


def token_aad(user_id: UUID, field: TokenField) -> str:
    """Associated data for one stored token: `<user id>:<field>`."""
    return f"{user_id}:{field}"


def encrypt(key: SecretStr, plaintext: str, aad: str) -> tuple[bytes, bytes]:
    """(ciphertext with tag, nonce)."""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(load_key(key)).encrypt(nonce, plaintext.encode(), aad.encode())
    return ciphertext, nonce


def decrypt(key: SecretStr, ciphertext: bytes, nonce: bytes, aad: str) -> SecretStr:
    try:
        plain = AESGCM(load_key(key)).decrypt(nonce, ciphertext, aad.encode())
    except (InvalidTag, ValueError):
        raise CryptoError("token decryption failed") from None
    return SecretStr(plain.decode())


def derive_subkey(key: SecretStr, info: bytes) -> bytes:
    """A 32-byte HKDF-SHA256 subkey of the token key for a separate purpose (`info`)."""
    if not info:
        raise ValueError("derive_subkey needs a non-empty info label")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info)
    return hkdf.derive(load_key(key))
