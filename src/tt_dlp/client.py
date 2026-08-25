"""Small standard-library TikTok web client used by tt-dlp."""

from __future__ import annotations

import html
import json
import random
import re
import time
from http.cookiejar import CookieJar, MozillaCookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .errors import TikTokError
from .models import MediaItem, ProfileIdentity, Settings
from .targets import (
    validate_post_id,
    validate_sec_uid,
    validate_user_id,
    validate_username,
)


ROOT = "https://www.tiktok.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
SHORT_URL_USER_AGENT = "facebookexternalhit/1.1"
STATE_RE = re.compile(
    r'<script[^>]+id=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>'
    r"(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


class TikTokClient:
    """HTTP and response-normalization layer.

    TikTok's normal profile page is protected by a JavaScript challenge.  The
    client starts from TikTok's creator/media embeds, then enumerates posts by
    the stable ``secUid`` exposed in media metadata.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.default_headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/json;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        }
        self.cookie_jar = self._load_cookies(settings)
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.anonymous_opener = build_opener(
            HTTPCookieProcessor(CookieJar())
        )
        self.device_id = str(random.randint(
            7_250_000_000_000_000_000,
            7_325_099_899_999_994_577,
        ))

    @property
    def has_cookies(self) -> bool:
        for cookie in self.cookie_jar:
            domain = str(cookie.domain or "").lower().lstrip(".")
            if domain == "tiktok.com" or domain.endswith(".tiktok.com"):
                return True
        return False

    @staticmethod
    def _load_cookies(settings: Settings) -> CookieJar:
        filename = settings.cookies
        if filename is None:
            return CookieJar()
        if not filename.is_file():
            raise TikTokError(f"Cookie file not found: {filename}")
        jar = MozillaCookieJar(str(filename))
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError) as exc:
            raise TikTokError(
                "Could not read the cookie file. Export it in Netscape "
                f"cookies.txt format: {exc}"
            ) from exc
        print(f"Loaded {len(jar)} cookies from {filename}")
        return jar

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        attempts: int = 4,
        anonymous: bool = False,
    ):
        """Open a response, retrying transient failures.

        The caller owns the returned response and should use it as a context
        manager.  HTTPError responses are closed immediately, which avoids a
        Python 3.14 response-finalizer warning on Windows.
        """
        opener = self.anonymous_opener if anonymous else self.opener
        last_error = "unknown network error"
        for attempt in range(1, attempts + 1):
            try:
                request_headers = dict(self.default_headers)
                if headers:
                    request_headers.update(headers)
                response = opener.open(
                    Request(url, headers=request_headers), timeout=90
                )
                if response.getcode() == 200:
                    return response
                last_error = (
                    f"HTTP {response.getcode()} for {response.geturl()}"
                )
                response.close()
            except HTTPError as exc:
                last_error = f"HTTP {exc.code} for {exc.geturl()}"
                try:
                    exc.close()
                except (OSError, ValueError):
                    pass
            except (URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)

            if attempt < attempts:
                delay = self._retry_delay(attempt)
                print(
                    f"Request failed; retrying in {delay:.1f}s "
                    f"({attempt}/{attempts})"
                )
                time.sleep(delay)
        raise TikTokError(str(last_error))

    def resolve_short_url(self, url: str) -> str:
        with self.request(
            url,
            headers={"User-Agent": SHORT_URL_USER_AGENT},
        ) as response:
            return response.geturl()

    def embed_data(self, path: str) -> dict:
        with self.request(ROOT + path) as response:
            page = response.read().decode("utf-8", "replace")
        match = STATE_RE.search(page)
        if not match:
            raise TikTokError(f"TikTok embed data was missing for {path}")
        try:
            state = json.loads(match.group(1))
            sources = state["source"]["data"]
            data = sources.get(path) or sources.get(path + "/")
            if not isinstance(data, dict):
                raise KeyError(path)
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise TikTokError(f"Invalid TikTok embed data for {path}: {exc}")

    def creator_data(self, username: str) -> dict:
        return self.embed_data(f"/embed/@{validate_username(username)}")

    def media_from_embed(
        self,
        post_id: str,
        *,
        is_story: bool = False,
    ) -> MediaItem:
        post_id = validate_post_id(str(post_id))
        embed = self.embed_data(f"/embed/v2/{post_id}")
        video_data = embed.get("videoData")
        if not isinstance(video_data, dict):
            raise TikTokError(f"TikTok returned no media data for {post_id}")
        raw_item = video_data.get("itemInfos")
        if not isinstance(raw_item, dict):
            raise TikTokError(f"TikTok returned an invalid item for {post_id}")
        raw_item = dict(raw_item)
        image_post = self._photo_metadata(
            video_data.get("imagePostInfo"),
            raw_item.get("imagePostInfo"),
            raw_item.get("imagePost"),
        )
        if isinstance(image_post, dict):
            raw_item["imagePost"] = image_post
        author = self.identity_from_author(video_data.get("authorInfos"))
        return self.normalize_item(
            raw_item,
            expected_id=post_id,
            author=author,
            is_story=is_story,
        )

    @staticmethod
    def identity_from_creator(creator: dict) -> ProfileIdentity | None:
        user = creator.get("userInfo")
        if not isinstance(user, dict):
            return None
        return TikTokClient.identity_from_author(user)

    @staticmethod
    def identity_from_author(value) -> ProfileIdentity | None:
        if not isinstance(value, dict):
            return None
        username = value.get("uniqueId") or value.get("unique_id")
        user_id = value.get("id") or value.get("userId") or value.get("uid")
        sec_uid = value.get("secUid") or value.get("sec_uid")
        if not username:
            return None
        try:
            username = validate_username(str(username))
            user_id = validate_user_id(str(user_id)) if user_id else ""
            sec_uid = validate_sec_uid(str(sec_uid)) if sec_uid else ""
        except TikTokError:
            return None
        return ProfileIdentity(
            username=username,
            user_id=user_id,
            sec_uid=sec_uid,
        )

    @classmethod
    def normalize_item(
        cls,
        value,
        *,
        expected_id: str | None = None,
        author: ProfileIdentity | None = None,
        is_story: bool = False,
    ) -> MediaItem:
        if not isinstance(value, dict):
            raise TikTokError("TikTok returned a malformed media item")
        post_id = str(value.get("id") or expected_id or "")
        post_id = validate_post_id(post_id)
        if expected_id and post_id != str(expected_id):
            raise TikTokError(
                f"TikTok returned {post_id} while requesting {expected_id}"
            )
        if author is None:
            author = cls.identity_from_author(value.get("author"))

        image_post = cls._photo_metadata(
            value.get("imagePost"),
            value.get("imagePostInfo"),
            value.get("image_post_info"),
        )
        declared_photo = isinstance(image_post, dict)
        image_urls: list[tuple[str, ...]] = []
        image_count = 0
        title = ""
        if declared_photo:
            title = str(image_post.get("title") or "")
            images = image_post.get("images")
            direct_addresses = False
            if not isinstance(images, list) or not images:
                images = image_post.get("displayImages")
                direct_addresses = True
            if isinstance(images, list):
                image_count = len(images)
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    if direct_addresses:
                        address = image
                    else:
                        address = (
                            image.get("imageURL")
                            or image.get("imageUrl")
                            or image.get("displayImage")
                            or {}
                        )
                    urls = cls._address_urls(address)
                    if urls:
                        image_urls.append(tuple(urls))

            # TikTok sometimes declares an image post before exposing its
            # slide list. Keep it classified as a photo so the downloader
            # refreshes image metadata instead of inventing an MP4 job.
            if image_count == 0:
                image_count = 1

        video = value.get("video")
        video_urls = [] if declared_photo else cls.video_urls(
            video if isinstance(video, dict) else {}
        )
        description = str(
            value.get("desc")
            or value.get("text")
            or title
            or ("story" if is_story else f"TikTok post #{post_id}")
        )
        return MediaItem(
            post_id=post_id,
            description=description,
            video_urls=tuple(video_urls),
            image_urls=tuple(image_urls),
            image_count=image_count,
            author=author,
            is_story=is_story,
        )

    @staticmethod
    def _photo_metadata(*values) -> dict | None:
        candidates = [value for value in values if isinstance(value, dict)]
        if not candidates:
            return None

        def score(value: dict) -> int:
            for key in ("images", "displayImages"):
                slides = value.get(key)
                if isinstance(slides, list) and slides:
                    return 2
            return 1 if value else 0

        return max(candidates, key=score)

    @staticmethod
    def _address_urls(value) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return list(dict.fromkeys(
                str(url) for url in value if isinstance(url, str) and url
            ))
        if not isinstance(value, dict):
            return []
        urls = (
            value.get("urlList")
            or value.get("UrlList")
            or value.get("urls")
            or ()
        )
        if isinstance(urls, str):
            urls = [urls]
        return list(dict.fromkeys(
            str(url) for url in urls if isinstance(url, str) and url
        ))

    @classmethod
    def video_urls(cls, video: dict) -> list[str]:
        quality: dict[int, list[str]] = {}
        bitrate_info = video.get("bitrateInfo") or ()
        if isinstance(bitrate_info, dict):
            bitrate_info = (bitrate_info,)
        if isinstance(bitrate_info, (list, tuple)):
            for entry in bitrate_info:
                if not isinstance(entry, dict):
                    continue
                address = entry.get("PlayAddr") or entry.get("playAddr") or {}
                if not isinstance(address, dict):
                    continue
                try:
                    size = int(address.get("Width") or 0) * int(
                        address.get("Height") or 0
                    )
                except (TypeError, ValueError):
                    size = 0
                quality.setdefault(size, []).extend(cls._address_urls(address))

        urls: list[str] = []
        for size in sorted(quality, reverse=True):
            urls.extend(quality[size])
        urls.extend(cls._address_urls(video.get("playAddr") or {}))
        urls.extend(cls._address_urls(video.get("downloadAddr") or {}))
        urls.extend(cls._address_urls(video.get("urls") or {}))
        return list(dict.fromkeys(urls))

    def collect_posts(
        self,
        sec_uid: str,
        *,
        profile_url: str,
        recent: list[dict] | tuple = (),
        is_private: bool = False,
    ) -> list[MediaItem]:
        sec_uid = validate_sec_uid(sec_uid)
        posts = self._scan_posts(sec_uid, profile_url=profile_url)
        if (
            not posts
            and self.has_cookies
            and not is_private
        ):
            print(
                "Authenticated scan returned no posts; retrying without "
                "cookies..."
            )
            posts = self._scan_posts(
                sec_uid, profile_url=profile_url, anonymous=True
            )

        fallback: list[MediaItem] = []
        fallback_errors: list[str] = []
        if isinstance(recent, (list, tuple)):
            for value in recent:
                try:
                    fallback.append(self.normalize_item(value))
                except TikTokError as exc:
                    fallback_errors.append(str(exc))
        if fallback_errors:
            raise TikTokError(
                "TikTok's creator embed returned malformed post metadata: "
                + fallback_errors[0]
            )
        if not posts and fallback:
            print(
                "The post-list API stayed empty; using posts exposed by "
                "TikTok's creator embed."
            )

        # The creator embed can contain lightweight placeholders. Preserve
        # its recent-post ordering, but substitute the canonical profile API
        # item for duplicate IDs so photo posts cannot become fake MP4s.
        canonical = {item.post_id: item for item in posts}
        merged: list[MediaItem] = []
        seen: set[str] = set()
        for fallback_item in fallback:
            item = canonical.get(fallback_item.post_id)
            if item is None:
                item = self.media_from_embed(fallback_item.post_id)
            if item.post_id not in seen:
                seen.add(item.post_id)
                merged.append(item)
        for item in posts:
            if item.post_id not in seen:
                seen.add(item.post_id)
                merged.append(item)
        return merged

    def _scan_posts(
        self,
        sec_uid: str,
        *,
        profile_url: str,
        anonymous: bool = False,
    ) -> list[MediaItem]:
        cursor = int(time.time()) * 1000
        oldest_cursor = 1_472_706_000_000
        seen: set[str] = set()
        posts: list[MediaItem] = []
        page = 1
        empty_pages = 0

        while cursor >= oldest_cursor:
            params = self._profile_params(sec_uid, cursor)
            data = self._request_json_api(
                "/api/creator/item_list/",
                params,
                profile_url=profile_url,
                label="TikTok profile API",
                retry_empty=True,
                anonymous=anonymous,
            )
            raw_items = data.get("itemList", [])
            if raw_items is None:
                raw_items = []
            if not isinstance(raw_items, list):
                raise TikTokError("TikTok profile API returned invalid items")

            new_items = 0
            timestamps: list[int] = []
            malformed_errors: list[str] = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    malformed_errors.append("item is not a JSON object")
                    continue
                try:
                    item = self.normalize_item(raw_item)
                except TikTokError as exc:
                    malformed_errors.append(str(exc))
                    continue
                if (
                    item.author
                    and item.author.sec_uid
                    and item.author.sec_uid != sec_uid
                ):
                    raise TikTokError(
                        "TikTok profile API returned a post for another "
                        "creator"
                    )
                if item.post_id not in seen:
                    seen.add(item.post_id)
                    posts.append(item)
                    new_items += 1
                try:
                    timestamp = int(raw_item.get("createTime") or 0)
                except (TypeError, ValueError):
                    timestamp = 0
                if timestamp > 0:
                    timestamps.append(timestamp)

            if malformed_errors:
                raise TikTokError(
                    "TikTok profile API returned malformed post metadata: "
                    + malformed_errors[0]
                )

            print(
                f"Profile page {page}: {new_items} new "
                f"{'post' if new_items == 1 else 'posts'} "
                f"({len(posts)} total)"
            )
            page += 1
            empty_pages = empty_pages + 1 if new_items == 0 else 0

            if not data.get("hasMorePrevious"):
                break
            if empty_pages >= 3:
                print(
                    "Stopping pagination after 3 consecutive empty pages."
                )
                break

            next_cursor = min(timestamps) * 1000 if timestamps else 0
            if not next_cursor or next_cursor >= cursor:
                next_cursor = cursor - 7 * 86_400_000
            cursor = next_cursor
        return posts

    def _api_params(self, cursor: int) -> dict[str, str]:
        return {
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
            "tz_name": "UTC",
            "verifyFp": "verify_" + "".join(
                random.choices("0123456789abcdef", k=7)
            ),
            "webcast_language": "en",
        }

    def _profile_params(self, sec_uid: str, cursor: int) -> dict[str, str]:
        params = self._api_params(cursor)
        params.update({
            "count": "15",
            "secUid": sec_uid,
            "type": "1",
        })
        return params

    def _story_params(self, user_id: str, cursor: int) -> dict[str, str]:
        params = self._api_params(cursor)
        params.update({
            "authorId": validate_user_id(str(user_id)),
            "count": "5",
            "loadBackward": "false",
        })
        return params

    def _request_json_api(
        self,
        path: str,
        params: dict[str, str],
        *,
        profile_url: str,
        label: str,
        retry_empty: bool = False,
        anonymous: bool = False,
    ) -> dict:
        last_error: Exception | str = "unknown API error"
        for attempt in range(1, 5):
            try:
                with self.request(
                    ROOT + path + "?" + urlencode(params),
                    headers={"Referer": profile_url},
                    attempts=1,
                    anonymous=anonymous,
                ) as response:
                    payload = response.read().decode("utf-8", "replace")
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise TikTokError(f"{label} returned invalid JSON")
                status = data.get("statusCode", data.get("status_code", 0))
                if status:
                    raise TikTokError(
                        data.get("statusMsg")
                        or data.get("status_msg")
                        or f"{label} error {status}"
                    )
                if (
                    retry_empty
                    and not (data.get("itemList") or ())
                    and data.get("hasMorePrevious")
                    and attempt < 4
                ):
                    raise TikTokError("TikTok returned a transient empty page")
                return data
            except (OSError, ValueError, TikTokError) as exc:
                last_error = exc
                if attempt < 4:
                    delay = self._retry_delay(attempt)
                    print(
                        f"{label} failed; retrying in {delay:.1f}s "
                        f"({attempt}/4)"
                    )
                    time.sleep(delay)
        raise TikTokError(str(last_error))

    def story_items(
        self,
        user_id: str,
        *,
        profile_url: str,
    ) -> list[MediaItem]:
        """Read every active Story before the download queue starts."""
        user_id = validate_user_id(str(user_id))
        cursor = 0
        page = 1
        empty_pages = 0
        seen: set[str] = set()
        stories: list[MediaItem] = []

        while True:
            if page > 100:
                raise TikTokError(
                    "TikTok Story pagination exceeded 100 pages; refusing "
                    "a potentially looping partial scan"
                )
            data = self._request_json_api(
                "/api/story/item_list/",
                self._story_params(user_id, cursor),
                profile_url=profile_url,
                label="TikTok story API",
            )
            raw_items = data.get("itemList", [])
            if raw_items is None:
                raw_items = []
            if not isinstance(raw_items, list):
                raise TikTokError("TikTok story API returned invalid items")

            new_items = 0
            malformed_errors: list[str] = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    malformed_errors.append("item is not a JSON object")
                    continue
                try:
                    item = self.normalize_item(raw_item, is_story=True)
                except TikTokError as exc:
                    malformed_errors.append(str(exc))
                    continue
                if (
                    item.author
                    and item.author.user_id
                    and item.author.user_id != user_id
                ):
                    raise TikTokError(
                        f"TikTok returned story {item.post_id} for another "
                        "creator"
                    )
                if item.post_id not in seen:
                    seen.add(item.post_id)
                    stories.append(item)
                    new_items += 1

            if malformed_errors:
                raise TikTokError(
                    "TikTok story API returned malformed media metadata: "
                    + malformed_errors[0]
                )

            print(
                f"Story page {page}: {new_items} new "
                f"{'story' if new_items == 1 else 'stories'} "
                f"({len(stories)} total)"
            )
            page += 1

            has_more_value = data.get(
                "HasMoreAfter", data.get("hasMoreAfter", False)
            )
            if isinstance(has_more_value, bool):
                has_more = has_more_value
            elif isinstance(has_more_value, int) and has_more_value in (0, 1):
                has_more = bool(has_more_value)
            elif (
                isinstance(has_more_value, str)
                and has_more_value.lower() in {"0", "1", "false", "true"}
            ):
                has_more = has_more_value.lower() in {"1", "true"}
            else:
                raise TikTokError(
                    "TikTok Story pagination returned an invalid "
                    "HasMoreAfter value"
                )
            if not has_more:
                break
            empty_pages = empty_pages + 1 if new_items == 0 else 0
            if empty_pages >= 3:
                raise TikTokError(
                    "TikTok Story pagination returned 3 pages without new "
                    "media; refusing a partial or looping scan"
                )
            try:
                next_cursor = int(
                    data.get("MaxCursor", data.get("maxCursor", 0)) or 0
                )
            except (TypeError, ValueError):
                next_cursor = 0
            if next_cursor <= cursor:
                raise TikTokError(
                    "TikTok Story pagination returned a missing or "
                    "non-advancing cursor; refusing a partial Story scan"
                )
            cursor = next_cursor

        return stories

    def story_ids(self, user_id: str, *, profile_url: str) -> list[str]:
        return [
            item.post_id
            for item in self.story_items(user_id, profile_url=profile_url)
        ]

    def active_story_identity(
        self,
        user_id: str,
        *,
        profile_url: str,
    ) -> ProfileIdentity | None:
        for item in self.story_items(user_id, profile_url=profile_url):
            if item.author:
                return item.author
        return None

    def collect_stories(
        self,
        identity: ProfileIdentity,
        *,
        profile_url: str,
    ) -> list[MediaItem]:
        stories = self.story_items(
            identity.user_id, profile_url=profile_url
        )
        for item in stories:
            if item.author:
                if (
                    identity.user_id
                    and item.author.user_id
                    and identity.user_id != item.author.user_id
                ) or (
                    identity.sec_uid
                    and item.author.sec_uid
                    and identity.sec_uid != item.author.sec_uid
                    ):
                    raise TikTokError(
                        f"TikTok returned story {item.post_id} for another "
                        "creator"
                    )
        return stories

    def authenticated_profile_identity(
        self,
        username: str,
    ) -> ProfileIdentity | None:
        """Extract an exact, structured identity from signed-in page JSON."""
        username = validate_username(username)
        with self.request(f"{ROOT}/@{username}") as response:
            page = response.read().decode("utf-8", "replace")
        scripts = re.findall(
            r"<script[^>]*>(.*?)</script>",
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for source in scripts:
            source = html.unescape(source.strip())
            if not source or source[0] not in "[{":
                continue
            try:
                value = json.loads(source)
            except ValueError:
                continue
            identity = self._find_exact_identity(value, username)
            if identity:
                return identity
        return None

    @classmethod
    def _find_exact_identity(
        cls,
        value,
        username: str,
    ) -> ProfileIdentity | None:
        if isinstance(value, dict):
            unique_id = value.get("uniqueId") or value.get("unique_id")
            if str(unique_id or "").lower() == username.lower():
                identity = cls.identity_from_author(value)
                if identity and (identity.sec_uid or identity.user_id):
                    return identity
            for child in value.values():
                found = cls._find_exact_identity(child, username)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_exact_identity(child, username)
                if found:
                    return found
        return None

    def _retry_delay(self, attempt: int) -> float:
        return max(self.settings.sleep, min(3 * attempt, 12)) + random.uniform(
            0.0, 1.0
        )
