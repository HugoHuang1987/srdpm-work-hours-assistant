from __future__ import annotations

import ctypes
import unittest

import windows_credential_store as credential_store
from windows_credential_store import (
    CREDENTIAL_TARGET,
    CRED_MAX_CREDENTIAL_BLOB_SIZE,
    CRED_MAX_USERNAME_LENGTH,
    CRED_PERSIST_LOCAL_MACHINE,
    CRED_TYPE_GENERIC,
    ERROR_NOT_FOUND,
    CREDENTIALW,
    PCREDENTIALW,
    CredentialStoreDataError,
    CredentialStoreOperationError,
    CredentialStoreUnavailableError,
    Credentials,
    WindowsCredentialStore,
)


class FakeAdvapi:
    """In-memory ctypes-shaped fake; it never calls Windows or writes a secret."""

    def __init__(self) -> None:
        self.record: tuple[str, str, bytes, int, int] | None = None
        self.last_error = 0
        self.write_error = 0
        self.read_error = 0
        self.delete_error = 0
        self.write_calls = 0
        self.read_calls = 0
        self.delete_calls = 0
        self.free_calls = 0
        self._read_references: list[object] = []

    def get_last_error(self) -> int:
        return self.last_error

    def CredWriteW(self, credential_arg: object, flags: int) -> int:
        self.write_calls += 1
        if self.write_error:
            self.last_error = self.write_error
            return 0
        credential = ctypes.cast(credential_arg, PCREDENTIALW).contents
        blob = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        self.record = (
            str(credential.TargetName),
            str(credential.UserName),
            blob,
            int(credential.Type),
            int(credential.Persist),
        )
        self.last_error = 0
        return 1

    def CredReadW(
        self, target: str, credential_type: int, flags: int, output_arg: object
    ) -> int:
        self.read_calls += 1
        if self.read_error:
            self.last_error = self.read_error
            return 0
        if self.record is None:
            self.last_error = ERROR_NOT_FOUND
            return 0
        saved_target, username, blob, saved_type, persist = self.record
        if target != saved_target or int(credential_type) != saved_type:
            self.last_error = ERROR_NOT_FOUND
            return 0

        username_buffer = ctypes.create_unicode_buffer(username)
        blob_buffer = ctypes.create_string_buffer(blob, len(blob) + 1)
        credential = CREDENTIALW()
        credential.Type = saved_type
        credential.TargetName = saved_target
        credential.UserName = ctypes.cast(username_buffer, ctypes.c_wchar_p)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            blob_buffer, ctypes.POINTER(ctypes.c_ubyte)
        )
        credential.Persist = persist
        pointer = ctypes.pointer(credential)
        ctypes.cast(output_arg, ctypes.POINTER(PCREDENTIALW))[0] = pointer
        self._read_references = [username_buffer, blob_buffer, credential, pointer]
        self.last_error = 0
        return 1

    def CredDeleteW(self, target: str, credential_type: int, flags: int) -> int:
        self.delete_calls += 1
        if self.delete_error:
            self.last_error = self.delete_error
            return 0
        if self.record is None:
            self.last_error = ERROR_NOT_FOUND
            return 0
        if target != self.record[0] or int(credential_type) != self.record[3]:
            self.last_error = ERROR_NOT_FOUND
            return 0
        self.record = None
        self.last_error = 0
        return 1

    def CredFree(self, pointer: object) -> None:
        self.free_calls += 1
        self._read_references = []


class WindowsCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeAdvapi()
        self.store = WindowsCredentialStore(_api=self.api, _os_name="nt")

    def test_round_trip_uses_one_fixed_generic_target(self) -> None:
        username = "employee@example.test"
        password = "S3cur3-瀵嗙爜-🔒"

        self.assertFalse(self.store.has_credentials())
        self.store.save(username, password)
        self.assertTrue(self.store.has_credentials())
        loaded = self.store.load()

        self.assertEqual(loaded, Credentials(username, password))
        self.assertIsNotNone(self.api.record)
        target, stored_username, blob, credential_type, persist = self.api.record
        self.assertEqual(target, CREDENTIAL_TARGET)
        self.assertNotIn(username, target)
        self.assertEqual(stored_username, username)
        self.assertEqual(blob.decode("utf-16-le"), password)
        self.assertEqual(credential_type, CRED_TYPE_GENERIC)
        self.assertEqual(persist, CRED_PERSIST_LOCAL_MACHINE)
        self.assertEqual(self.api.free_calls, 2)

        self.assertTrue(self.store.delete())
        self.assertFalse(self.store.delete())
        self.assertIsNone(self.store.load())

    def test_loaded_credentials_repr_and_str_redact_password(self) -> None:
        password = "never-print-this-password"
        credentials = Credentials("safe-user", password)
        for rendered in (repr(credentials), str(credentials)):
            self.assertNotIn(password, rendered)
            self.assertIn("<redacted>", rendered)

    def test_validation_rejects_bad_values_before_calling_advapi(self) -> None:
        invalid_pairs: list[tuple[object, object]] = [
            ("", "password"),
            ("user", ""),
            (123, "password"),
            ("user", 123),
            ("bad\nuser", "password"),
            ("user", "bad\x00password"),
            ("user\u200b", "password"),
            ("user", "bad\u2028password"),
            ("u" * (CRED_MAX_USERNAME_LENGTH + 1), "password"),
            ("user", "p" * (CRED_MAX_CREDENTIAL_BLOB_SIZE // 2 + 1)),
        ]
        for username, password in invalid_pairs:
            with self.subTest(username_type=type(username), password_type=type(password)):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.save(username, password)  # type: ignore[arg-type]
        self.assertEqual(self.api.write_calls, 0)

    def test_documented_length_boundaries_are_accepted(self) -> None:
        username = "u" * CRED_MAX_USERNAME_LENGTH
        password = "p" * (CRED_MAX_CREDENTIAL_BLOB_SIZE // 2)
        self.store.save(username, password)
        self.assertEqual(self.store.load(), Credentials(username, password))

    def test_operation_errors_contain_only_operation_and_numeric_code(self) -> None:
        password = "do-not-leak-this-secret"
        self.api.write_error = 5
        with self.assertRaises(CredentialStoreOperationError) as captured:
            self.store.save("employee", password)
        rendered = repr(captured.exception) + str(captured.exception)
        self.assertNotIn(password, rendered)
        self.assertIn("error code 5", rendered)
        self.assertEqual(captured.exception.operation, "write")
        self.assertEqual(captured.exception.error_code, 5)

    def test_read_and_delete_errors_are_explicit(self) -> None:
        self.api.read_error = 87
        with self.assertRaises(CredentialStoreOperationError) as read_error:
            self.store.load()
        self.assertEqual(read_error.exception.error_code, 87)

        self.api.read_error = 0
        self.api.delete_error = 1312
        with self.assertRaises(CredentialStoreOperationError) as delete_error:
            self.store.delete()
        self.assertEqual(delete_error.exception.error_code, 1312)

    def test_malformed_blob_is_rejected_and_freed(self) -> None:
        self.api.record = (
            CREDENTIAL_TARGET,
            "employee",
            b"odd",
            CRED_TYPE_GENERIC,
            CRED_PERSIST_LOCAL_MACHINE,
        )
        with self.assertRaises(CredentialStoreDataError):
            self.store.load()
        self.assertEqual(self.api.free_calls, 1)

    def test_non_windows_is_explicitly_unavailable_even_with_fake_api(self) -> None:
        with self.assertRaises(CredentialStoreUnavailableError) as captured:
            WindowsCredentialStore(_api=self.api, _os_name="posix")
        self.assertIn("only available on Windows", str(captured.exception))

    def test_module_level_api_delegates_to_lazy_store(self) -> None:
        original = credential_store._default_instance
        credential_store._default_instance = self.store
        try:
            self.assertFalse(credential_store.has_credentials())
            credential_store.save("employee", "password")
            self.assertEqual(
                credential_store.load(), Credentials("employee", "password")
            )
            self.assertTrue(credential_store.delete())
        finally:
            credential_store._default_instance = original


if __name__ == "__main__":
    unittest.main()
