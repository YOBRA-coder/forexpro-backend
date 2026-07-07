"""
Shared auth helpers (token create/decode + FastAPI dependencies).
Pulled out of forexpro_main.py so payments.py / mpesa.py can use
get_current_user without a circular import.
"""
import time, base64, hashlib
from typing import Optional
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import get_db

SECRET = "forexpro_secret_2026"
security = HTTPBearer(auto_error=False)


def create_token(user_id: int, username: str) -> str:
    payload = f"{user_id}:{username}:{int(time.time())+86400*7}"
    sig = hashlib.sha256(f"{payload}{SECRET}".encode()).hexdigest()[:16]
    return base64.b64encode(f"{payload}:{sig}".encode()).decode()


def decode_token(token: str) -> Optional[dict]:
    try:
        decoded = base64.b64decode(token.encode()).decode()
        parts = decoded.rsplit(":", 1)
        if len(parts) != 2:
            return None
        payload, sig = parts[0], parts[1]
        expected = hashlib.sha256(f"{payload}{SECRET}".encode()).hexdigest()[:16]
        if sig != expected:
            return None
        uid, uname, exp = payload.split(":", 2)
        if int(exp) < time.time():
            return None
        return {"user_id": int(uid), "username": uname}
    except Exception:
        return None


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    data = decode_token(creds.credentials)
    if not data:
        raise HTTPException(401, "Invalid or expired token")
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (data["user_id"],)).fetchone()
        if not user:
            raise HTTPException(401, "User not found")
        return dict(user)


def get_optional_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        return None
    data = decode_token(creds.credentials) if creds else None
    if not data:
        return None
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (data["user_id"],)).fetchone()
        return dict(user) if user else None
