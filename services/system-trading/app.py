import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import wraps
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, flash, redirect, render_template, request, session, url_for
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest


LOG = logging.getLogger("system-trading")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

SECRET_DIR = Path(os.getenv("SYSTEM_TRADING_SECRET_DIR", "/run/system-trading-secrets"))
DATA_DIR = Path(os.getenv("SYSTEM_TRADING_DATA_DIR", "/data"))
VAULT_PATH = DATA_DIR / "bingx-credentials.enc"
AUDIT_PATH = DATA_DIR / "audit.ndjson"
BINGX_BASE_URL = os.getenv("BINGX_REST_BASE", "https://open-api.bingx.com").rstrip("/")


def _read_secret(name: str) -> bytes:
    path = SECRET_DIR / name
    value = path.read_bytes().strip()
    if not value:
        raise RuntimeError(f"Required secret file is empty: {path}")
    return value


MASTER_KEY = _read_secret("master.key")
ADMIN_PASSWORD_HASH = _read_secret("admin-password.scrypt").decode("ascii")
FERNET = Fernet(MASTER_KEY)

METRICS_REGISTRY = CollectorRegistry()
LOGIN_FAILURES = Counter("system_trading_login_failures_total", "Failed admin logins", registry=METRICS_REGISTRY)
CONNECTION_TESTS = Counter(
    "system_trading_connection_tests_total", "BingX read-only tests", ["result"], registry=METRICS_REGISTRY
)
VAULT_CONFIGURED = Gauge(
    "system_trading_vault_configured", "Whether a BingX credential is stored", registry=METRICS_REGISTRY
)

app = Flask(__name__)
app.secret_key = hmac.new(MASTER_KEY, b"system-trading-session-v1", hashlib.sha256).digest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=1800,
    MAX_CONTENT_LENGTH=16 * 1024,
)


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_text, r_text, p_text, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n_text), r=int(r_text), p=int(p_text), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def audit(event: str, success: bool, detail: str = "") -> None:
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "timestamp": int(time.time()),
        "event": event,
        "success": success,
        "detail": detail[:160],
        "remote": request.remote_addr,
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    os.chmod(AUDIT_PATH, 0o600)


def load_credentials() -> dict[str, str] | None:
    if not VAULT_PATH.exists():
        return None
    try:
        return json.loads(FERNET.decrypt(VAULT_PATH.read_bytes()).decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError) as exc:
        LOG.error("Credential vault cannot be decrypted: %s", type(exc).__name__)
        raise RuntimeError("Credential vault is unreadable") from exc


def save_credentials(api_key: str, secret_key: str) -> None:
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"api_key": api_key, "secret_key": secret_key}, separators=(",", ":")).encode("utf-8")
    encrypted = FERNET.encrypt(payload)
    fd, temporary = tempfile.mkstemp(dir=DATA_DIR, prefix=".bingx-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, VAULT_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def signed_get(path: str, api_key: str, secret_key: str) -> dict:
    params = {"recvWindow": "5000", "timestamp": str(int(time.time() * 1000))}
    signing_text = urllib.parse.urlencode(sorted(params.items()))
    signature = hmac.new(secret_key.encode("utf-8"), signing_text.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINGX_BASE_URL}{path}?{signing_text}&signature={signature}"
    req = urllib.request.Request(url, headers={"X-BX-APIKEY": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"BingX HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("BingX connection failed") from exc
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"BingX rejected request: code={payload.get('code')}")
    return payload


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


def require_csrf() -> None:
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "missing")):
        raise RuntimeError("Invalid CSRF token")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@app.get("/healthz")
def healthz():
    return {"status": "healthy", "trading_enabled": False, "dry_run": True}


@app.get("/metrics")
def metrics():
    VAULT_CONFIGURED.set(1 if VAULT_PATH.exists() else 0)
    return generate_latest(METRICS_REGISTRY), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            require_csrf()
        except RuntimeError:
            return render_template("login.html", error="Phiên đăng nhập không hợp lệ."), 400
        if verify_password(request.form.get("password", ""), ADMIN_PASSWORD_HASH):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            audit("login", True)
            return redirect(url_for("index"))
        audit("login", False)
        LOGIN_FAILURES.inc()
        time.sleep(0.5)
        return render_template("login.html", error="Mật khẩu không đúng."), 401
    return render_template("login.html")


@app.post("/logout")
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    credentials = load_credentials()
    masked = None
    if credentials:
        key = credentials["api_key"]
        masked = f"{key[:4]}…{key[-4:]}" if len(key) >= 10 else "••••••••"
    return render_template("index.html", configured=bool(credentials), masked_key=masked)


@app.post("/credentials")
@login_required
def credentials():
    try:
        require_csrf()
        api_key = request.form.get("api_key", "").strip()
        secret_key = request.form.get("secret_key", "").strip()
        confirmation = request.form.get("confirm_no_withdraw") == "yes"
        if not (12 <= len(api_key) <= 256 and 12 <= len(secret_key) <= 256):
            raise ValueError("API key hoặc secret không đúng độ dài.")
        if not confirmation:
            raise ValueError("Bạn phải xác nhận key không có quyền Withdraw.")
        save_credentials(api_key, secret_key)
        audit("credentials_saved", True, "bingx")
        flash("Đã mã hóa và lưu credential BingX. Chưa có lệnh giao dịch nào được gửi.", "success")
    except ValueError as exc:
        audit("credentials_saved", False, "validation")
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.post("/test-connection")
@login_required
def test_connection():
    require_csrf()
    credentials = load_credentials()
    if not credentials:
        flash("Chưa có credential BingX.", "error")
        return redirect(url_for("index"))
    try:
        payload = signed_get("/openApi/spot/v1/account/balance", credentials["api_key"], credentials["secret_key"])
        balances = payload.get("data", {}).get("balances", [])
        nonzero = sum(1 for item in balances if float(item.get("free", 0) or 0) or float(item.get("locked", 0) or 0))
        audit("spot_readonly_test", True, f"nonzero_assets={nonzero}")
        CONNECTION_TESTS.labels(result="success").inc()
        flash(f"Kết nối Spot read-only thành công; tìm thấy {nonzero} tài sản có số dư. Giá trị số dư không được ghi log.", "success")
    except (RuntimeError, ValueError) as exc:
        audit("spot_readonly_test", False, type(exc).__name__)
        CONNECTION_TESTS.labels(result="error").inc()
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.post("/credentials/delete")
@login_required
def delete_credentials():
    require_csrf()
    if request.form.get("confirm") != "DELETE":
        flash("Nhập DELETE để xóa credential đã lưu.", "error")
        return redirect(url_for("index"))
    VAULT_PATH.unlink(missing_ok=True)
    audit("credentials_deleted", True, "bingx")
    flash("Đã xóa credential BingX khỏi vault.", "success")
    return redirect(url_for("index"))


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
