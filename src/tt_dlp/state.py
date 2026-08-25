"""Persistent stable-profile identities and rename history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .errors import TikTokError
from .models import ProfileIdentity, Target, TargetKind
from .targets import validate_sec_uid, validate_user_id, validate_username


STATE_VERSION = 1
INVALID_COMPONENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_directory_name(value: str) -> str:
    """Return a single safe path component for a profile archive."""
    value = INVALID_COMPONENT_RE.sub("_", str(value)).strip(" .")
    if not value or value in {".", ".."}:
        raise TikTokError("TikTok returned an unsafe profile directory name")
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value += "_"
    return value[:100].rstrip(" .")


class ProfileStore:
    """Small JSON registry keyed primarily by TikTok's stable secUid."""

    def __init__(self, path: Path | None):
        self.path = path
        self._profiles: list[ProfileIdentity] = []
        if path and path.is_file():
            self._profiles = self._load(path)

    @staticmethod
    def _load(path: Path) -> list[ProfileIdentity]:
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError) as exc:
            raise TikTokError(f"Could not read profile store {path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise TikTokError(f"Unsupported profile store format: {path}")
        entries = data.get("profiles", [])
        if not isinstance(entries, list):
            raise TikTokError(f"Invalid profile store: {path}")

        profiles = []
        directories: set[str] = set()
        usernames: set[str] = set()
        user_ids: set[str] = set()
        sec_uids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise TikTokError(f"Invalid profile entry in {path}")
            try:
                username = validate_username(str(entry["username"]))
                user_id = str(entry.get("user_id") or "")
                sec_uid = str(entry.get("sec_uid") or "")
                if user_id:
                    validate_user_id(user_id)
                if sec_uid:
                    validate_sec_uid(sec_uid)
                if not (user_id or sec_uid):
                    raise ValueError("a stable user ID is required")
                aliases_value = entry.get("aliases", [])
                if not isinstance(aliases_value, list):
                    raise ValueError("aliases must be a list")
                aliases = tuple(
                    validate_username(str(alias)) for alias in aliases_value
                )
                directory = safe_directory_name(
                    str(entry.get("directory") or username)
                )
            except (KeyError, TypeError, ValueError, TikTokError) as exc:
                raise TikTokError(
                    f"Invalid profile entry in {path}: {exc}"
                ) from exc
            directory_key = directory.casefold()
            if directory_key in directories:
                raise TikTokError(
                    f"Duplicate profile directory {directory!r} in {path}"
                )
            username_key = username.casefold()
            if username_key in usernames:
                raise TikTokError(
                    f"Duplicate current username @{username} in {path}"
                )
            if user_id and user_id in user_ids:
                raise TikTokError(
                    f"Duplicate numeric user ID {user_id} in {path}"
                )
            if sec_uid and sec_uid in sec_uids:
                raise TikTokError(f"Duplicate secUid in {path}")
            directories.add(directory_key)
            usernames.add(username_key)
            if user_id:
                user_ids.add(user_id)
            if sec_uid:
                sec_uids.add(sec_uid)
            profiles.append(ProfileIdentity(
                username=username,
                user_id=user_id,
                sec_uid=sec_uid,
                directory=directory,
                aliases=aliases,
            ))
        return profiles

    def find(self, target: Target) -> ProfileIdentity | None:
        if target.sec_uid or target.user_id:
            for profile in self._profiles:
                if (
                    (not target.sec_uid or profile.sec_uid == target.sec_uid)
                    and (
                        not target.user_id
                        or profile.user_id == target.user_id
                    )
                ):
                    return profile
            return None
        for profile in self._profiles:
            if target.kind is TargetKind.USERNAME:
                if profile.username.lower() == target.username.lower():
                    return profile
        if target.kind is TargetKind.USERNAME:
            matches = [
                profile for profile in self._profiles
                if any(
                    alias.lower() == target.username.lower()
                    for alias in profile.aliases
                )
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise TikTokError(
                    f"Saved username alias @{target.username} is ambiguous; "
                    "use a ttid: or secuid: target"
                )
        return None

    def update(
        self,
        identity: ProfileIdentity,
        *,
        requested_alias: str | None = None,
    ) -> ProfileIdentity:
        username = validate_username(identity.username)
        user_id = validate_user_id(identity.user_id) if identity.user_id else ""
        sec_uid = validate_sec_uid(identity.sec_uid) if identity.sec_uid else ""
        if not (user_id or sec_uid):
            raise TikTokError(
                "Cannot save a profile without a stable TikTok user ID"
            )
        sec_index = next((
            index for index, profile in enumerate(self._profiles)
            if sec_uid and profile.sec_uid == sec_uid
        ), None)
        user_index = next((
            index for index, profile in enumerate(self._profiles)
            if user_id and profile.user_id == user_id
        ), None)
        if (
            sec_index is not None
            and user_index is not None
            and sec_index != user_index
        ):
            raise TikTokError(
                "The supplied user ID and secUid map to different saved "
                "profiles"
            )
        existing_index = sec_index if sec_index is not None else user_index
        existing = (
            self._profiles[existing_index]
            if existing_index is not None
            else None
        )
        if existing:
            if user_id and existing.user_id and user_id != existing.user_id:
                raise TikTokError(
                    "Conflicting numeric user ID for saved TikTok secUid"
                )
            if sec_uid and existing.sec_uid and sec_uid != existing.sec_uid:
                raise TikTokError(
                    "Conflicting secUid for saved TikTok numeric user ID"
                )

        aliases = set(identity.aliases)
        directory = identity.directory or safe_directory_name(identity.username)
        if existing:
            user_id = user_id or existing.user_id
            sec_uid = sec_uid or existing.sec_uid
            aliases.update(existing.aliases)
            if existing.username.lower() != identity.username.lower():
                aliases.add(existing.username)
            directory = existing.directory or directory
        for index, profile in enumerate(self._profiles):
            if index == existing_index:
                continue
            if profile.username.lower() == username.lower():
                raise TikTokError(
                    f"Username @{username} is already mapped to another "
                    "saved TikTok identity"
                )

        directory = self._unique_directory(
            directory,
            user_id=user_id,
            sec_uid=sec_uid,
            existing_index=existing_index,
        )
        if requested_alias and requested_alias.lower() != identity.username.lower():
            aliases.add(validate_username(requested_alias))
        aliases = {
            alias for alias in aliases
            if alias.lower() != username.lower()
        }

        updated = ProfileIdentity(
            username=username,
            user_id=user_id,
            sec_uid=sec_uid,
            directory=safe_directory_name(directory),
            aliases=tuple(sorted(aliases, key=str.lower)),
        )
        if existing_index is None:
            self._profiles.append(updated)
        else:
            self._profiles[existing_index] = updated
        self._save()
        return updated

    def _unique_directory(
        self,
        preferred: str,
        *,
        user_id: str,
        sec_uid: str,
        existing_index: int | None,
    ) -> str:
        preferred = safe_directory_name(preferred)
        used = {
            profile.directory.casefold()
            for index, profile in enumerate(self._profiles)
            if index != existing_index
        }
        if preferred.casefold() not in used:
            return preferred

        suffix = user_id[-8:] if user_id else hashlib.sha256(
            sec_uid.encode("utf-8")
        ).hexdigest()[:8]
        base = preferred[:90].rstrip(" ._") or "profile"
        candidate = safe_directory_name(f"{base}_{suffix}")
        number = 2
        while candidate.casefold() in used:
            candidate = safe_directory_name(
                f"{base[:87]}_{suffix}_{number}"
            )
            number += 1
        return candidate

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": STATE_VERSION,
            "profiles": [
                {
                    "username": profile.username,
                    "user_id": profile.user_id,
                    "sec_uid": profile.sec_uid,
                    "directory": profile.directory,
                    "aliases": list(profile.aliases),
                }
                for profile in sorted(
                    self._profiles, key=lambda item: item.username.lower()
                )
            ],
        }
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                json.dump(data, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            raise TikTokError(
                f"Could not update profile store {self.path}: {exc}"
            ) from exc
