"""Шифрование менеджера паролей.

Мастер-пароль -> ключ через scrypt (с солью) -> Fernet (AES-128-CBC + HMAC).
Сам мастер-пароль нигде не хранится: в базе лежат только соль и
проверочная строка, зашифрованная ключом.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

import db

_CHECK = b"vault-ok"


def _derive_key(master: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(
        master.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return base64.urlsafe_b64encode(raw)


def is_set() -> bool:
    return db.get_meta("salt") is not None


def setup(master: str) -> Fernet:
    """Первый запуск: задаём мастер-пароль."""
    salt = os.urandom(16)
    fernet = Fernet(_derive_key(master, salt))
    db.set_meta("salt", salt)
    db.set_meta("check", fernet.encrypt(_CHECK))
    return fernet


def unlock(master: str) -> Fernet | None:
    """Возвращает Fernet, если мастер-пароль верный, иначе None."""
    salt = db.get_meta("salt")
    token = db.get_meta("check")
    if salt is None or token is None:
        return None
    fernet = Fernet(_derive_key(master, salt))
    try:
        return fernet if fernet.decrypt(token) == _CHECK else None
    except InvalidToken:
        return None
