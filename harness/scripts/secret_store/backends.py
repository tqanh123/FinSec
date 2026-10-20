"""Where secret VALUES live. Backends, one protocol:

* the OS keychain — Linux secret-service (through `secret-tool`), macOS
  Keychain (through `security`), Windows Credential Manager (ctypes
  advapi32 CredWriteW/CredReadW/CredDeleteW). Each OS door has its own
  value channel: stdin for `secret-tool`, the `-w` pair for `security`
  (a child-process argv the CLI spawns directly — never a shell, never
  our own argv), a credential blob for advapi32;
* the encrypted file — AES-GCM (`cryptography`), key generated once at
  `<home>/vault.key` (0600), ciphertext at `<home>/store.enc`;
* memory — in-process, for tests and `--backend memory`.

`select()` picks the keychain when one answers, else the encrypted file;
`--backend` / `HARNESS_SECRETS_BACKEND` overrides either way. The store's
ledgers are append-only; THIS file is not a ledger — `store.enc` is the
value blob, rewritten whole on each put/delete (the history of what
changed lives in index.jsonl, not here).

The keychain doors are verified here against FAKE CLIs (no box in this
fleet runs a real keychain); the real ones answer on their own OSes' CI
runners — that verification is theirs, and this module says so rather
than pretending a fake tool is a daemon.
"""
from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
import shutil
import subprocess
import sys
from pathlib import Path

ENV_BACKEND = "HARNESS_SECRETS_BACKEND"
#: the OS this process runs on, as a module attr so a test can fake the
#: platform without faking the platform.
PLATFORM = sys.platform
#: the one attribute pair / service name every keychain door files a secret
#: under — the keychain is shared with every other app on the machine, so a
#: bare ref as the lookup key would collide with someone else's item.
KEYCHAIN_ATTR = "hs-secrets-ref"
KEYCHAIN_SERVICE = "hs-secrets"


class BackendError(Exception):
    """A backend refused or failed — the CLI maps this to its broken exit."""


class MemoryBackend:
    """In-process values. For tests and explicit `--backend memory` — nothing
    persists, and a value read after the process ends is correctly absent."""

    def __init__(self) -> None:
        self._v: dict = {}

    def put(self, ref: str, value: str) -> None:
        self._v[ref] = value

    def get(self, ref: str) -> "str | None":
        return self._v.get(ref)

    def delete(self, ref: str) -> None:
        self._v.pop(ref, None)


