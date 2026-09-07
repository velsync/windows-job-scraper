"""Auth-directory ACL hardening and *structural* verification.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md section 6
(restrictive ACLs) and SEC-01; plan S0.7 (choices 4-5).

On Windows the pinned ``pywin32`` runtime (``win32security``/``win32api``/
``win32con``) is the authoritative primitive: the auth directory receives a
fresh protected DACL granting full control to the current user, SYSTEM and
Administrators only, with inheritance disabled.

Verification is *structural*: the effective DACL is read back and every ACE is
examined. The check proves the actual intended app-owned authority —
  * the current user retains the required access;
  * no broad principal (Everyone, Authenticated Users, Users, Guests, or any
    non-authorized account) holds write-capable access;
  * the DACL is protected (inheritance disabled).
It does not merely search for the string "Everyone" in tool output.
"""

from __future__ import annotations

import sys
from pathlib import Path

W32 = sys.platform == "win32"

# Well-known SIDs (fixed, no lookup needed).
SID_EVERYONE = "S-1-1-0"
SID_AUTHENTICATED_USERS = "S-1-5-11"
SID_USERS = "S-1-5-32-545"
SID_GUESTS = "S-1-5-32-546"
SID_POWER_USERS = "S-1-5-32-547"
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"

BROAD_PRINCIPALS = {
    SID_EVERYONE,
    SID_AUTHENTICATED_USERS,
    SID_USERS,
    SID_GUESTS,
    SID_POWER_USERS,
}

# Write-capable bits (file/directory): data write/append, EA write, delete
# child, delete, write-DAC, write-owner, plus generic-all/all-access bundles.
WRITE_CAPABLE_MASK = (
    0x00000002  # FILE_WRITE_DATA
    | 0x00000004  # FILE_APPEND_DATA
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x001F01FF  # FILE_ALL_ACCESS
)


def harden_auth_directory(path: Path) -> None:
    """Apply restrictive permissions to the auth directory (pywin32)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if W32:  # pragma: no cover - Windows native (CI + native harness)
        import win32con
        import win32file
        import win32security

        user_sid = _current_user_sid()
        dacl = win32security.ACL()
        inherit = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        for sid in (
            user_sid,
            win32security.ConvertStringSidToSid(SID_SYSTEM),
            win32security.ConvertStringSidToSid(SID_ADMINISTRATORS),
        ):
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS, inherit, win32file.FILE_ALL_ACCESS, sid
            )
        # PROTECTED_DACL disables inheritance from the parent directory.
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    else:
        import os

        os.chmod(path, 0o700)


def _current_user_sid():  # pragma: no cover - Windows native
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        info = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        token.Close()
    # pywin32 returns the SID (or a (sid, attrs) tuple depending on version).
    if isinstance(info, tuple):
        first = info[0]
        return first[0] if isinstance(first, tuple) else first
    return info


def inspect_auth_directory_acl(path: Path) -> dict[str, object]:
    """Inspect the *effective* protections of the auth directory.

    Returns a dict with ``ok`` plus machine-readable details of the actual
    authority granted: the current user's access, every ACE's principal and
    write-capability, and whether the DACL is protected from inheritance.
    ``ok`` is true only when the current user retains required access, no
    broad principal holds write authority, and every allowed principal is an
    explicitly authorized identity (current user, SYSTEM, Administrators).
    """
    path = Path(path)
    info: dict[str, object] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return {**info, "ok": False, "reason": "auth directory missing"}
    if W32:  # pragma: no cover - Windows native (CI + native harness)
        import win32security

        user_sid_str = win32security.ConvertSidToStringSid(_current_user_sid())
        try:
            sd = win32security.GetNamedSecurityInfo(
                str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
            )
            control, _revision = sd.GetSecurityDescriptorControl()
            dacl = sd.GetSecurityDescriptorDacl()
        except Exception as exc:
            return {**info, "ok": False, "reason": f"cannot read security descriptor: {exc}"}

        protected_dacl = bool(control & 0x800)  # SE_DACL_PROTECTED
        aces: list[dict[str, object]] = []
        user_has_access = False
        broad_write: list[str] = []
        unauthorized: list[str] = []
        allowed_principals = {user_sid_str, SID_SYSTEM, SID_ADMINISTRATORS}

        if dacl is not None:
            for i in range(dacl.GetAceCount()):
                ace = dacl.GetAce(i)
                # pywin32 simple ACE shape: ((ace_type, ace_flags), mask, sid).
                header, mask, sid = ace
                ace_type, _flags = header
                if ace_type != 0:  # 0 = ACCESS_ALLOWED_ACE_TYPE
                    # Deny ACEs and others are recorded but not authority.
                    aces.append(
                        {"sid": _sid_str(sid), "type": int(ace_type), "mask": int(mask)}
                    )
                    continue
                sid_str = _sid_str(sid)
                write_capable = bool(int(mask) & WRITE_CAPABLE_MASK)
                aces.append(
                    {
                        "sid": sid_str,
                        "type": "ALLOW",
                        "mask": int(mask),
                        "write_capable": write_capable,
                    }
                )
                if sid_str == user_sid_str:
                    user_has_access = True
                if sid_str in BROAD_PRINCIPALS and write_capable:
                    broad_write.append(sid_str)
                if sid_str not in allowed_principals:
                    unauthorized.append(sid_str)

        ok = (
            protected_dacl
            and user_has_access
            and not broad_write
            and not unauthorized
        )
        reasons: list[str] = []
        if not protected_dacl:
            reasons.append("DACL inherits from parent (not protected)")
        if not user_has_access:
            reasons.append("current user has no allowed ACE")
        if broad_write:
            reasons.append("broad principals with write authority: " + ",".join(broad_write))
        if unauthorized:
            reasons.append("unauthorized principals with allowed ACEs: " + ",".join(unauthorized))
        return {
            **info,
            "ok": ok,
            "reason": "; ".join(reasons) if reasons else "app-owned",
            "current_user_sid": user_sid_str,
            "protected_dacl": protected_dacl,
            "aces": aces,
            "broad_write_principals": broad_write,
            "unauthorized_principals": unauthorized,
        }
    import os

    mode = os.stat(path).st_mode & 0o777
    info["mode"] = oct(mode)
    info["ok"] = mode & 0o077 == 0
    info["reason"] = "owner-only" if info["ok"] else "group/other access detected"
    return info


def _sid_str(sid) -> str:  # pragma: no cover - Windows native
    import win32security

    return win32security.ConvertSidToStringSid(sid)
