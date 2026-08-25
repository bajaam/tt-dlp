#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line application and downloader implementation for tt-dlp.

This intentionally avoids TikTok's normal profile HTML, which is guarded by
the short-lived JavaScript WAF challenge that can return HTTP 403 to non-browser
clients.  It starts with TikTok's official creator embed, obtains the public
creator ID from a post embed, and then reads the creator item-list API.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
from http.cookiejar import CookieJar, MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from . import __version__


ROOT = "https://www.tiktok.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
PROFILE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?tiktok\.com/@([\w.-]+)"
    r"(?:/(?:video|photo|story)/(\d+))?",
    re.IGNORECASE,
)
SHORT_RE = re.compile(
    r"^(?:https?://)?(?:vm|vt)\.tiktok\.com/",
    re.IGNORECASE,
)
STATE_RE = re.compile(
    r'<script[^>]+id=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>'
    r"(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class TikTokError(RuntimeError):
    pass


class TikTokDownloader:
    def __init__(self, args):
        self.args = args
        self.default_headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/json;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        }
        self.cookie_jar = self._load_cookies(args.cookies)
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.anonymous_opener = build_opener(
            HTTPCookieProcessor(CookieJar())
        )
        self.device_id = str(random.randint(
            7_250_000_000_000_000_000,
            7_325_099_899_999_994_577,
        ))
        self.last_download_at = None
        self.username = ""
        self.profile_url = ""
        self.output = None

    @staticmethod
    def _load_cookies(filename):
        if not filename:
            return CookieJar()
        cookie_path = Path(filename).expanduser()
        if not cookie_path.is_file():
            raise TikTokError(f"Cookie file not found: {cookie_path}")
        jar = MozillaCookieJar(str(cookie_path))
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError) as exc:
            raise TikTokError(
                "Could not read the cookie file. Export it in Netscape "
                f"cookies.txt format: {exc}"
            ) from exc
        print(f"Loaded {len(jar)} cookies from {cookie_path}")
        return jar

    def run(self, value):
        self.username, target_id = self._parse_input(value)
        self.profile_url = f"{ROOT}/@{self.username}"
        output_root = (
            Path(self.args.output).expanduser()
            if self.args.output
            else Path.home() / "Downloads" / "TikTok"
        )
        self.output = output_root / self.username

        try:
            creator = self._embed_data(f"/embed/@{self.username}")
        except TikTokError:
            if not self.args.cookies:
                raise
            # Authenticated profile-page JSON can still identify a creator
            # when the public embed endpoint hides a private profile.
            creator = {}
        user = creator.get("userInfo") or {}
        author_id = str(user.get("id") or "")
        if user.get("code") not in (None, 0, 200):
            raise TikTokError(
                user.get("message") or f"TikTok rejected @{self.username}"
            )
        is_private = bool(user.get("privateAccount"))
        if is_private and not self.args.cookies:
            raise TikTokError(
                f"@{self.username} is private. Provide a Netscape cookies.txt "
                "file for an account that is allowed to view it."
            )

        recent = creator.get("videoList") or ()
        seed_id = target_id or (recent and str(recent[0].get("id") or ""))
        sec_uid = None
        if seed_id:
            try:
                post_embed = self._embed_data(f"/embed/v2/{seed_id}")
                video_data = post_embed.get("videoData") or {}
                author = video_data.get("authorInfos") or {}
                sec_uid = author.get("secUid")
            except TikTokError:
                if not self.args.cookies:
                    raise
        if (not sec_uid or (self.args.stories and not author_id)) and self.args.cookies:
            authenticated_identity = self._authenticated_profile_identity()
            sec_uid = sec_uid or authenticated_identity.get("secUid")
            author_id = author_id or authenticated_identity.get("id", "")
        if not sec_uid and not (self.args.stories and author_id):
            if is_private:
                raise TikTokError(
                    f"The supplied cookies cannot access @{self.username}. "
                    "Make sure they are current and belong to an account that "
                    "follows this private profile."
                )
            print(f"@{self.username} has no accessible posts.")
            return 0

        print(f"TikTok user: @{self.username}")
        print(f"Saving to: {self.output}")
        if not self.args.dry_run:
            self.output.mkdir(parents=True, exist_ok=True)

        # Finish reading every available profile page before starting any
        # media download. This keeps discovery separate from downloading and
        # gives us a complete queue up front.
        posts = (
            self._collect_posts(
                sec_uid,
                public_embed_has_posts=bool(recent),
                is_private=is_private,
            )
            if sec_uid else []
        )
        print(f"Profile scan complete: {len(posts)} posts found.")

        stories = []
        if self.args.stories:
            if not self.args.cookies:
                raise TikTokError(
                    "TikTok story access requires a current Netscape "
                    "cookies.txt file. Pass --cookies or set cookies_file "
                    "in config.json."
                )
            if not author_id:
                raise TikTokError(
                    f"Could not identify @{self.username} for story access"
                )
            stories = self._collect_stories(author_id)
            print(
                f"Story scan complete: {len(stories)} active "
                f"{'story' if len(stories) == 1 else 'stories'} found."
            )

        if target_id:
            matching_posts = [
                item for item in posts
                if str(item.get("id") or "") == target_id
            ]
            matching_stories = [
                item for item in stories
                if str(item.get("id") or "") == target_id
            ]
            posts = matching_posts
            stories = matching_stories
            if not posts and not stories:
                raise TikTokError(
                    f"Post or active story {target_id} was not found for "
                    f"@{self.username}"
                )
        elif self.args.limit:
            posts = posts[:self.args.limit]

        downloaded = skipped = failed = examined = 0
        media_queue = [(item, False) for item in posts]
        media_queue.extend((item, True) for item in stories)
        print(
            f"Starting download queue: {len(posts)} posts, "
            f"{len(stories)} stories."
        )
        for item, is_story in media_queue:
            examined += 1

            results = self._download_post(item, is_story=is_story)
            downloaded += results[0]
            skipped += results[1]
            failed += results[2]

        print()
        print(
            f"Finished: {downloaded} downloaded, {skipped} skipped, "
            f"{failed} failed"
        )
        return 1 if failed else 0

    def _collect_posts(self, sec_uid, *, public_embed_has_posts, is_private):
        posts = list(self._iter_posts(sec_uid))
        if (
            posts
            or not self.args.cookies
            or is_private
            or not public_embed_has_posts
        ):
            return posts

        # A signed-in TikTok session can occasionally return an empty creator
        # list for a public account even though its public embed contains
        # posts. Retry only the profile scan without cookies; restore the
        # authenticated opener before downloading media.
        print(
            "Authenticated profile scan returned no posts; "
            "retrying public scan without cookies..."
        )
        authenticated_opener = self.opener
        try:
            self.opener = self.anonymous_opener
            return list(self._iter_posts(sec_uid))
        finally:
            self.opener = authenticated_opener

    def _authenticated_profile_identity(self):
        """Find this profile's numeric ID and secUid in signed-in HTML."""
        with self._request(self.profile_url) as response:
            page = response.read().decode("utf-8", "replace")

        script_values = re.findall(
            r"<script[^>]*>(.*?)</script>", page,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for value in script_values:
            value = html.unescape(value.strip())
            if not value or value[0] not in "[{":
                continue
            try:
                data = json.loads(value)
            except ValueError:
                continue
            identity = self._find_profile_identity(data)
            if identity:
                return identity

        escaped_username = re.escape(self.username)
        sec_uid_patterns = (
            rf'"uniqueId"\s*:\s*"{escaped_username}".{{0,3000}}?'
            rf'"secUid"\s*:\s*"([^"]+)"',
            rf'"secUid"\s*:\s*"([^"]+)".{{0,3000}}?'
            rf'"uniqueId"\s*:\s*"{escaped_username}"',
        )
        identity = {}
        for pattern in sec_uid_patterns:
            match = re.search(pattern, page, re.DOTALL | re.IGNORECASE)
            if match:
                identity["secUid"] = match.group(1)
                break
        id_patterns = (
            rf'"uniqueId"\s*:\s*"{escaped_username}".{{0,3000}}?'
            rf'"id"\s*:\s*"(\d+)"',
            rf'"id"\s*:\s*"(\d+)".{{0,3000}}?'
            rf'"uniqueId"\s*:\s*"{escaped_username}"',
        )
        for pattern in id_patterns:
            match = re.search(pattern, page, re.DOTALL | re.IGNORECASE)
            if match:
                identity["id"] = match.group(1)
                break
        return identity

    def _authenticated_profile_id(self):
        """Backward-compatible helper returning only the profile secUid."""
        return self._authenticated_profile_identity().get("secUid")

    def _find_profile_identity(self, value):
        if isinstance(value, dict):
            unique_id = value.get("uniqueId") or value.get("unique_id")
            if str(unique_id or "").lower() == self.username.lower():
                identity = {}
                sec_uid = value.get("secUid") or value.get("sec_uid")
                author_id = value.get("id") or value.get("userId")
                if sec_uid:
                    identity["secUid"] = str(sec_uid)
                if author_id:
                    identity["id"] = str(author_id)
                if identity:
                    return identity
            for child in value.values():
                found = self._find_profile_identity(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._find_profile_identity(child)
                if found:
                    return found
        return None

    def _find_profile_id(self, value):
        identity = self._find_profile_identity(value)
        return identity and identity.get("secUid")

    def _parse_input(self, value):
        value = value.strip()
        if value.startswith("@") and "/" not in value:
            return value[1:], None
        if re.fullmatch(r"[\w.-]+", value):
            return value, None

        if SHORT_RE.match(value):
            url = value if value.startswith("http") else "https://" + value
            with self._request(url, stream=True) as response:
                value = response.geturl()

        match = PROFILE_RE.match(value)
        if not match:
            raise TikTokError(
                "Expected @username or a TikTok profile/video/photo/story URL"
            )
        return match.group(1), match.group(2)

    def _request(self, url, *, stream=False, headers=None, attempts=4):
        del stream  # urllib responses are streamed until read.
        last_error = "unknown network error"
        for attempt in range(1, attempts + 1):
            try:
                request_headers = dict(self.default_headers)
                if headers:
                    request_headers.update(headers)
                request = Request(url, headers=request_headers)
                response = self.opener.open(request, timeout=90)
                if response.getcode() == 200:
                    return response
                message = f"HTTP {response.getcode()} for {response.geturl()}"
                last_error = TikTokError(message)
                response.close()
            except HTTPError as exc:
                # HTTPError is also a file-like response. Close it here and
                # retain only its message so its finalizer cannot encounter a
                # half-closed socket later (notably on Python 3.14).
                last_error = f"HTTP {exc.code} for {exc.geturl()}"
                exc.close()
            except (URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)

            if attempt < attempts:
                delay = max(self.args.sleep, min(3 * attempt, 12))
                delay += random.uniform(0.0, 1.0)
                print(
                    f"Request failed; retrying in {delay:.1f}s "
                    f"({attempt}/{attempts})"
                )
                time.sleep(delay)
        raise TikTokError(str(last_error))

    def _embed_data(self, path):
        with self._request(ROOT + path) as response:
            page = response.read().decode("utf-8", "replace")

        match = STATE_RE.search(page)
        if not match:
            raise TikTokError(f"TikTok embed data was missing for {path}")
        try:
            state = json.loads(match.group(1))
            sources = state["source"]["data"]
            data = sources.get(path) or sources.get(path + "/")
            if not data:
                raise KeyError(path)
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise TikTokError(f"Invalid TikTok embed data for {path}: {exc}")

    def _iter_posts(self, sec_uid):
        cursor = int(time.time()) * 1000
        seen = set()
        page = 1

        while cursor >= 1_472_706_000_000:
            params = {
                "aid": "1988",
                "app_language": "en",
                "app_name": "tiktok_web",
                "browser_language": "en-US",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": "5.0 (Windows)",
                "channel": "tiktok_web",
                "cookie_enabled": "true",
                "count": "15",
                "cursor": str(cursor),
                "device_id": self.device_id,
                "device_platform": "web_pc",
                "focus_state": "true",
                "from_page": "user",
                "history_len": "2",
                "is_fullscreen": "false",
                "is_page_visible": "true",
                "language": "en",
                "os": "windows",
                "priority_region": "",
                "referer": "",
                "region": "US",
                "screen_height": "1080",
                "screen_width": "1920",
                "secUid": sec_uid,
                "type": "1",
                "tz_name": "UTC",
                "verifyFp": "verify_" + "".join(
                    random.choices("0123456789abcdef", k=7)
                ),
                "webcast_language": "en",
            }
            data = self._request_api(params)
            items = data.get("itemList") or ()
            new_items = 0
            for item in items:
                post_id = str(item.get("id") or "")
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                new_items += 1
                yield item

            if new_items:
                print(
                    f"Profile page {page}: {new_items} new posts "
                    f"({len(seen)} total)"
                )
                page += 1

            if not data.get("hasMorePrevious"):
                break

            try:
                next_cursor = int(items[-1]["createTime"]) * 1000
            except (IndexError, KeyError, TypeError, ValueError):
                next_cursor = cursor - 7 * 86_400_000
            if not new_items or next_cursor >= cursor:
                next_cursor = cursor - 7 * 86_400_000
            cursor = next_cursor

    def _request_api(self, params):
        last_error = None
        for attempt in range(1, 5):
            try:
                api_url = (
                    ROOT + "/api/creator/item_list/?" + urlencode(params)
                )
                with self._request(
                    api_url,
                    headers={"Referer": self.profile_url},
                    attempts=1,
                ) as response:
                    data = json.loads(
                        response.read().decode("utf-8", "replace")
                    )
                status = data.get("statusCode", data.get("status_code", 0))
                if status:
                    raise TikTokError(
                        data.get("statusMsg")
                        or data.get("status_msg")
                        or f"TikTok API error {status}"
                    )
                # TikTok occasionally answers a valid first cursor with an
                # empty page and hasMorePrevious=true, then returns posts for
                # the identical request moments later. Retry these transient
                # empty responses before allowing pagination to move back.
                if (
                    not (data.get("itemList") or ())
                    and data.get("hasMorePrevious")
                    and attempt < 4
                ):
                    raise TikTokError("TikTok API returned an empty page")
                return data
            except (OSError, ValueError, TikTokError) as exc:
                last_error = exc
                if attempt < 4:
                    delay = max(self.args.sleep, min(3 * attempt, 12))
                    delay += random.uniform(0.0, 1.0)
                    print(
                        f"TikTok API failed; retrying in {delay:.1f}s "
                        f"({attempt}/4)"
                    )
                    time.sleep(delay)
        raise TikTokError(str(last_error))

    def _request_story_api(self, path, params):
        query = {"aid": "1988"}
        query.update(params)
        last_error = None
        for attempt in range(1, 5):
            try:
                with self._request(
                    ROOT + path + "?" + urlencode(query),
                    headers={"Referer": self.profile_url},
                    attempts=1,
                ) as response:
                    data = json.loads(
                        response.read().decode("utf-8", "replace")
                    )
                status = data.get("statusCode", data.get("status_code", 0))
                if status:
                    raise TikTokError(
                        data.get("statusMsg")
                        or data.get("status_msg")
                        or f"TikTok story API error {status}"
                    )
                return data
            except (OSError, ValueError, TikTokError) as exc:
                last_error = exc
                if attempt < 4:
                    delay = max(self.args.sleep, min(3 * attempt, 12))
                    delay += random.uniform(0.0, 1.0)
                    print(
                        f"TikTok story API failed; retrying in {delay:.1f}s "
                        f"({attempt}/4)"
                    )
                    time.sleep(delay)
        raise TikTokError(str(last_error))

    def _collect_stories(self, author_id):
        data = self._request_story_api(
            "/api/story/user/story_list/",
            {"authorIds": str(author_id)},
        )
        story_ids = []
        for entry in data.get("storyIdListStructs") or ():
            if str(entry.get("authorId") or "") != str(author_id):
                continue
            story_ids.extend(
                str(value) for value in entry.get("storyIds") or () if value
            )

        stories = []
        for story_id in dict.fromkeys(story_ids):
            embed = self._embed_data(f"/embed/v2/{story_id}")
            stories.append(self._story_from_embed(story_id, embed))
        return stories

    @staticmethod
    def _story_from_embed(story_id, embed):
        video_data = embed.get("videoData") or {}
        item = video_data.get("itemInfos") or {}
        actual_id = str(item.get("id") or story_id)
        if actual_id != str(story_id):
            raise TikTokError(
                f"TikTok returned story {actual_id} while requesting {story_id}"
            )

        normalized = {
            "id": actual_id,
            "desc": item.get("text") or "story",
        }
        image_post = (
            video_data.get("imagePostInfo")
            or item.get("imagePostInfo")
            or item.get("imagePost")
        )
        if image_post:
            normalized["imagePost"] = image_post
            return normalized

        video = item.get("video") or {}
        urls = video.get("urls") or video.get("urlList") or ()
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            play_address = video.get("playAddr")
            if isinstance(play_address, str):
                urls = [play_address]
            elif isinstance(play_address, dict):
                urls = play_address.get("urlList") or ()
        normalized["video"] = {"playAddr": {"urlList": list(urls)}}
        return normalized

    def _download_post(self, item, *, is_story=False):
        post_id = str(item.get("id") or "unknown")
        image_post = item.get("imagePost")
        description = self._clean_description(
            item.get("desc") or (image_post and image_post.get("title")),
            "story" if is_story else f"TikTok post #{post_id}",
        )
        destination = self.output / "stories" if is_story else self.output
        if is_story and not self.args.dry_run:
            destination.mkdir(parents=True, exist_ok=True)

        if image_post:
            images = image_post.get("images") or ()
            if not images:
                print(f"[failed] {post_id}: photo post contains no image URLs")
                return 0, 0, 1
            totals = [0, 0, 0]
            for number, image in enumerate(images, 1):
                image_data = image.get("imageURL") or {}
                urls = image_data.get("urlList") or ()
                extension = self._extension(urls[0] if urls else "", "jpg")
                filename = self._filename(
                    post_id, description, extension, number
                )
                result = self._download_urls(urls, destination / filename)
                totals[result] += 1
            return tuple(totals)

        video = item.get("video") or {}
        urls = self._video_urls(video)
        filename = self._filename(post_id, description, "mp4")
        result = self._download_urls(urls, destination / filename)
        totals = [0, 0, 0]
        totals[result] = 1
        return tuple(totals)

    def _video_urls(self, video):
        quality = {}
        bitrate_info = video.get("bitrateInfo") or ()
        if isinstance(bitrate_info, dict):
            bitrate_info = (bitrate_info,)
        for entry in bitrate_info:
            address = entry.get("PlayAddr") or {}
            try:
                size = int(address.get("Width") or 0) * int(
                    address.get("Height") or 0
                )
            except (TypeError, ValueError):
                size = 0
            quality.setdefault(size, []).extend(address.get("UrlList") or ())

        urls = []
        for size in sorted(quality, reverse=True):
            urls.extend(quality[size])
        play_address = video.get("playAddr")
        if isinstance(play_address, str):
            urls.append(play_address)
        elif isinstance(play_address, dict):
            urls.extend(play_address.get("urlList") or ())
        return list(dict.fromkeys(urls))

    def _download_urls(self, urls, destination):
        if destination.exists() and not self.args.overwrite:
            print(f"[skip] {destination.name}")
            return 1
        if self.args.dry_run:
            print(f"[dry-run] {destination.name}")
            return 1
        if not urls:
            print(f"[failed] {destination.name}: no media URL")
            return 2

        self._wait_for_download_slot()
        temporary = Path(str(destination) + ".part")
        headers = {"Referer": self.profile_url}
        last_error = None
        print(f"[download] {destination.name}")
        media_attempt = 0
        while True:
            media_attempt += 1
            for url in urls:
                try:
                    # TikTok usually supplies several equivalent CDN URLs.
                    # A 403 from one URL should immediately fall through to
                    # the next URL instead of retrying the rejected one.
                    with self._request(
                        url, stream=True, headers=headers, attempts=1
                    ) as response:
                        with temporary.open("wb") as output:
                            while True:
                                chunk = response.read(256 * 1024)
                                if not chunk:
                                    break
                                output.write(chunk)
                    os.replace(temporary, destination)
                    self.last_download_at = time.monotonic()
                    return 0
                except (OSError, TikTokError) as exc:
                    last_error = exc
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

            # Do not advance to the next post after a media failure. Keep the
            # current post selected and retry its available links until one
            # succeeds or the user stops the program with Ctrl+C.
            delay = max(5.0, self.args.sleep * 2.0)
            delay += random.uniform(0.0, 1.0)
            print(
                f"[retry] {destination.name} still failed "
                f"(round {media_attempt}: {last_error}); waiting {delay:.1f}s"
            )
            time.sleep(delay)

    def _wait_for_download_slot(self):
        if self.args.sleep <= 0 or self.last_download_at is None:
            return
        delay = self.args.sleep + random.uniform(
            0.0, min(1.0, self.args.sleep * 0.25)
        )
        remaining = delay - (time.monotonic() - self.last_download_at)
        if remaining > 0:
            print(f"[wait] {remaining:.1f}s")
            time.sleep(remaining)

    @staticmethod
    def _clean_description(value, fallback):
        value = html.unescape(str(value or fallback))
        value = " ".join(value.split())
        value = INVALID_FILENAME_RE.sub("_", value).strip(" .")
        return value[:150].rstrip(" .") or fallback

    @staticmethod
    def _extension(url, fallback):
        extension = Path(urlsplit(url).path).suffix.lower().lstrip(".")
        if extension == "jpeg":
            extension = "jpg"
        if not extension or len(extension) > 5:
            extension = fallback
        return extension

    @staticmethod
    def _filename(post_id, description, extension, number=None):
        suffix = f"_{number:02d}" if number is not None else ""
        return f"{post_id}{suffix} {description}.{extension}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="tt-dlp",
        description=(
            "Download TikTok videos, photo posts, and active stories from "
            "one or more queued profiles or post URLs"
        )
    )
    parser.add_argument(
        "targets", nargs="*",
        help="one or more @usernames or TikTok profile/video/photo/story URLs",
    )
    parser.add_argument(
        "-c", "--config",
        help="JSON config file (prompted when no targets are supplied)",
    )
    parser.add_argument(
        "--init-config", nargs="?", const="config.json", metavar="FILE",
        help="create a new config and queue (default: ./config.json)",
    )
    parser.add_argument(
        "-o", "--output",
        help=(
            "base destination folder (default: ~/Downloads/TikTok); each "
            "profile is saved in its own subfolder"
        ),
    )
    parser.add_argument(
        "--cookies",
        help="optional Netscape-format cookies.txt file",
    )
    parser.add_argument(
        "--queue-file",
        help="text file containing one target per line",
    )
    parser.add_argument(
        "--limit", type=int,
        help="download at most this many posts (0 means all)",
    )
    parser.add_argument(
        "--sleep", type=float,
        help="minimum seconds between media downloads (default: 2.0)",
    )
    parser.add_argument(
        "--overwrite", action="store_true", default=None,
        help="replace files that already exist",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="show filenames without downloading media",
    )
    story_group = parser.add_mutually_exclusive_group()
    story_group.add_argument(
        "--stories", dest="stories", action="store_true", default=None,
        help="also download active stories (requires cookies)",
    )
    story_group.add_argument(
        "--no-stories", dest="stories", action="store_false",
        help="disable stories when enabled by the config file",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def _config_path(args):
    if args.config:
        return Path(args.config).expanduser()
    if args.targets or args.queue_file:
        return None

    configured = os.environ.get("TT_DLP_CONFIG")
    if configured:
        return Path(configured).expanduser()

    candidates = [
        Path.cwd() / "config.json",
        Path.home() / ".config" / "tt-dlp" / "config.json",
    ]
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        candidates.append(Path(xdg_config) / "tt-dlp" / "config.json")
    app_data = os.environ.get("APPDATA")
    if app_data:
        candidates.append(Path(app_data) / "tt-dlp" / "config.json")
    if sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support"
            / "tt-dlp" / "config.json"
        )

    # Prefer a project-specific config in the current directory, then common
    # per-user config locations. Return immediately when one already exists so
    # a plain `tt-dlp` command stays non-interactive.
    seen = set()
    unique_candidates = []
    for candidate in candidates:
        candidate = candidate.expanduser()
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)
            if candidate.is_file():
                return candidate

    default = unique_candidates[1]
    if sys.stdin.isatty():
        try:
            entered = input(f"Config file [{default}]: ").strip()
        except EOFError:
            entered = ""
        return Path(entered).expanduser() if entered else default
    return default