class EncryptedFileBackend:
    """AES-GCM over one JSON blob, one fresh nonce per value per write, the
    ref itself as the AAD — a ciphertext cannot be replayed under another ref.

    The key is 32 random bytes at `<home>/vault.key`, created 0600 on first
    use together with its one-time recovery echo on stderr: the recovery copy
    is the key itself, shown exactly once, for the operator who must move the
    store to a machine where the key file never existed.
    """

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._home.mkdir(parents=True, exist_ok=True)
        self._key_path = self._home / "vault.key"
        self._store_path = self._home / "store.enc"
        self._aesgcm = None

    def _engine(self):
        if self._aesgcm is None:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError as e:
                raise BackendError(
                    "the encrypted-file backend needs the `cryptography` package "
                    "(declared in harness/requirements.txt) — install the harness "
                    "requirements, or pass --backend memory for a throwaway store"
                ) from e
            self._aesgcm = AESGCM(self._key())
        return self._aesgcm

    def _key(self) -> bytes:
        if self._key_path.is_file():
            return base64.urlsafe_b64decode(self._key_path.read_text(encoding="utf-8").strip())
        key = _secrets.token_bytes(32)
        tmp = self._key_path.with_name(self._key_path.name + ".tmp")
        # the file holds the SAME urlsafe-base64 text the recovery echo prints,
        # so what is shown once is exactly what is on disk
        tmp.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._key_path)
        print("hs-secrets: new vault key written to %s" % self._key_path, file=sys.stderr)
        print("hs-secrets: recovery key (shown ONCE, store it somewhere safe if you "
              "ever need to move the vault): %s"
              % base64.urlsafe_b64encode(key).decode("ascii"), file=sys.stderr)
        return key

    def _load(self) -> dict:
        if not self._store_path.is_file():
            return {}
        raw = self._store_path.read_text(encoding="utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _save(self, data: dict) -> None:
        tmp = self._store_path.with_name(self._store_path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._store_path)

    def put(self, ref: str, value: str) -> None:
        data = self._load()
        nonce = _secrets.token_bytes(12)
        ct = self._engine().encrypt(nonce, value.encode("utf-8"), ref.encode("utf-8"))
        data[ref] = {"n": base64.urlsafe_b64encode(nonce).decode("ascii"),
                     "c": base64.urlsafe_b64encode(ct).decode("ascii")}
        self._save(data)

    def get(self, ref: str) -> "str | None":
        entry = self._load().get(ref)
        if entry is None:
            return None
        nonce = base64.urlsafe_b64decode(entry["n"])
        ct = base64.urlsafe_b64decode(entry["c"])
        try:
            return self._engine().decrypt(nonce, ct, ref.encode("utf-8")).decode("utf-8")
        except Exception as e:  # noqa: BLE001 — every failure mode is one honest refusal
            raise BackendError("cannot decrypt %s under the current vault key" % ref) from e

    def delete(self, ref: str) -> None:
        data = self._load()
        if ref in data:
            del data[ref]
            self._save(data)


def _run(argv, *, input_text=None, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input_text, capture_output=True, text=True,
                          timeout=timeout)


def linux_keychain_available() -> bool:
    """A secret-service that answers. `which` alone is not enough: a machine
    can carry `secret-tool` while no keyring daemon runs (measured on this
    fleet's boxes), and every command would then die mid-sentence. The probe
    is a LOOKUP of a ref that cannot exist — rc 0 (not found, daemon alive)
    or rc 1 (the same, older tools) counts as answering; anything else, or a
    hang past the timeout, counts as no keychain and selection falls through
    to the encrypted file. It never stores anything."""
    if not shutil.which("secret-tool"):
        return False
    try:
        r = _run(["secret-tool", "lookup", KEYCHAIN_ATTR, "hs-availability-probe"],
                 timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode in (0, 1)


def macos_keychain_available() -> bool:
    return bool(shutil.which("security"))


class SecretServiceBackend:
    """Linux secret-service through its `secret-tool` CLI: the value rides
    STDIN (never argv — argv lands in the process table), one attribute pair
    (`hs-secrets-ref` = the ref) files the item."""

    def put(self, ref: str, value: str) -> None:
        r = _run(["secret-tool", "store", "--label", KEYCHAIN_SERVICE,
                  KEYCHAIN_ATTR, ref], input_text=value)
        if r.returncode != 0:
            raise BackendError("secret-tool store failed: %s" % (r.stderr.strip() or r.returncode))

    def get(self, ref: str) -> "str | None":
        r = _run(["secret-tool", "lookup", KEYCHAIN_ATTR, ref])
        if r.returncode != 0:
            raise BackendError("secret-tool lookup failed: %s" % (r.stderr.strip() or r.returncode))
        return r.stdout or None

    def delete(self, ref: str) -> None:
        r = _run(["secret-tool", "clear", KEYCHAIN_ATTR, ref])
        if r.returncode != 0:
            raise BackendError("secret-tool clear failed: %s" % (r.stderr.strip() or r.returncode))


class KeychainBackend:
    """macOS Keychain through its `security` CLI, one generic password per
    ref: account = the ref, service = `hs-secrets`. `security` has no stdin
    door, so the value rides its `-w` pair — an argv of the child process
    this CLI spawns directly, with no shell in between."""

    def _argv(self, verb: str, ref: str) -> list:
        return ["security", verb, "-a", ref, "-s", KEYCHAIN_SERVICE]

    def put(self, ref: str, value: str) -> None:
        r = _run(self._argv("add-generic-password", ref) + ["-w", value, "-U"])
        if r.returncode != 0:
            raise BackendError("security add-generic-password failed: %s"
                               % (r.stderr.strip() or r.returncode))

    def get(self, ref: str) -> "str | None":
        r = _run(self._argv("find-generic-password", ref) + ["-w"])
        if r.returncode != 0:
            # 44 is `security`'s own exit for "the specified item could not be
            # found" (SecKeychainSearchCopyNext) — an absent ref is an answer,
            # not a failure.
            if r.returncode == 44 or "could not be found" in (r.stderr or "").lower():
                return None
            raise BackendError("security find-generic-password failed: %s"
                               % (r.stderr.strip() or r.returncode))
        return r.stdout or None

    def delete(self, ref: str) -> None:
        r = _run(self._argv("delete-generic-password", ref))
        if r.returncode != 0:
            raise BackendError("security delete-generic-password failed: %s"
                               % (r.stderr.strip() or r.returncode))


class CredentialManagerBackend:
    """Windows Credential Manager through ctypes advapi32 — no CLI to shell
    out to on that platform. One generic credential per ref, target
    `<service>:<ref>`, the value as the credential blob (UTF-8 bytes).

    Only importable/constructible on win32; the suite's Windows runner is
    the one place this tier is exercised against the real door."""

    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2

    def _target(self, ref: str) -> str:
        return "%s:%s" % (KEYCHAIN_SERVICE, ref)

    def _advapi32(self):
        import ctypes
        from ctypes import wintypes

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME),
                        ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        return ctypes, wintypes, CREDENTIALW, ctypes.windll.advapi32

    def put(self, ref: str, value: str) -> None:
        ctypes, _wintypes, CREDENTIALW, advapi32 = self._advapi32()
        blob = ctypes.create_string_buffer(value.encode("utf-8"))
        cred = CREDENTIALW(Type=self._TYPE_GENERIC, TargetName=self._target(ref),
                           CredentialBlobSize=len(value.encode("utf-8")),
                           CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte)),
                           Persist=self._PERSIST_LOCAL_MACHINE)
        if not advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise BackendError("CredWriteW failed for %s" % ref)

    def get(self, ref: str) -> "str | None":
        ctypes, _wintypes, _CREDENTIALW, advapi32 = self._advapi32()
        pcred = ctypes.c_void_p()
        if not advapi32.CredReadW(self._target(ref), self._TYPE_GENERIC, 0, ctypes.byref(pcred)):
            return None
        try:
            cred = ctypes.cast(pcred, ctypes.POINTER(_CREDENTIALW)).contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return blob.decode("utf-8")
        finally:
            ctypes.windll.kernel32.CoTaskMemFree(pcred)

    def delete(self, ref: str) -> None:
        ctypes, _wintypes, _CREDENTIALW, advapi32 = self._advapi32()
        if not advapi32.CredDeleteW(self._target(ref), self._TYPE_GENERIC, 0):
            raise BackendError("CredDeleteW failed for %s" % ref)


def _keychain_backend() -> "object | None":
    """The OS keychain tier for THIS machine, or None when it cannot answer —
    the caller falls through to the encrypted file."""
    if PLATFORM == "linux":
        return SecretServiceBackend() if linux_keychain_available() else None
    if PLATFORM == "darwin":
        return KeychainBackend() if macos_keychain_available() else None
    if PLATFORM == "win32":
        return CredentialManagerBackend()
    return None


def select(store_home: Path, choice: "str | None" = None, env=None) -> object:
    """The active backend: `choice` (the --backend flag) wins, then the
    environment, then the declared order — the OS keychain when one answers,
    else the encrypted file. Memory is never picked implicitly: it forgets,
    and an operator who did not ask for a throwaway store must not get one.
    """
    env = os.environ if env is None else env
    name = choice or env.get(ENV_BACKEND)
    if name == "memory":
        return MemoryBackend()
    if name == "file":
        return EncryptedFileBackend(store_home)
    if name:
        raise BackendError("unknown backend %r (known: memory, file)" % name)
    return _keychain_backend() or EncryptedFileBackend(store_home)
