"""Persisted credentials and snapshots share the deployment's explicit Fernet key."""

import json

from cryptography.fernet import Fernet, InvalidToken

from .settings import ServerError


class SecretCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError, AttributeError):
            raise ServerError(500, "secret_key_invalid", "服务加密密钥无效") from None

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError, TypeError, AttributeError, UnicodeError):
            raise ServerError(
                500, "secret_decryption_failed", "无法解密服务凭据或任务快照"
            ) from None

    def encrypt_mapping(self, mapping: dict) -> str:
        return self.encrypt(
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )

    def decrypt_mapping(self, token: str) -> dict:
        try:
            value = json.loads(self.decrypt(token))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise ServerError(
                500, "secret_decryption_failed", "无法解密服务凭据或任务快照"
            ) from None
