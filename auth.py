"""Server-side authentication gate.

Design notes (this app handles electoral-roll PII, so the bar is high):
  * No credential ever lives in the code or the repo. The username and a
    PBKDF2-SHA256 password *hash* come from the environment; the plaintext
    password exists only in the operator's head.
  * Verification is constant-time (hmac.compare_digest) for BOTH the username
    and the password, so neither can be discovered by timing.
  * The same generic error is shown whether the user or the password was
    wrong -- never reveal which.
  * Brute force is throttled per-session AND globally (the process is the
    single container), with a hard lockout window.
  * Auth state lives in Streamlit's server-side session_state. It is never a
    client-supplied cookie/param, so it cannot be forged from the browser.
  * Sessions expire, so an unattended tab does not stay authenticated forever.

Generate a hash with:  python make_password.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import deque

import streamlit as st

# --- tunables -------------------------------------------------------------
PBKDF2_ITERATIONS = 600_000        # OWASP-recommended floor for SHA-256
SESSION_TIMEOUT_S = 60 * 60        # re-login after 1 hour idle
MAX_FAILS_PER_SESSION = 5
GLOBAL_FAIL_LIMIT = 15             # across all sessions...
GLOBAL_FAIL_WINDOW_S = 300         # ...within this window
GLOBAL_LOCKOUT_S = 900             # then lock everyone out this long
FAILED_LOGIN_DELAY_S = 1.0         # slow down automated guessing

# Global (per-process) brute-force tracking. The app runs as one container,
# so module state is shared across all browser sessions.
_recent_failures: deque[float] = deque()
_locked_until: float = 0.0


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return 'pbkdf2_sha256:<iters>:<salt_hex>:<hash_hex>'.

    The separator is ':' and never '$'. A '$' would be eaten by Docker Compose
    variable interpolation on the way into the container, silently corrupting
    the hash so that every login fails.
    """
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256:{PBKDF2_ITERATIONS}:{salt.hex()}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    # '$' is still accepted so hashes minted by older builds keep working.
    sep = ":" if ":" in stored else "$"
    try:
        algo, iters, salt_hex, hash_hex = stored.strip().split(sep)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _hash_is_wellformed(stored: str) -> bool:
    """True if `stored` has the shape of a hash we could verify against."""
    sep = ":" if ":" in stored else "$"
    parts = stored.strip().split(sep)
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    _, iters, salt_hex, hash_hex = parts
    try:
        return (int(iters) > 0
                and bool(salt_hex) and bool(hash_hex)
                and bytes.fromhex(salt_hex) is not None
                and bytes.fromhex(hash_hex) is not None)
    except ValueError:
        return False


def _globally_locked() -> float:
    """Seconds remaining on the global lockout (0 if not locked)."""
    global _locked_until
    now = time.time()
    while _recent_failures and now - _recent_failures[0] > GLOBAL_FAIL_WINDOW_S:
        _recent_failures.popleft()
    if len(_recent_failures) >= GLOBAL_FAIL_LIMIT and now >= _locked_until:
        _locked_until = now + GLOBAL_LOCKOUT_S
        _recent_failures.clear()
    return max(0.0, _locked_until - now)


def _record_failure() -> None:
    _recent_failures.append(time.time())
    st.session_state["_auth_fails"] = st.session_state.get("_auth_fails", 0) + 1


def is_authenticated() -> bool:
    if not st.session_state.get("_authed"):
        return False
    if time.time() - st.session_state.get("_authed_at", 0) > SESSION_TIMEOUT_S:
        st.session_state["_authed"] = False       # expired
        return False
    return True


def logout() -> None:
    for k in ("_authed", "_authed_at", "_auth_fails"):
        st.session_state.pop(k, None)


def require_auth() -> None:
    """Gate the page. Call as the FIRST thing on every page, before any data
    is read or rendered. Halts the script if the visitor is not signed in."""
    user_env = os.getenv("APP_USERNAME", "")
    hash_env = os.getenv("APP_PASSWORD_HASH", "")

    # Fail CLOSED: with no credentials configured the app must not be usable.
    if not user_env or not hash_env:
        st.error("🔒 Authentication is not configured. Set APP_USERNAME and "
                 "APP_PASSWORD_HASH on the server. Access is denied until then.")
        st.stop()

    # A hash that cannot possibly verify is an operator error, not a bad guess.
    # Say so, instead of rejecting every correct password as "invalid".
    if not _hash_is_wellformed(hash_env):
        st.error("🔒 APP_PASSWORD_HASH is malformed, so no password can ever "
                 "match it. If the value contains '$', Docker Compose ate part "
                 "of it -- regenerate with `python make_password.py` and paste "
                 "the ':'-separated hash. Access is denied until then.")
        st.stop()

    if is_authenticated():
        with st.sidebar:
            st.caption(f"Signed in as **{user_env}**")
            if st.button("Sign out", use_container_width=True):
                logout()
                st.rerun()
        return

    # --- not signed in: render only the login form, nothing else ---------
    st.title("🔒 Sign in")
    st.caption("This system holds electoral-roll data. Authorised access only.")

    locked_for = _globally_locked()
    if locked_for > 0:
        st.error(f"Too many failed attempts. Locked for "
                 f"{int(locked_for // 60) + 1} more minute(s).")
        st.stop()

    if st.session_state.get("_auth_fails", 0) >= MAX_FAILS_PER_SESSION:
        st.error("Too many failed attempts in this session. "
                 "Reload the page to try again.")
        st.stop()

    with st.form("login", clear_on_submit=True):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password",
                                 autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        # Constant-time on BOTH fields; always run the KDF so that a wrong
        # username costs the same as a wrong password (no timing oracle).
        user_ok = hmac.compare_digest(username or "", user_env)
        pass_ok = _verify_password(password or "", hash_env)
        if user_ok and pass_ok:
            st.session_state["_authed"] = True
            st.session_state["_authed_at"] = time.time()
            st.session_state["_auth_fails"] = 0
            st.rerun()
        else:
            _record_failure()
            time.sleep(FAILED_LOGIN_DELAY_S)
            st.error("Invalid credentials.")   # never say which field was wrong

    st.stop()
