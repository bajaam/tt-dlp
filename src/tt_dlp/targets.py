"""Strict parsing for TikTok usernames, URLs, and stable identifiers."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .errors import TikTokError
from .models import Target, TargetKind


TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
USERNAME_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,62}[A-Za-z0-9_])?")
USER_ID_RE = re.compile(r"\d{5,30}")
SEC_UID_RE = re.compile(r"[A-Za-z0-9_-]{20,200}")
POST_ID_RE = re.compile(r"\d{5,30}")
MEDIA_KINDS = {"video", "photo", "story"}


def validate_username(value: str) -> str:
    if (
        not USERNAME_RE.fullmatch(value)
        or value in {".", ".."}
        or ".." in value
    ):
        raise TikTokError(f"Invalid TikTok username: {value!r}")
    return value


def validate_user_id(value: str) -> str:
    if not USER_ID_RE.fullmatch(value):
        raise TikTokError("A TikTok user ID must contain only 5-30 digits")
    return value


def validate_sec_uid(value: str) -> str:
    if not SEC_UID_RE.fullmatch(value):
        raise TikTokError("Invalid TikTok secUid")
    return value


def validate_post_id(value: str) -> str:
    if not POST_ID_RE.fullmatch(value):
        raise TikTokError("Invalid TikTok post ID")
    return value


def _identifier_target(value: str, raw: str) -> Target:
    lowered = value.lower()
    if lowered.startswith("ttid:"):
        pieces = value.split(":", 2)
        if len(pieces) != 3:
            raise TikTokError("Expected ttid:<userId>:<secUid>")
        return Target(
            kind=TargetKind.STABLE_ID,
            raw=raw,
            user_id=validate_user_id(pieces[1]),
            sec_uid=validate_sec_uid(pieces[2]),
        )
    for prefix in ("userid:", "uid:"):
        if lowered.startswith(prefix):
            return Target(
                kind=TargetKind.USER_ID,
                raw=raw,
                user_id=validate_user_id(value[len(prefix):]),
            )
    if lowered.startswith("secuid:"):
        return Target(
            kind=TargetKind.SEC_UID,
            raw=raw,
            sec_uid=validate_sec_uid(value[len("secuid:"):]),
        )
    username = value[1:] if value.startswith("@") else value
    return Target(
        kind=TargetKind.USERNAME,
        raw=raw,
        username=validate_username(username),
    )


def parse_target(value: str) -> Target:
    raw = str(value)
    value = raw.strip()
    if not value:
        raise TikTokError("Queue targets cannot be empty")

    looks_like_url = value.lower().startswith(("http://", "https://"))
    if not looks_like_url:
        return _identifier_target(value, raw)

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise TikTokError("Invalid TikTok URL")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in SHORT_HOSTS:
        if not parsed.path or parsed.path == "/":
            raise TikTokError("Invalid TikTok short URL")
        return Target(kind=TargetKind.SHORT_URL, raw=raw, short_url=value)
    if host not in TIKTOK_HOSTS:
        raise TikTokError(f"Unsupported TikTok host: {host or '(missing)'}")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts or not parts[0].startswith("@"):
        raise TikTokError("Expected a TikTok profile, video, photo, or story URL")
    identifier = parts[0][1:]
    if USERNAME_RE.fullmatch(identifier):
        target = Target(
            kind=TargetKind.USERNAME,
            raw=raw,
            username=validate_username(identifier),
        )
    elif SEC_UID_RE.fullmatch(identifier):
        target = Target(
            kind=TargetKind.SEC_UID,
            raw=raw,
            sec_uid=validate_sec_uid(identifier),
        )
    else:
        raise TikTokError("Invalid TikTok profile identifier")
    if len(parts) == 1:
        return target
    if len(parts) != 3 or parts[1].lower() not in MEDIA_KINDS:
        raise TikTokError("Invalid TikTok profile or media URL")
    return Target(
        kind=target.kind,
        raw=raw,
        username=target.username,
        user_id=target.user_id,
        sec_uid=target.sec_uid,
        post_id=validate_post_id(parts[2]),
        media_kind=parts[1].lower(),
    )
