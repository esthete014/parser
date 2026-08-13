import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

SECRET_FILE = Path("data/.secret")


def _ensure_secret() -> bytes:
    """Возвращает ключ из data/.secret, создаёт файл, если его нет."""
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()

    logger.info("🔑 Генерация Fernet-ключа → %s", SECRET_FILE)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    SECRET_FILE.write_bytes(key)
    # Только владелец может читать (Unix)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except NotImplementedError:
        pass
    return key


def encrypt(plaintext: str) -> str:
    key = _ensure_secret()
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    key = _ensure_secret()
    f = Fernet(key)
    return f.decrypt(token.encode()).decode()


def has_key() -> bool:
    return SECRET_FILE.exists()
