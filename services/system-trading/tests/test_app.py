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
    module.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
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


def test_paper_state_requires_explicit_paper_only_marker(tmp_path):
    module = load_app(tmp_path)
    module.PAPER_STATE_PATH = tmp_path / "paper-state.json"
    module.PAPER_STATE_PATH.write_text('{"mode":"LIVE","live_trading":true}', encoding="utf-8")
    assert module.load_paper_state() is None
    module.PAPER_STATE_PATH.write_text('{"mode":"PAPER_ONLY","live_trading":false,"equity":10000}', encoding="utf-8")
    assert module.load_paper_state()["equity"] == 10000


def test_normalize_portfolio_filters_zero_and_positions(tmp_path):
    module = load_app(tmp_path)
    result = module.normalize_portfolio(
        {"data": {"balances": [{"asset": "USDT", "free": "12.5", "locked": "0.5"}, {"asset": "BTC", "free": "0", "locked": "0"}]}},
        {"data": [{"asset": "USDT", "balance": "20", "equity": "21", "availableMargin": "18", "unrealizedProfit": "1"}]},
        {"data": [{"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.01", "avgPrice": "60000", "unrealizedProfit": "2", "leverage": 2, "liquidationPrice": "30000"}]},
    )
    assert [row["asset"] for row in result["spot"]] == ["USDT"]
    assert str(result["spot"][0]["total"]) == "13.0"
    assert [row["asset"] for row in result["futures"]] == ["USDT"]
    assert result["positions"][0]["symbol"] == "BTC-USDT"


def test_portfolio_uses_read_only_paths_and_does_not_audit_values(tmp_path, monkeypatch):
    module = load_app(tmp_path)
    module.save_credentials("K" * 32, "Z" * 32)
    calls = []

    def fake_signed_get(path, api_key, secret_key):
        calls.append(path)
        if "spot" in path:
            return {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "7.25", "locked": "0"}]}}
        if "balance" in path:
            return {"code": 0, "data": [{"asset": "USDT", "balance": "4", "equity": "4", "availableMargin": "4"}]}
        return {"code": 0, "data": []}

    monkeypatch.setattr(module, "signed_get", fake_signed_get)
    client = module.app.test_client()
    client.get("/login")
    csrf = csrf_from_session(client)
    client.post("/login", data={"password": "a-secure-test-password", "csrf": csrf})
    client.get("/")
    csrf = csrf_from_session(client)
    response = client.post("/portfolio", data={"csrf": csrf})
    assert response.status_code == 200
    assert calls == [
        "/openApi/spot/v1/account/balance",
        "/openApi/swap/v3/user/balance",
        "/openApi/swap/v2/user/positions",
    ]
    assert b"7.25" in response.data
    assert b"4" in response.data
    audit_text = module.AUDIT_PATH.read_text(encoding="utf-8")
    assert "7.25" not in audit_text
    assert '"detail":"spot_assets=1,futures_assets=1,positions=0"' in audit_text
