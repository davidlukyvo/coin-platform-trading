#!/usr/bin/env python3
import base64
import getpass
import hashlib
import os
import secrets
import sys
from pathlib import Path


def write_exclusive(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "secrets/system-trading")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    master_path = root / "master.key"
    password_path = root / "admin-password.scrypt"
    if master_path.exists() or password_path.exists():
        print(f"Refusing to overwrite existing secrets under {root}", file=sys.stderr)
        return 2
    password = getpass.getpass("New System Trading admin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match", file=sys.stderr)
        return 2
    if len(password) < 14:
        print("Password must contain at least 14 characters", file=sys.stderr)
        return 2
    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    salt = secrets.token_bytes(16)
    n, r, p = 16384, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    encoded = "$".join(
        ["scrypt", str(n), str(r), str(p), base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode()]
    ).encode("ascii")
    try:
        write_exclusive(master_path, master_key + b"\n")
        write_exclusive(password_path, encoded + b"\n")
        os.chown(root, 0, 10004)
        os.chmod(root, 0o750)
        for path in (master_path, password_path):
            os.chown(path, 0, 10004)
            os.chmod(path, 0o640)
    except Exception:
        master_path.unlink(missing_ok=True)
        password_path.unlink(missing_ok=True)
        raise
    print(f"Created encrypted-vault key and password hash under {root} for service group 10004")
    print("Back up master.key securely; losing it makes the credential vault unreadable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
