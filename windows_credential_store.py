"""Small, dependency-free wrapper around Windows Credential Manager.

The credential target is deliberately constant.  The SRDPM username belongs in
the credential's ``UserName`` field and must never be appended to the target.
That gives the application one current-user Generic Credential to manage.

The module is safe to import on non-Windows platforms.  Constructing or using a
store there raises :class:`CredentialStoreUnavailableError` explicitly.
"""

from __future__ import annotations

import ctypes
import os
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any


# This value is intentionally fixed and contains no account-specific data.
CREDENTIAL_TARGET = "SRDPM.WorkHoursAssistant"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168

# Values documented by WinCred.h for current Windows versions.
CRED_MAX_USERNAME_LENGTH = 513
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512

DWORD = ctypes.c_ulong
LPBYTE = ctypes.POINTER(ctypes.c_ubyte)


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", ctypes.c_wchar_p),
        ("Flags", DWORD),
        ("ValueSize", DWORD),
        ("Value", LPBYTE),
    ]


PCREDENTIAL_ATTRIBUTEW = ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", DWORD),
        ("Type", DWORD),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", DWORD),
        ("CredentialBlob", LPBYTE),
        ("Persist", DWORD),
        ("AttributeCount", DWORD),
        ("Attributes", PCREDENTIAL_ATTRIBUTEW),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


class CredentialStoreError(RuntimeError):
    """Base exception whose message never contains credential material."""


class CredentialStoreUnavailableError(CredentialStoreError):
    """Raised when Windows Credential Manager is unavailable."""


class CredentialStoreOperationError(CredentialStoreError):
    """Raised when a Win32 credential operation fails."""

    def __init__(self, operation: str, error_code: int) -> None:
        self.operation = operation
        self.error_code = int(error_code)
        super().__init__(
            f"Windows Credential Manager {operation} failed "
            f"(error code {self.error_code})"
        )


class CredentialStoreDataError(CredentialStoreError):
    """Raised when a stored credential is malformed or unsupported."""

    def __init__(self) -> None:
        super().__init__("Stored SRDPM credential is malformed or unsupported")


@dataclass(frozen=True)
class Credentials:
    """A loaded username/password pair with a deliberately redacted repr."""

    username: str
    password: str

    def __repr__(self) -> str:
        return f"Credentials(username={self.username!r}, password=<redacted>)"

    __str__ = __repr__


def _encoded_utf16(value: str, field: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value:
        raise ValueError(f"{field} must not be empty")
    # Cc covers NUL/newlines, Cf covers invisible format controls, Cs covers
    # isolated surrogate code points, and Zl/Zp cover line separators.
    forbidden_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    if any(unicodedata.category(char) in forbidden_categories for char in value):
        raise ValueError(f"{field} contains a forbidden control character")
    try:
        return value.encode("utf-16-le", errors="strict")
    except UnicodeEncodeError as exc:
        # Do not chain an exception whose object may carry part of a password.
        raise ValueError(f"{field} contains invalid Unicode") from None


def _validate_credentials(username: str, password: str) -> tuple[bytes, bytes]:
    username_bytes = _encoded_utf16(username, "username")
    password_bytes = _encoded_utf16(password, "password")
    if len(username_bytes) // 2 > CRED_MAX_USERNAME_LENGTH:
        raise ValueError(
            f"username exceeds {CRED_MAX_USERNAME_LENGTH} UTF-16 code units"
        )
    if len(password_bytes) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
        raise ValueError(
            f"password exceeds {CRED_MAX_CREDENTIAL_BLOB_SIZE} UTF-16 bytes"
        )
    return username_bytes, password_bytes


class _WindowsAdvapi:
    """Configured ctypes bindings for the four WinCred functions we use."""

    def __init__(self) -> None:
        try:
            library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise CredentialStoreUnavailableError(
                "Windows Credential Manager is unavailable"
            ) from None

        library.CredWriteW.argtypes = [PCREDENTIALW, DWORD]
        library.CredWriteW.restype = ctypes.c_int
        library.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            DWORD,
            DWORD,
            ctypes.POINTER(PCREDENTIALW),
        ]
        library.CredReadW.restype = ctypes.c_int
        library.CredDeleteW.argtypes = [ctypes.c_wchar_p, DWORD, DWORD]
        library.CredDeleteW.restype = ctypes.c_int
        library.CredFree.argtypes = [ctypes.c_void_p]
        library.CredFree.restype = None

        self.CredWriteW = library.CredWriteW
        self.CredReadW = library.CredReadW
        self.CredDeleteW = library.CredDeleteW
        self.CredFree = library.CredFree

    @staticmethod
    def get_last_error() -> int:
        return int(ctypes.get_last_error())


