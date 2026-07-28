"""Encrypted BYOK credential store (M4, §9).

Maps a credential *handle* (the `api_key_ref` a package uses, e.g. 'azure-main') to the
real provider credential (key + endpoint), encrypted at rest with Fernet. This is the only
place raw provider keys live; they flow from here into the LiteLLM proxy config at
provision time and never into a project package or an agent container.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class Credential:
    """A resolved provider credential. `api_base` is required for endpoint-specific providers
    like Azure (the resource URL); optional for plain OpenAI/Anthropic."""

    api_key: str
    api_base: str | None = None
    provider: str | None = None


class KeyStoreError(Exception):
    """Missing handle, wrong master key, or corrupt store."""


class KeyStore:
    """Handle -> encrypted Credential. File-backed if a path is given, else in-memory."""

    def __init__(self, fernet_key: bytes, path: str | Path | None = None) -> None:
        self._f = Fernet(fernet_key)
        self._path = Path(path) if path else None
        self._data: dict[str, str] = {}
        if self._path and self._path.exists():
            self._data = json.loads(self._path.read_text())

    @staticmethod
    def generate_key() -> bytes:
        """A fresh Fernet master key (store it in the runner's env, never in a package)."""
        return Fernet.generate_key()

    def put(self, handle: str, api_key: str, api_base: str | None = None,
            provider: str | None = None) -> None:
        cred = Credential(api_key=api_key, api_base=api_base, provider=provider)
        self._data[handle] = self._f.encrypt(json.dumps(asdict(cred)).encode()).decode()
        self._flush()

    def get(self, handle: str) -> Credential:
        if handle not in self._data:
            raise KeyStoreError(f"no credential for handle {handle!r}")
        try:
            raw = self._f.decrypt(self._data[handle].encode())
        except InvalidToken as exc:
            raise KeyStoreError("cannot decrypt store (wrong master key or corrupt)") from exc
        return Credential(**json.loads(raw.decode()))

    def handles(self) -> list[str]:
        return sorted(self._data)

    def _flush(self) -> None:
        """Write the store 0600, atomically.

        `write_text` creates it 0644 — world-readable — and truncates in place, so a crash mid-write
        leaves a store that cannot be decrypted. It holds every BYOK provider credential, encrypted,
        but a mode is free and an encrypted file is not an invitation."""
        if not self._path:
            return
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
