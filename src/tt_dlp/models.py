"""Typed values shared across tt-dlp's configuration and download layers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class TargetKind(str, Enum):
    USERNAME = "username"
    USER_ID = "userid"
    SEC_UID = "secuid"
    STABLE_ID = "ttid"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class Target:
    """A validated queue target before TikTok identity resolution."""

    kind: TargetKind
    raw: str
    username: str = ""
    user_id: str = ""
    sec_uid: str = ""
    short_url: str = ""
    post_id: str | None = None
    media_kind: str | None = None

    @property
    def display(self) -> str:
        if self.kind is TargetKind.USERNAME:
            base = f"@{self.username}"
        elif self.kind is TargetKind.USER_ID:
            base = f"userid:{self.user_id}"
        elif self.kind is TargetKind.SEC_UID:
            base = f"secuid:{self.sec_uid}"
        elif self.kind is TargetKind.STABLE_ID:
            base = f"ttid:{self.user_id}:{self.sec_uid}"
        else:
            return self.short_url
        return f"{base}/{self.media_kind}/{self.post_id}" if self.post_id else base


@dataclass(frozen=True, slots=True)
class ProfileIdentity:
    """Canonical TikTok profile identity.

    ``sec_uid`` and ``user_id`` are stable; ``username`` may change.
    ``directory`` remains stable so a rename does not split an archive.
    """

    username: str
    user_id: str = ""
    sec_uid: str = ""
    directory: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def stable_target(self) -> str:
        if self.user_id and self.sec_uid:
            return f"ttid:{self.user_id}:{self.sec_uid}"
        if self.sec_uid:
            return f"secuid:{self.sec_uid}"
        if self.user_id:
            return f"userid:{self.user_id}"
        return f"@{self.username}"

    def with_directory(self, directory: str) -> ProfileIdentity:
        return ProfileIdentity(
            username=self.username,
            user_id=self.user_id,
            sec_uid=self.sec_uid,
            directory=directory,
            aliases=self.aliases,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    output: Path
    cookies: Path | None
    profile_store: Path | None
    limit: int
    sleep: float
    overwrite: bool
    dry_run: bool
    stories: bool
    identify: bool


class DownloadOutcome(str, Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    PLANNED = "planned"
    FAILED = "failed"


@dataclass(slots=True)
class DownloadSummary:
    downloaded: int = 0
    skipped: int = 0
    planned: int = 0
    failed: int = 0

    def record(self, outcome: DownloadOutcome) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)

    def add(self, other: DownloadSummary) -> None:
        self.downloaded += other.downloaded
        self.skipped += other.skipped
        self.planned += other.planned
        self.failed += other.failed


@dataclass(frozen=True, slots=True)
class MediaItem:
    """Normalized video or photo post returned by TikTok."""

    post_id: str
    description: str
    video_urls: tuple[str, ...] = ()
    image_urls: tuple[tuple[str, ...], ...] = ()
    image_count: int = 0
    author: ProfileIdentity | None = None
    is_story: bool = False

    @property
    def is_photo(self) -> bool:
        return bool(self.image_urls) or self.image_count > 0
