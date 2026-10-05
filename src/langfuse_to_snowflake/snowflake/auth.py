"""Key-pair authentication."""

from __future__ import annotations

from ..config import SnowflakeSettings


def load_private_key(settings: SnowflakeSettings) -> bytes:
    """The configured RSA key as unencrypted PKCS#8 DER, the form the connector takes."""
    from cryptography.hazmat.primitives import serialization

    if settings.private_key is not None:
        # Secret stores and .env files often hold the PEM with literal "\n".
        pem = settings.private_key.get_secret_value().replace("\\n", "\n").encode()
    else:
        assert settings.private_key_path is not None
        pem = settings.private_key_path.expanduser().read_bytes()
    passphrase = settings.private_key_passphrase
    key = serialization.load_pem_private_key(
        pem, password=passphrase.get_secret_value().encode() if passphrase else None
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
