from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.models.expressions import Expression
from django.utils.functional import cached_property

_TypeAny = Any


class EncryptedFieldMixin:
    @cached_property
    def keys(self) -> list[bytes]:
        keys = []
        salt_keys = (
            settings.SALT_KEY
            if isinstance(settings.SALT_KEY, list)
            else [settings.SALT_KEY]
        )
        secret_keys = [settings.SECRET_KEY] + getattr(settings, "SECRET_KEY_FALLBACKS", list())
        for secret_key in secret_keys:
            for salt_key in salt_keys:
                salt = bytes(salt_key, "utf-8")
                kdf = PBKDF2HMAC(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=salt,
                    iterations=100_000,
                    backend=default_backend(),
                )
                keys.append(
                    kdf.derive(secret_key.encode("utf-8"))
                )
        return keys

    def encrypt_value(self, plaintext: str) -> str:
        # Use the first key for encryption.
        key = self.keys[0]
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)  # Recommended size for GCM nonce is 12 bytes.
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        encrypted = nonce + ciphertext
        return base64.urlsafe_b64encode(encrypted).decode("utf-8")

    def decrypt_value(self, encrypted_value: str) -> str:
        try:
            encrypted = base64.urlsafe_b64decode(encrypted_value.encode("utf-8"))
        except Exception:
            # If it is not properly encoded, assume it is plain text.
            return encrypted_value
        # The nonce is the first 12 bytes.
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        # Try decryption with each key for key rotation support.
        for key in self.keys:
            aesgcm = AESGCM(key)
            try:
                plaintext = aesgcm.decrypt(nonce, ciphertext, None)
                return plaintext.decode("utf-8")
            except Exception:
                continue
        # If none of the keys worked, raise an exception.
        raise ValueError("Decryption failed with all provided keys.")

    def get_internal_type(self) -> str:
        # Treat everything as text
        return "TextField"

    def get_prep_value(self, value: _TypeAny) -> _TypeAny:
        value = super().get_prep_value(value)
        if value:
            if not isinstance(value, str):
                value = str(value)
            return self.encrypt_value(value)
        return None

    def get_db_prep_value(
        self,
        value: _TypeAny,
        connection: BaseDatabaseWrapper,  # noqa: ARG002
        prepared: bool = False,  # noqa: FBT001, FBT002
    ) -> _TypeAny:
        if not prepared:
            value = self.get_prep_value(value)
        return value

    def from_db_value(
        self,
        value: _TypeAny,
        expression: Expression,  # noqa: ARG002
        connection: BaseDatabaseWrapper,  # noqa: ARG002
    ) -> _TypeAny:
        return self.to_python(value)

    def to_python(self, value: _TypeAny) -> _TypeAny:
        if (
            value is None
            or not isinstance(value, str)
            or hasattr(self, "_already_decrypted")
        ):
            return value
        try:
            decrypted_value = self.decrypt_value(value)
        except Exception:
            decrypted_value = value
        return super().to_python(decrypted_value)

    def clean(self, value: _TypeAny, model_instance: models.Field) -> _TypeAny:
        # Prevent repeated decryption during form cleaning.
        self._already_decrypted = True
        ret = super().clean(value, model_instance)
        del self._already_decrypted
        return ret


class EncryptedCharField(EncryptedFieldMixin, models.CharField):
    pass


class EncryptedTextField(EncryptedFieldMixin, models.TextField):
    pass


class EncryptedDateTimeField(EncryptedFieldMixin, models.DateTimeField):
    pass


class EncryptedIntegerField(EncryptedFieldMixin, models.IntegerField):
    @cached_property
    def validators(self) -> list[MinValueValidator | MaxValueValidator]:
        # Validators are added based on connection information at runtime.
        validators_ = [*self.default_validators, *self._validators]
        internal_type = models.IntegerField().get_internal_type()
        min_value, max_value = BaseDatabaseOperations.integer_field_ranges[internal_type]
        if min_value is not None and not any(
            (
                isinstance(validator, MinValueValidator)
                and (
                    validator.limit_value()
                    if callable(validator.limit_value)
                    else validator.limit_value
                )
                >= min_value
            )
            for validator in validators_
        ):
            validators_.append(MinValueValidator(min_value))
        if max_value is not None and not any(
            (
                isinstance(validator, MaxValueValidator)
                and (
                    validator.limit_value()
                    if callable(validator.limit_value)
                    else validator.limit_value
                )
                <= max_value
            )
            for validator in validators_
        ):
            validators_.append(MaxValueValidator(max_value))
        return validators_


class EncryptedDateField(EncryptedFieldMixin, models.DateField):
    pass


class EncryptedFloatField(EncryptedFieldMixin, models.FloatField):
    pass


class EncryptedEmailField(EncryptedFieldMixin, models.EmailField):
    pass


class EncryptedBooleanField(EncryptedFieldMixin, models.BooleanField):
    pass


class EncryptedJSONField(EncryptedFieldMixin, models.JSONField):
    def _encrypt_values(self, value: _TypeAny) -> _TypeAny:
        if isinstance(value, dict):
            return {key: self._encrypt_values(data) for key, data in value.items()}
        if isinstance(value, list):
            return [self._encrypt_values(data) for data in value]
        # Convert the value to a string and encrypt it.
        plain = str(value)
        return self.encrypt_value(plain)

    def _decrypt_values(self, value: _TypeAny) -> _TypeAny:
        if value is None:
            return value
        if isinstance(value, dict):
            return {key: self._decrypt_values(data) for key, data in value.items()}
        if isinstance(value, list):
            return [self._decrypt_values(data) for data in value]
        # Attempt decryption; if it fails, return the value as-is.
        try:
            return self.decrypt_value(str(value))
        except Exception:
            return value

    def get_prep_value(self, value: _TypeAny) -> str:
        # Encrypt each value within the JSON structure and then dump to JSON.
        return json.dumps(self._encrypt_values(value=value), cls=self.encoder)

    def get_internal_type(self) -> str:
        return "JSONField"

    def to_python(self, value: _TypeAny) -> _TypeAny:
        if (
            value is None
            or not isinstance(value, str)
            or hasattr(self, "_already_decrypted")
        ):
            return value
        try:
            loaded = json.loads(value)
            value = self._decrypt_values(loaded)
        except Exception:
            pass
        return super().to_python(value)
