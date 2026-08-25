"""Profile discovery, stable identity handling, and atomic media downloads."""

from __future__ import annotations

import html
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .client import ROOT, TikTokClient
from .errors import TikTokError
from .models import (
    DownloadOutcome,
    DownloadSummary,
    MediaItem,
    ProfileIdentity,
    Settings,
    Target,
    TargetKind,
)
from .state import ProfileStore, safe_directory_name
from .targets import parse_target


INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EXTENSION_RE = re.compile(r"[a-z0-9]{1,5}")


class TikTokDownloader:
    def __init__(
        self,
        settings: Settings,
        *,
        client: TikTokClient | None = None,
        profile_store: ProfileStore | None = None,
    ):
        self.settings = settings
        self.client = client or TikTokClient(settings)
        self.profile_store = profile_store or ProfileStore(
            settings.profile_store
        )
        self.last_download_at: float | None = None

    def run(self, raw_target: str) -> int:
        target = parse_target(raw_target)
        if target.kind is TargetKind.SHORT_URL:
            target = parse_target(
                self.client.resolve_short_url(target.short_url)
            )

        cached = self.profile_store.find(target)
        requested_username = target.username
        identity = self._initial_identity(target, cached)
        creator: dict = {}
        recent: list[dict] = []
        is_private = False
        direct_item: MediaItem | None = None

        # A direct media embed is authoritative.  It prevents a stale or
        # deliberately mismatched @username in a URL from mixing two users'
        # posts and stories.
        if target.post_id:
            direct_item = self.client.media_from_embed(
                target.post_id,
                is_story=target.media_kind == "story",
            )
            if direct_item.author is None:
                raise TikTokError(
                    f"TikTok did not identify the creator of {target.post_id}"
                )
            self._validate_target_identity(target, direct_item.author)
            identity = self._merge_identity(
                identity,
                direct_item.author,
                authoritative=True,
            )

        lookup_username = identity.username or target.username
        if lookup_username:
            try:
                creator = self.client.creator_data(lookup_username)
            except TikTokError:
                if cached is None and direct_item is None:
                    if not self.client.has_cookies:
                        raise
                creator = {}

            user = creator.get("userInfo") if creator else None
            if creator and user is None and cached is None and direct_item is None:
                raise TikTokError(
                    "TikTok creator embed returned no user metadata"
                )
            if creator and user is not None and not isinstance(user, dict):
                raise TikTokError(
                    "TikTok creator embed returned malformed user metadata"
                )
            if isinstance(user, dict):
                code = user.get("code")
                if code not in (None, 0, 200):
                    raise TikTokError(
                        str(user.get("message") or "TikTok rejected profile")
                    )
                creator_identity = self.client.identity_from_creator(creator)
                if creator_identity:
                    if cached and self._stable_conflict(
                        cached, creator_identity
                    ):
                        print(
                            f"Saved identity for @{lookup_username} no longer "
                            "matches that username; following the saved "
                            "TikTok identity."
                        )
                        creator = {}
                    elif direct_item and self._stable_conflict(
                        identity, creator_identity
                    ):
                        creator = {}
                    else:
                        identity = self._merge_identity(
                            identity, creator_identity
                        )
                if creator:
                    is_private = bool(user.get("privateAccount"))
                    creator_recent = creator.get("videoList") or ()
                    if isinstance(creator_recent, list):
                        recent = creator_recent
                    elif creator_recent:
                        raise TikTokError(
                            "TikTok creator embed returned malformed post "
                            "metadata"
                        )

        if is_private and not self.client.has_cookies:
            raise TikTokError(
                f"@{identity.username or requested_username} is private. "
                "Provide a current Netscape cookies.txt file for an account "
                "that can view it."
            )

        # Creator embeds usually expose the numeric ID but not secUid.  A
        # recent post embed supplies both stable identifiers.
        if not identity.sec_uid and direct_item is None and recent:
            seed_id = str(recent[0].get("id") or "")
            if seed_id:
                try:
                    seed_item = self.client.media_from_embed(seed_id)
                except TikTokError:
                    seed_item = None
                if seed_item and seed_item.author:
                    identity = self._merge_identity(
                        identity, seed_item.author, authoritative=True
                    )

        if (
            lookup_username
            and self.client.has_cookies
            and (not identity.sec_uid or not identity.user_id)
        ):
            try:
                authenticated = self.client.authenticated_profile_identity(
                    lookup_username
                )
            except TikTokError:
                authenticated = None
            if authenticated:
                identity = self._merge_identity(
                    identity, authenticated, authoritative=True
                )
            elif is_private and not identity.sec_uid:
                raise TikTokError(
                    f"The supplied cookies cannot access @{lookup_username}. "
                    "Refresh them and make sure the signed-in account can "
                    "view this private profile."
                )

        profile_url = self._profile_url(identity, target)

        # Numeric IDs cannot enumerate profile posts by themselves.  An active
        # story can disclose the paired secUid; otherwise the local registry or
        # a ttid/secuid target is required.
        if not identity.sec_uid and identity.user_id and self.client.has_cookies:
            story_identity = self.client.active_story_identity(
                identity.user_id, profile_url=profile_url
            )
            if story_identity:
                identity = self._merge_identity(
                    identity, story_identity, authoritative=True
                )
                profile_url = self._profile_url(identity, target)

        if target.kind is TargetKind.USER_ID and not identity.sec_uid:
            raise TikTokError(
                "A numeric user ID alone cannot list TikTok posts. Use "
                "--identify @username once, then queue the printed "
                "ttid:<userId>:<secUid> value. A saved userid: target also "
                "works after that identity has been recorded."
            )

        posts: list[MediaItem] = []
        if identity.sec_uid:
            posts = self.client.collect_posts(
                identity.sec_uid,
                profile_url=profile_url,
                recent=recent,
                is_private=is_private,
            )
            found_author = False
            for item in posts:
                if item.author:
                    found_author = True
                    identity = self._merge_identity(
                        identity, item.author, authoritative=True
                    )
            if posts and not found_author:
                first_item = self.client.media_from_embed(posts[0].post_id)
                if first_item.author:
                    identity = self._merge_identity(
                        identity, first_item.author, authoritative=True
                    )
            profile_url = self._profile_url(identity, target)
        print(
            f"Profile scan complete: {len(posts)} "
            f"{'post' if len(posts) == 1 else 'posts'} found."
        )

        want_stories = (
            self.settings.stories or target.media_kind == "story"
        )
        stories: list[MediaItem] = []
        if want_stories:
            if not self.client.has_cookies:
                raise TikTokError(
                    "TikTok story access requires a current Netscape "
                    "cookies.txt file. Pass --cookies or set cookies_file "
                    "in config.json."
                )
            if not identity.user_id:
                raise TikTokError(
                    "Could not resolve the profile's numeric user ID for "
                    "story access"
                )
            stories = self.client.collect_stories(
                identity, profile_url=profile_url
            )
            story_author = next(
                (item.author for item in stories if item.author), None
            )
            if story_author:
                identity = self._merge_identity(
                    identity, story_author, authoritative=True
                )
                profile_url = self._profile_url(identity, target)
            print(
                f"Story scan complete: {len(stories)} active "
                f"{'story' if len(stories) == 1 else 'stories'} found."
            )

        if direct_item and direct_item.author:
            identity = self._merge_identity(
                identity, direct_item.author, authoritative=True
            )
            profile_url = self._profile_url(identity, target)
        if not identity.username:
            if (
                not posts
                and not stories
                and target.kind in {TargetKind.STABLE_ID, TargetKind.SEC_UID}
            ):
                stable_target = ProfileIdentity(
                    username="",
                    user_id=identity.user_id,
                    sec_uid=identity.sec_uid,
                ).stable_target
                if self.settings.identify and not (
                    identity.user_id and identity.sec_uid
                ):
                    raise TikTokError(
                        "TikTok exposes no accessible media from which to "
                        "complete this secuid target with a numeric user ID"
                    )
                print("TikTok user: not currently exposed")
                print(f"Stable target: {stable_target}")
                print(
                    "No accessible media; the profile store was left "
                    "unchanged."
                )
                return 0
            raise TikTokError(
                "TikTok did not expose the current username for this stable "
                "identifier. Queue ttid:<userId>:<secUid> again when the "
                "account has an accessible post or active story."
            )

        alias = None
        if requested_username:
            if direct_item is None or cached is not None or (
                requested_username.lower() == identity.username.lower()
            ):
                alias = requested_username
        previous_username = cached.username if cached else ""
        identity = self.profile_store.update(
            identity,
            requested_alias=alias,
        )
        if (
            previous_username
            and previous_username.lower() != identity.username.lower()
        ):
            print(
                f"Username changed: @{previous_username} -> "
                f"@{identity.username}; keeping folder "
                f"{identity.directory!r}."
            )

        output = self._profile_output(identity)
        print(f"TikTok user: @{identity.username}")
        print(f"Stable target: {identity.stable_target}")
        print(f"Saving to: {output}")

        if self.settings.identify:
            if not (identity.user_id and identity.sec_uid):
                raise TikTokError(
                    "TikTok exposes no accessible post or active story from "
                    "which to obtain this profile's secUid, so a complete "
                    "rename-safe ttid target cannot be generated yet."
                )
            return 0
        if not self.settings.dry_run:
            output.mkdir(parents=True, exist_ok=True)

        if target.post_id:
            if direct_item is None:
                raise TikTokError(f"Media {target.post_id} is unavailable")
            if target.media_kind == "story":
                posts = []
                stories = [direct_item]
            else:
                posts = [direct_item]
                stories = []
        elif self.settings.limit:
            posts = posts[:self.settings.limit]

        print(
            f"Starting download queue: {len(posts)} posts, "
            f"{len(stories)} stories."
        )
        summary = DownloadSummary()
        for item in posts:
            summary.add(self._download_item(
                item, output=output, referer=profile_url
            ))
        for item in stories:
            summary.add(self._download_item(
                item, output=output, referer=profile_url
            ))

        print()
        print(
            f"Finished: {summary.downloaded} downloaded, "
            f"{summary.skipped} skipped, {summary.planned} planned, "
            f"{summary.failed} failed"
        )
        return 1 if summary.failed else 0

    @staticmethod
    def _initial_identity(
        target: Target,
        cached: ProfileIdentity | None,
    ) -> ProfileIdentity:
        if cached:
            return cached
        return ProfileIdentity(
            username=target.username,
            user_id=target.user_id,
            sec_uid=target.sec_uid,
        )

    @staticmethod
    def _stable_conflict(
        first: ProfileIdentity,
        second: ProfileIdentity,
    ) -> bool:
        return bool(
            first.sec_uid
            and second.sec_uid
            and first.sec_uid != second.sec_uid
        ) or bool(
            first.user_id
            and second.user_id
            and first.user_id != second.user_id
        )

    @classmethod
    def _merge_identity(
        cls,
        current: ProfileIdentity,
        discovered: ProfileIdentity,
        *,
        authoritative: bool = False,
    ) -> ProfileIdentity:
        if cls._stable_conflict(current, discovered):
            raise TikTokError(
                "TikTok returned conflicting stable identities for this "
                "target; refusing to mix different creators"
            )
        username = (
            discovered.username
            if authoritative and discovered.username
            else current.username or discovered.username
        )
        return ProfileIdentity(
            username=username,
            user_id=current.user_id or discovered.user_id,
            sec_uid=current.sec_uid or discovered.sec_uid,
            directory=current.directory or discovered.directory,
            aliases=tuple(dict.fromkeys(
                (*current.aliases, *discovered.aliases)
            )),
        )

    @staticmethod
    def _validate_target_identity(
        target: Target,
        identity: ProfileIdentity,
    ) -> None:
        if target.user_id and identity.user_id != target.user_id:
            raise TikTokError(
                "The media URL belongs to a different numeric user ID"
            )
        if target.sec_uid and identity.sec_uid != target.sec_uid:
            raise TikTokError(
                "The media URL belongs to a different TikTok secUid"
            )

    @staticmethod
    def _profile_url(identity: ProfileIdentity, target: Target) -> str:
        username = identity.username or target.username
        return f"{ROOT}/@{username}" if username else ROOT + "/"

    def _profile_output(self, identity: ProfileIdentity) -> Path:
        output_root = self.settings.output.expanduser().resolve()
        directory = safe_directory_name(
            identity.directory or identity.username
        )
        output = (output_root / directory).resolve()
        if not output.is_relative_to(output_root):
            raise TikTokError("Resolved profile folder escaped output root")
        return output

    def _download_item(
        self,
        item: MediaItem,
        *,
        output: Path,
        referer: str,
    ) -> DownloadSummary:
        summary = DownloadSummary()
        destination_dir = output / "stories" if item.is_story else output
        description = self._clean_description(
            item.description,
            "story" if item.is_story else f"TikTok post #{item.post_id}",
        )

        if item.is_photo:
            if (
                not self.settings.dry_run
                and len(item.image_urls) < max(1, item.image_count)
            ):
                item = self._refresh_incomplete_photo(item)
            part_count = max(len(item.image_urls), item.image_count, 1)
            for index in range(1, part_count + 1):
                urls = (
                    item.image_urls[index - 1]
                    if index <= len(item.image_urls)
                    else ()
                )
                extension = self._extension(
                    urls[0] if urls else "", "jpg"
                )
                filename = self._filename(
                    item.post_id,
                    description,
                    extension,
                    index=index,
                )
                outcome = self._download_media_part(
                    item,
                    output=output,
                    destination_dir=destination_dir,
                    filename=filename,
                    index=index,
                    referer=referer,
                )
                summary.record(outcome)
            return summary

        filename = self._filename(
            item.post_id, description, "mp4"
        )
        summary.record(self._download_media_part(
            item,
            output=output,
            destination_dir=destination_dir,
            filename=filename,
            index=None,
            referer=referer,
        ))
        return summary

    def _refresh_incomplete_photo(self, item: MediaItem) -> MediaItem:
        round_number = 0
        current = item
        required_count = max(len(item.image_urls), item.image_count, 1)
        last_error: Exception | str = "photo metadata contains missing URLs"
        while (
            not current.is_photo
            or current.image_count < required_count
            or len(current.image_urls) < required_count
        ):
            round_number += 1
            try:
                current = self.client.media_from_embed(
                    item.post_id, is_story=item.is_story
                )
                required_count = max(required_count, current.image_count)
                last_error = "photo metadata still contains missing URLs"
            except TikTokError as exc:
                last_error = exc
            if (
                current.is_photo
                and current.image_count >= required_count
                and len(current.image_urls) >= required_count
            ):
                return current
            delay = max(5.0, self.settings.sleep * 2.0)
            delay += random.uniform(0.0, 1.0)
            print(
                f"[retry] {item.post_id} photo metadata is incomplete "
                f"(round {round_number}: {last_error}); waiting {delay:.1f}s"
            )
            time.sleep(delay)
        return current

    def _download_media_part(
        self,
        item: MediaItem,
        *,
        output: Path,
        destination_dir: Path,
        filename: str,
        index: int | None,
        referer: str,
    ) -> DownloadOutcome:
        destination_dir = destination_dir.resolve()
        output = output.resolve()
        if not destination_dir.is_relative_to(output):
            raise TikTokError("Resolved media folder escaped profile folder")
        key = (
            f"{item.post_id}_{index:02d}"
            if index is not None
            else item.post_id
        )
        existing = self._find_existing(destination_dir, key)
        if existing and not self.settings.overwrite:
            print(f"[skip] {existing.name}")
            return DownloadOutcome.SKIPPED

        destination = existing or (destination_dir / filename)
        destination = destination.resolve()
        if not destination.is_relative_to(destination_dir):
            raise TikTokError("Resolved filename escaped its media folder")
        if self.settings.dry_run:
            print(f"[dry-run] {destination.name}")
            return DownloadOutcome.PLANNED

        destination_dir.mkdir(parents=True, exist_ok=True)
        self._download_with_refresh(
            item,
            destination=destination,
            index=index,
            referer=referer,
        )
        return DownloadOutcome.DOWNLOADED

    @staticmethod
    def _find_existing(directory: Path, key: str) -> Path | None:
        if not directory.is_dir():
            return None
        prefix = key + " "
        for child in directory.iterdir():
            if (
                child.is_file()
                and child.name.startswith(prefix)
                and not child.name.endswith(".part")
            ):
                return child
        return None

    def _download_with_refresh(
        self,
        item: MediaItem,
        *,
        destination: Path,
        index: int | None,
        referer: str,
    ) -> None:
        current = item
        round_number = 0
        last_error: Exception | str = "no media URL"
        temporary = destination.with_name(destination.name + ".part")

        while True:
            round_number += 1
            urls = self._part_urls(current, index)
            self._wait_for_download_slot()
            if round_number == 1:
                print(f"[download] {destination.name}")
            for url in urls:
                try:
                    self._write_media(
                        url, temporary, destination, referer=referer
                    )
                    self.last_download_at = time.monotonic()
                    return
                except (OSError, TikTokError) as exc:
                    last_error = exc
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

            # Signed CDN addresses can expire while a large profile is being
            # scanned. Refresh this exact item, but never advance the queue.
            try:
                current = self.client.media_from_embed(
                    item.post_id, is_story=item.is_story
                )
            except TikTokError as exc:
                last_error = exc

            delay = max(5.0, self.settings.sleep * 2.0)
            delay += random.uniform(0.0, 1.0)
            print(
                f"[retry] {destination.name} still failed "
                f"(round {round_number}: {last_error}); refreshed links, "
                f"waiting {delay:.1f}s"
            )
            time.sleep(delay)

    @staticmethod
    def _part_urls(item: MediaItem, index: int | None) -> tuple[str, ...]:
        if index is None:
            return item.video_urls
        position = index - 1
        if position < 0 or position >= len(item.image_urls):
            return ()
        return item.image_urls[position]

    def _write_media(
        self,
        url: str,
        temporary: Path,
        destination: Path,
        *,
        referer: str,
    ) -> None:
        with self.client.request(
            url,
            headers={"Referer": referer},
            attempts=1,
        ) as response:
            content_type = response.headers.get_content_type().lower()
            first = response.read(16 * 1024)
            stripped = first.lstrip().lower()
            if (
                content_type.startswith("text/")
                or content_type in {
                    "application/json",
                    "application/javascript",
                    "application/xml",
                }
                or stripped.startswith((b"<!doctype html", b"<html", b"{"))
            ):
                raise TikTokError(
                    f"TikTok CDN returned {content_type} instead of media"
                )
            if not first:
                raise TikTokError("TikTok CDN returned an empty response")
            with temporary.open("wb") as output:
                output.write(first)
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        os.replace(temporary, destination)

    def _wait_for_download_slot(self) -> None:
        if self.settings.sleep <= 0 or self.last_download_at is None:
            return
        delay = self.settings.sleep + random.uniform(
            0.0, min(1.0, self.settings.sleep * 0.25)
        )
        remaining = delay - (time.monotonic() - self.last_download_at)
        if remaining > 0:
            print(f"[wait] {remaining:.1f}s")
            time.sleep(remaining)

    @staticmethod
    def _clean_description(value: str, fallback: str) -> str:
        value = html.unescape(str(value or fallback))
        value = " ".join(value.split())
        value = INVALID_FILENAME_RE.sub("_", value).strip(" .")
        return value[:140].rstrip(" .") or fallback

    @staticmethod
    def _extension(url: str, fallback: str) -> str:
        extension = Path(urlsplit(url).path).suffix.lower().lstrip(".")
        if extension == "jpeg":
            extension = "jpg"
        if not EXTENSION_RE.fullmatch(extension):
            extension = fallback
        return extension

    @staticmethod
    def _filename(
        post_id: str,
        description: str,
        extension: str,
        *,
        index: int | None = None,
    ) -> str:
        suffix = f"_{index:02d}" if index is not None else ""
        return f"{post_id}{suffix} {description}.{extension}"
