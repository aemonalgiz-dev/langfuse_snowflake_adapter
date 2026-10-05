from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from langfuse_to_snowflake.config import SnowflakeSettings
from langfuse_to_snowflake.snowflake import load_private_key


def _pem(passphrase: bytes | None = None) -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )
    return key, pem


def _settings(**overrides) -> SnowflakeSettings:
    values = {
        "account": "acct",
        "user": "loader",
        "warehouse": "WH",
        "database": "DB",
        "schema_name": "LANGFUSE",
    }
    return SnowflakeSettings(_env_file=None, **values, **overrides)


def _public_numbers(der: bytes):
    return serialization.load_der_private_key(der, password=None).public_key().public_numbers()


def test_private_key_from_an_encrypted_file(tmp_path):
    key, pem = _pem(passphrase=b"hunter2")
    path = tmp_path / "rsa_key.p8"
    path.write_bytes(pem)

    der = load_private_key(_settings(private_key_path=path, private_key_passphrase="hunter2"))

    assert _public_numbers(der) == key.public_key().public_numbers()


def test_private_key_inline_with_escaped_newlines():
    key, pem = _pem()
    inline = pem.decode().replace("\n", "\\n")

    der = load_private_key(_settings(private_key=inline))

    assert _public_numbers(der) == key.public_key().public_numbers()