class WindowsCredentialStore:
    """Manage the current user's single SRDPM Generic Credential.

    ``_api`` and ``_os_name`` exist solely to make the Win32 boundary testable
    without touching a real Credential Manager.  Application code should leave
    both at their defaults.
    """

    def __init__(self, *, _api: Any | None = None, _os_name: str | None = None) -> None:
        effective_os_name = os.name if _os_name is None else _os_name
        if effective_os_name != "nt":
            raise CredentialStoreUnavailableError(
                "Windows Credential Manager is only available on Windows"
            )
        self._api = _WindowsAdvapi() if _api is None else _api
        self._lock = threading.RLock()

    def _last_error(self) -> int:
        getter = getattr(self._api, "get_last_error", None)
        return int(getter()) if getter is not None else int(ctypes.get_last_error())

    def _read_pointer(self) -> PCREDENTIALW | None:
        credential_pointer = PCREDENTIALW()
        ok = self._api.CredReadW(
            CREDENTIAL_TARGET,
            CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential_pointer),
        )
        if ok:
            if not credential_pointer:
                raise CredentialStoreDataError()
            return credential_pointer
        error_code = self._last_error()
        if error_code == ERROR_NOT_FOUND:
            return None
        raise CredentialStoreOperationError("read", error_code)

    def has_credentials(self) -> bool:
        """Return whether the fixed Generic Credential currently exists."""

        with self._lock:
            pointer = self._read_pointer()
            if pointer is None:
                return False
            self._api.CredFree(pointer)
            return True

    def load(self) -> Credentials | None:
        """Load credentials, returning ``None`` when none have been saved."""

        with self._lock:
            pointer = self._read_pointer()
            if pointer is None:
                return None
            try:
                credential = pointer.contents
                if credential.Type != CRED_TYPE_GENERIC or not credential.UserName:
                    raise CredentialStoreDataError()
                blob_size = int(credential.CredentialBlobSize)
                if (
                    blob_size <= 0
                    or blob_size > CRED_MAX_CREDENTIAL_BLOB_SIZE
                    or blob_size % 2
                    or not credential.CredentialBlob
                ):
                    raise CredentialStoreDataError()
                password_bytes = ctypes.string_at(
                    credential.CredentialBlob, blob_size
                )
                try:
                    password = password_bytes.decode("utf-16-le", errors="strict")
                except UnicodeDecodeError:
                    raise CredentialStoreDataError() from None
                username = str(credential.UserName)
                try:
                    _validate_credentials(username, password)
                except (TypeError, ValueError):
                    raise CredentialStoreDataError() from None
                return Credentials(username=username, password=password)
            finally:
                self._api.CredFree(pointer)

    def save(self, username: str, password: str) -> None:
        """Create or replace the fixed current-user Generic Credential."""

        _username_bytes, password_bytes = _validate_credentials(username, password)
        # The extra byte supplied by create_string_buffer is not included in the
        # WinCred blob size.  WinCred blobs are binary, not NUL-terminated.
        blob = ctypes.create_string_buffer(password_bytes, len(password_bytes) + 1)
        credential = CREDENTIALW()
        credential.Flags = 0
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = CREDENTIAL_TARGET
        credential.CredentialBlobSize = len(password_bytes)
        credential.CredentialBlob = ctypes.cast(blob, LPBYTE)
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = PCREDENTIAL_ATTRIBUTEW()
        credential.UserName = username

        try:
            with self._lock:
                ok = self._api.CredWriteW(ctypes.byref(credential), 0)
                if not ok:
                    raise CredentialStoreOperationError("write", self._last_error())
        finally:
            # Reduce the lifetime of the extra mutable copy we had to make for
            # the Win32 call.  Python strings themselves cannot be zeroed.
            ctypes.memset(blob, 0, ctypes.sizeof(blob))

    def delete(self) -> bool:
        """Delete the credential; return ``False`` if it did not exist."""

        with self._lock:
            ok = self._api.CredDeleteW(
                CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0
            )
            if ok:
                return True
            error_code = self._last_error()
            if error_code == ERROR_NOT_FOUND:
                return False
            raise CredentialStoreOperationError("delete", error_code)


_default_lock = threading.Lock()
_default_instance: WindowsCredentialStore | None = None


def _default_store() -> WindowsCredentialStore:
    global _default_instance
    if _default_instance is None:
        with _default_lock:
            if _default_instance is None:
                _default_instance = WindowsCredentialStore()
    return _default_instance


def has_credentials() -> bool:
    """Return whether the application credential exists for the current user."""

    return _default_store().has_credentials()


def load() -> Credentials | None:
    """Load the application credential for the current user."""

    return _default_store().load()


def save(username: str, password: str) -> None:
    """Save the application credential for the current user."""

    _default_store().save(username, password)


def delete() -> bool:
    """Delete the application credential for the current user."""

    return _default_store().delete()


__all__ = [
    "CREDENTIAL_TARGET",
    "CredentialStoreDataError",
    "CredentialStoreError",
    "CredentialStoreOperationError",
    "CredentialStoreUnavailableError",
    "Credentials",
    "WindowsCredentialStore",
    "delete",
    "has_credentials",
    "load",
    "save",
]
