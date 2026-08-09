import base64
import hashlib
import importlib.util
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def password_hash(password: str) -> str:
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "$".join(
        ["scrypt", "16384", "8", "1", base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode()]
    )


def load_app(tmp_path: Path):
    secret_dir = tmp_path / "secrets"
    data_dir = tmp_path / "data"
    secret_dir.mkdir()
    data_dir.mkdir()
    (secret_dir / "master.key").write_bytes(Fernet.generate_key())
    (secret_dir / "admin-password.scrypt").write_text(password_hash("a-secure-test-password"), encoding="ascii")
    os.environ["SYSTEM_TRADING_SECRET_DIR"] = str(secret_dir)
    os.environ["SYSTEM_TRADING_DATA_DIR"] = str(data_dir)
    sys.modules.pop("system_trading_app", None)
    spec = importlib.util.spec_from_file_location("system_trading_app", Path(__file__).parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


def csrf_from_session(client) -> str:
    with client.session_transaction() as session:
        return session["csrf"]


def test_password_hash_and_encrypted_roundtrip(tmp_path):
    module = load_app(tmp_path)
    assert module.verify_password("a-secure-test-password", module.ADMIN_PASSWORD_HASH)
    assert not module.verify_password("wrong", module.ADMIN_PASSWORD_HASH)
    module.save_credentials("A" * 32, "S" * 32)
    assert module.load_credentials() == {"api_key": "A" * 32, "secret_key": "S" * 32}
    ciphertext = module.VAULT_PATH.read_bytes()
    assert b"A" * 12 not in ciphertext
    assert b"S" * 12 not in ciphertext
    if os.name != "nt":
        assert oct(module.VAULT_PATH.stat().st_mode & 0o777) == "0o600"


def test_login_csrf_and_no_secret_echo(tmp_path):
    module = load_app(tmp_path)
    client = module.app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/login").status_code == 200
    csrf = csrf_from_session(client)
    response = client.post("/login", data={"password": "a-secure-test-password", "csrf": csrf})
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    with client.session_transaction() as session:
        csrf = session["csrf"]
    api_key, secret_key = "K" * 32, "Z" * 32
    response = client.post(
        "/credentials",
        data={"csrf": csrf, "api_key": api_key, "secret_key": secret_key, "confirm_no_withdraw": "yes"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert api_key.encode() not in response.data
    assert secret_key.encode() not in response.data
    assert b"KKKK" in response.data
    assert b"ZZZZ" not in response.data


def test_health_declares_trading_disabled(tmp_path):
    module = load_app(tmp_path)
    payload = module.app.test_client().get("/healthz").get_json()
    assert payload == {"status": "healthy", "trading_enabled": False, "dry_run": True}