def _load_config(path):
    if path is None:
        return {}, Path.cwd()
    path = path.resolve()
    if not path.is_file():
        raise TikTokError(
            f"Config file not found: {path}\n"
            "Run 'tt-dlp --init-config ~/.config/tt-dlp/config.json' "
            "or pass profiles directly."
        )
    try:
        with path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, ValueError) as exc:
        raise TikTokError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise TikTokError("The config file must contain one JSON object")
    print(f"Using config: {path}")
    return config, path.parent


def _relative_path(value, base):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return str(path if path.is_absolute() else base / path)


def _read_queue_file(path):
    try:
        with Path(path).open("r", encoding="utf-8-sig") as file:
            return [
                line.strip() for line in file
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError as exc:
        raise TikTokError(f"Could not read queue file {path}: {exc}") from exc


def prepare_run(args):
    config_file = _config_path(args)
    config, config_dir = _load_config(config_file)

    args.output = args.output or config.get("output")
    args.cookies = args.cookies or config.get("cookies_file")
    args.queue_file = args.queue_file or config.get("queue_file")
    args.limit = args.limit if args.limit is not None else config.get("limit", 0)
    args.sleep = args.sleep if args.sleep is not None else config.get("sleep", 2.0)
    args.overwrite = (
        args.overwrite if args.overwrite is not None
        else bool(config.get("overwrite", False))
    )
    args.dry_run = (
        args.dry_run if args.dry_run is not None
        else bool(config.get("dry_run", False))
    )
    args.stories = (
        args.stories if args.stories is not None
        else bool(config.get("stories", False))
    )

    args.output = _relative_path(args.output, config_dir)
    args.cookies = _relative_path(args.cookies, config_dir)
    args.queue_file = _relative_path(args.queue_file, config_dir)

    try:
        args.limit = int(args.limit)
        args.sleep = float(args.sleep)
    except (TypeError, ValueError) as exc:
        raise TikTokError("Config values 'limit' and 'sleep' must be numbers") from exc
    if args.limit < 0 or args.sleep < 0:
        raise TikTokError("Config values 'limit' and 'sleep' cannot be negative")

    targets = list(args.targets)
    configured = config.get("queue", [])
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        raise TikTokError("Config value 'queue' must be a JSON list")
    targets.extend(str(value).strip() for value in configured if str(value).strip())
    if args.queue_file:
        targets.extend(_read_queue_file(args.queue_file))

    # Preserve queue order while avoiding duplicate scans/downloads.
    targets = list(dict.fromkeys(targets))
    if not targets:
        raise TikTokError(
            "The queue is empty. Add profiles to config.json, queue.txt, or "
            "the command line."
        )
    return args, targets


def initialize_config(filename):
    config_path = Path(filename).expanduser().resolve()
    queue_path = config_path.parent / "queue.txt"
    if config_path.exists():
        raise TikTokError(f"Refusing to overwrite existing config: {config_path}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "output": "~/Downloads/TikTok",
        "cookies_file": "",
        "queue_file": queue_path.name,
        "queue": [],
        "sleep": 2.0,
        "limit": 0,
        "overwrite": False,
        "dry_run": False,
        "stories": False,
    }
    with config_path.open("x", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    if not queue_path.exists():
        with queue_path.open("x", encoding="utf-8") as file:
            file.write(
                "# Add one username or TikTok URL per line.\n"
                "# profile_one\n"
                "# https://www.tiktok.com/@profile_two\n"
            )
    print(f"Created config: {config_path}")
    print(f"Created queue:  {queue_path}")
    print("Add profiles to the queue, then run: tt-dlp")
    return 0


def main(argv=None):
    try:
        # Windows consoles may use a legacy encoding even though TikTok
        # descriptions are Unicode. Replace only unrepresentable terminal
        # glyphs; filenames on disk retain their original Unicode text.
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(errors="replace")
        parsed = parse_args(argv)
        if parsed.init_config:
            return initialize_config(parsed.init_config)
        args, targets = prepare_run(parsed)
        downloader = TikTokDownloader(args)
        queue_failures = 0
        print(f"Queued targets: {len(targets)}")
        for number, target in enumerate(targets, 1):
            print()
            print(f"=== Queue {number}/{len(targets)}: {target} ===")
            try:
                result = downloader.run(target)
                queue_failures += int(bool(result))
            except TikTokError as exc:
                queue_failures += 1
                print(f"Error for {target}: {exc}", file=sys.stderr)
        print()
        print(
            f"Queue finished: {len(targets) - queue_failures} succeeded, "
            f"{queue_failures} failed"
        )
        return 1 if queue_failures else 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except TikTokError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
