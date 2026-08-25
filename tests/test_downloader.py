import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp.downloader import TikTokDownloader
from tt_dlp.errors import TikTokError
from tt_dlp.models import MediaItem, ProfileIdentity, Settings
from tt_dlp.state import ProfileStore
from tt_dlp.targets import parse_target


SEC_UID = "MS4wLjABAAAAabcdefghijklmnop"
OTHER_SEC_UID = "MS4wLjABAAAAzyxwvutsrqponmlk"
USER_ID = "123456789"
REFERER = "https://www.tiktok.com/@example"


def settings(
    output: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    identify: bool = False,
    stories: bool = False,
) -> Settings:
    return Settings(
        output=output,
        cookies=None,
        profile_store=None,
        limit=0,
        sleep=0.0,
        overwrite=overwrite,
        dry_run=dry_run,
        stories=stories,
        identify=identify,
    )


class NoNetworkClient:
    has_cookies = False

    def request(self, *args, **kwargs):
        raise AssertionError("unexpected network request")


class BytesResponse:
    def __init__(self, body: bytes, content_type: str):
        self._body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._body.close()


class ResponseClient(NoNetworkClient):
    def __init__(self, body: bytes, content_type: str):
        self.body = body
        self.content_type = content_type

    def request(self, *args, **kwargs):
        return BytesResponse(self.body, self.content_type)


class DownloadArchiveTests(unittest.TestCase):
    def test_existing_video_id_skips_when_description_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "profile"
            output.mkdir()
            existing = output / "123456 old description.mp4"
            existing.write_bytes(b"existing")
            downloader = TikTokDownloader(
                settings(Path(directory)), client=NoNetworkClient()
            )

            summary = downloader._download_item(
                MediaItem(
                    post_id="123456",
                    description="completely new description",
                    video_urls=("https://cdn.example/new.mp4",),
                ),
                output=output,
                referer=REFERER,
            )

            self.assertEqual(summary.skipped, 1)
            self.assertEqual(summary.downloaded, 0)
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertEqual(len(list(output.iterdir())), 1)

    def test_existing_photo_index_is_matched_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "profile"
            output.mkdir()
            (output / "123456_01 old.jpg").write_bytes(b"first")
            downloader = TikTokDownloader(
                settings(Path(directory), dry_run=True),
                client=NoNetworkClient(),
            )

            summary = downloader._download_item(
                MediaItem(
                    post_id="123456",
                    description="new",
                    image_urls=(
                        ("https://cdn.example/one.jpg",),
                        ("https://cdn.example/two.jpg",),
                    ),
                ),
                output=output,
                referer=REFERER,
            )

            self.assertEqual(summary.skipped, 1)
            self.assertEqual(summary.planned, 1)

    def test_dry_run_is_planned_and_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing-profile"
            downloader = TikTokDownloader(
                settings(Path(directory), dry_run=True),
                client=NoNetworkClient(),
            )

            summary = downloader._download_item(
                MediaItem(
                    post_id="123456",
                    description="preview",
                    video_urls=("https://cdn.example/video.mp4",),
                ),
                output=output,
                referer=REFERER,
            )

            self.assertEqual(summary.planned, 1)
            self.assertEqual(summary.skipped, 0)
            self.assertFalse(output.exists())


class MediaResponseTests(unittest.TestCase):
    def test_unknown_media_refreshes_before_choosing_photo_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class ResolvingClient(NoNetworkClient):
                def media_from_embed(self, post_id, *, is_story=False):
                    return MediaItem(
                        post_id=post_id,
                        description="resolved photo",
                        image_urls=(("https://cdn.example/photo.jpg",),),
                        image_count=1,
                        is_story=is_story,
                    )

            downloader = TikTokDownloader(
                settings(root, dry_run=True), client=ResolvingClient()
            )
            output = io.StringIO()

            with redirect_stdout(output):
                summary = downloader._download_item(
                    MediaItem(post_id="123456", description="placeholder"),
                    output=root,
                    referer=REFERER,
                )

            self.assertEqual(summary.planned, 1)
            self.assertIn("123456_01 resolved photo.jpg", output.getvalue())
            self.assertNotIn(".mp4", output.getvalue())

    def test_unknown_media_fails_once_without_inventing_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class EmptyMetadataClient(NoNetworkClient):
                def media_from_embed(self, post_id, *, is_story=False):
                    return MediaItem(
                        post_id=post_id,
                        description="still empty",
                        is_story=is_story,
                    )

            downloader = TikTokDownloader(
                settings(root), client=EmptyMetadataClient()
            )

            with self.assertRaisesRegex(TikTokError, "no downloadable"):
                downloader._download_item(
                    MediaItem(post_id="123456", description="placeholder"),
                    output=root,
                    referer=REFERER,
                )

            self.assertEqual(list(root.iterdir()), [])

    def test_html_content_type_is_rejected_without_leaving_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "video.mp4"
            temporary = root / "video.mp4.part"
            downloader = TikTokDownloader(
                settings(root),
                client=ResponseClient(b"<html>challenge</html>", "text/html"),
            )

            with self.assertRaisesRegex(TikTokError, "instead of media"):
                downloader._write_media(
                    "https://cdn.example/video",
                    temporary,
                    destination,
                    referer=REFERER,
                )

            self.assertFalse(destination.exists())
            self.assertFalse(temporary.exists())

    def test_disguised_html_is_rejected_by_body_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = TikTokDownloader(
                settings(root),
                client=ResponseClient(
                    b"  <!doctype html><html>blocked</html>",
                    "application/octet-stream",
                ),
            )

            with self.assertRaisesRegex(TikTokError, "instead of media"):
                downloader._write_media(
                    "https://cdn.example/video",
                    root / "video.part",
                    root / "video.mp4",
                    referer=REFERER,
                )

    def test_valid_binary_response_is_atomically_moved_into_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "video.mp4"
            temporary = root / "video.mp4.part"
            payload = b"\x00\x00\x00\x18ftypmp42" + b"media bytes"
            downloader = TikTokDownloader(
                settings(root),
                client=ResponseClient(payload, "video/mp4"),
            )

            downloader._write_media(
                "https://cdn.example/video",
                temporary,
                destination,
                referer=REFERER,
            )

            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(temporary.exists())

    def test_failed_url_round_refreshes_same_item_before_succeeding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class RefreshingClient(NoNetworkClient):
                def __init__(self):
                    self.refreshes = []

                def media_from_embed(self, post_id, *, is_story=False):
                    self.refreshes.append((post_id, is_story))
                    return MediaItem(
                        post_id=post_id,
                        description="refreshed",
                        video_urls=("https://cdn.example/fresh.mp4",),
                        is_story=is_story,
                    )

            client = RefreshingClient()
            downloader = TikTokDownloader(settings(root), client=client)
            attempted = []

            def write(url, temporary, destination, *, referer):
                self.assertEqual(referer, REFERER)
                attempted.append(url)
                if "expired" in url:
                    raise TikTokError("expired")
                destination.write_bytes(b"ok")

            downloader._write_media = write
            destination = root / "123456 post.mp4"
            item = MediaItem(
                post_id="123456",
                description="post",
                video_urls=("https://cdn.example/expired.mp4",),
            )

            with patch("tt_dlp.downloader.time.sleep"):
                downloader._download_with_refresh(
                    item,
                    destination=destination,
                    index=None,
                    referer=REFERER,
                )

            self.assertEqual(attempted, [
                "https://cdn.example/expired.mp4",
                "https://cdn.example/fresh.mp4",
            ])
            self.assertEqual(client.refreshes, [("123456", False)])
            self.assertEqual(destination.read_bytes(), b"ok")

    def test_failed_mp4_refresh_reclassifies_carousel_as_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class PhotoRefreshClient(NoNetworkClient):
                def media_from_embed(self, post_id, *, is_story=False):
                    return MediaItem(
                        post_id=post_id,
                        description="refreshed carousel",
                        image_urls=(("https://cdn.example/photo.jpg",),),
                        image_count=1,
                        is_story=is_story,
                    )

            downloader = TikTokDownloader(
                settings(root), client=PhotoRefreshClient()
            )
            attempted = []

            def write(url, temporary, destination, *, referer):
                del temporary, referer
                attempted.append((url, destination.name))
                if url.endswith("expired.mp4"):
                    raise TikTokError("expired")
                destination.write_bytes(b"photo")

            downloader._write_media = write
            summary = downloader._download_item(
                MediaItem(
                    post_id="123456",
                    description="wrong placeholder",
                    video_urls=("https://cdn.example/expired.mp4",),
                ),
                output=root,
                referer=REFERER,
            )

            self.assertEqual(summary.downloaded, 1)
            self.assertEqual(attempted, [
                (
                    "https://cdn.example/expired.mp4",
                    "123456 wrong placeholder.mp4",
                ),
                (
                    "https://cdn.example/photo.jpg",
                    "123456_01 refreshed carousel.jpg",
                ),
            ])
            self.assertFalse((root / "123456 wrong placeholder.mp4").exists())
            self.assertEqual(
                (root / "123456_01 refreshed carousel.jpg").read_bytes(),
                b"photo",
            )

    def test_incomplete_photo_refresh_keeps_original_slide_count(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = MediaItem(
                post_id="123456",
                description="partial refresh",
                image_urls=(
                    ("https://cdn.example/one.jpg",),
                    ("https://cdn.example/two.jpg",),
                ),
                image_count=2,
            )
            complete = MediaItem(
                post_id="123456",
                description="complete refresh",
                image_urls=(
                    ("https://cdn.example/one.jpg",),
                    ("https://cdn.example/two.jpg",),
                    ("https://cdn.example/three.jpg",),
                ),
                image_count=3,
            )

            class RefreshingPhotoClient(NoNetworkClient):
                def __init__(self):
                    self.responses = iter((partial, complete))
                    self.refreshes = 0

                def media_from_embed(self, post_id, *, is_story=False):
                    self.assert_post(post_id, is_story)
                    self.refreshes += 1
                    return next(self.responses)

                @staticmethod
                def assert_post(post_id, is_story):
                    if post_id != "123456" or is_story:
                        raise AssertionError("unexpected refresh target")

            client = RefreshingPhotoClient()
            downloader = TikTokDownloader(
                settings(Path(directory)), client=client
            )
            initial = MediaItem(
                post_id="123456",
                description="original metadata",
                image_urls=(("https://cdn.example/one.jpg",),),
                image_count=3,
            )

            with patch("tt_dlp.downloader.time.sleep") as sleep:
                result = downloader._refresh_incomplete_photo(initial)

            self.assertEqual(result, complete)
            self.assertEqual(client.refreshes, 2)
            sleep.assert_called_once()

    def test_incomplete_photo_refresh_tracks_larger_discovered_count(self):
        with tempfile.TemporaryDirectory() as directory:
            expanded_partial = MediaItem(
                post_id="123456",
                description="expanded partial refresh",
                image_urls=(
                    ("https://cdn.example/one.jpg",),
                    ("https://cdn.example/two.jpg",),
                    ("https://cdn.example/three.jpg",),
                ),
                image_count=4,
            )
            expanded_complete = MediaItem(
                post_id="123456",
                description="expanded complete refresh",
                image_urls=(
                    ("https://cdn.example/one.jpg",),
                    ("https://cdn.example/two.jpg",),
                    ("https://cdn.example/three.jpg",),
                    ("https://cdn.example/four.jpg",),
                ),
                image_count=4,
            )

            class ExpandingPhotoClient(NoNetworkClient):
                def __init__(self):
                    self.responses = iter((
                        expanded_partial,
                        expanded_complete,
                    ))
                    self.refreshes = 0

                def media_from_embed(self, post_id, *, is_story=False):
                    if post_id != "123456" or is_story:
                        raise AssertionError("unexpected refresh target")
                    self.refreshes += 1
                    return next(self.responses)

            client = ExpandingPhotoClient()
            downloader = TikTokDownloader(
                settings(Path(directory)), client=client
            )
            initial = MediaItem(
                post_id="123456",
                description="original metadata",
                image_urls=(
                    ("https://cdn.example/one.jpg",),
                    ("https://cdn.example/two.jpg",),
                ),
                image_count=3,
            )

            with patch("tt_dlp.downloader.time.sleep") as sleep:
                result = downloader._refresh_incomplete_photo(initial)

            self.assertEqual(result, expanded_complete)
            self.assertEqual(client.refreshes, 2)
            sleep.assert_called_once()


class DownloaderIdentityTests(unittest.TestCase):
    def test_direct_story_skips_profile_scans_and_needs_no_cookie_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class DirectStoryClient(NoNetworkClient):
                def __init__(self):
                    self.embed_calls = []

                def media_from_embed(self, post_id, *, is_story=False):
                    self.embed_calls.append((post_id, is_story))
                    return MediaItem(
                        post_id=post_id,
                        description="active story",
                        video_urls=("https://cdn.example/story.mp4",),
                        author=ProfileIdentity(
                            username="example",
                            user_id=USER_ID,
                            sec_uid=SEC_UID,
                        ),
                        is_story=is_story,
                    )

                def creator_data(self, username):
                    return {"userInfo": {
                        "uniqueId": username,
                        "id": USER_ID,
                        "secUid": SEC_UID,
                        "privateAccount": False,
                    }, "videoList": []}

                @staticmethod
                def identity_from_creator(creator):
                    user = creator["userInfo"]
                    return ProfileIdentity(
                        username=user["uniqueId"],
                        user_id=user["id"],
                        sec_uid=user["secUid"],
                    )

                def collect_posts(self, *args, **kwargs):
                    raise AssertionError("direct story scanned regular posts")

                def collect_stories(self, *args, **kwargs):
                    raise AssertionError("direct story enumerated the profile")

            client = DirectStoryClient()
            downloader = TikTokDownloader(
                settings(root, dry_run=True, stories=True),
                client=client,
                profile_store=ProfileStore(None),
            )

            result = downloader.run(
                "https://www.tiktok.com/@example/story/555555"
            )

            self.assertEqual(result, 0)
            self.assertEqual(client.embed_calls, [("555555", True)])

    def test_fresh_complete_stable_target_can_have_no_current_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            class EmptyClient(NoNetworkClient):
                def collect_posts(self, sec_uid, **kwargs):
                    return []

            store = ProfileStore(None)
            downloader = TikTokDownloader(
                settings(Path(directory), identify=True),
                client=EmptyClient(),
                profile_store=store,
            )

            self.assertEqual(
                downloader.run(f"ttid:{USER_ID}:{SEC_UID}"), 0
            )
            self.assertIsNone(store.find(parse_target(
                f"ttid:{USER_ID}:{SEC_UID}"
            )))

    def test_composite_target_rejects_conflicting_post_author(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class ConflictClient(NoNetworkClient):
                def collect_posts(self, sec_uid, **kwargs):
                    return [MediaItem(
                        post_id="555555",
                        description="post",
                        video_urls=("https://cdn.example/video.mp4",),
                        author=ProfileIdentity(
                            username="actual",
                            user_id="987654321",
                            sec_uid=OTHER_SEC_UID,
                        ),
                    )]

            downloader = TikTokDownloader(
                settings(root, identify=True),
                client=ConflictClient(),
                profile_store=ProfileStore(None),
            )

            with self.assertRaisesRegex(TikTokError, "conflicting stable"):
                downloader.run(f"ttid:{USER_ID}:{SEC_UID}")

    def test_saved_numeric_id_resolves_to_cached_sec_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProfileStore(root / "profiles.json")
            store.update(ProfileIdentity(
                username="example",
                user_id=USER_ID,
                sec_uid=SEC_UID,
            ))

            class CachedClient(NoNetworkClient):
                def creator_data(self, username):
                    return {"userInfo": {
                        "uniqueId": username,
                        "id": USER_ID,
                        "secUid": SEC_UID,
                        "privateAccount": False,
                    }, "videoList": []}

                @staticmethod
                def identity_from_creator(creator):
                    user = creator["userInfo"]
                    return ProfileIdentity(
                        username=user["uniqueId"],
                        user_id=user["id"],
                        sec_uid=user["secUid"],
                    )

                def collect_posts(self, sec_uid, **kwargs):
                    self.collected = (sec_uid, kwargs)
                    return []

            client = CachedClient()
            downloader = TikTokDownloader(
                settings(root, identify=True),
                client=client,
                profile_store=store,
            )

            result = downloader.run(f"userid:{USER_ID}")

            self.assertEqual(result, 0)
            self.assertEqual(client.collected[0], SEC_UID)
            self.assertEqual(
                store.find(parse_target(f"userid:{USER_ID}")).directory,
                "example",
            )

    def test_unknown_numeric_id_explains_that_sec_uid_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            downloader = TikTokDownloader(
                settings(Path(directory), identify=True),
                client=NoNetworkClient(),
                profile_store=ProfileStore(None),
            )

            with self.assertRaisesRegex(TikTokError, "numeric user ID alone"):
                downloader.run(f"userid:{USER_ID}")

    def test_direct_media_uses_embed_author_not_stale_url_username(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProfileStore(root / "profiles.json")
            stale = ProfileIdentity(
                username="old_name",
                user_id="987654321",
                sec_uid=OTHER_SEC_UID,
            )
            store.update(stale)

            class DirectClient(NoNetworkClient):
                def media_from_embed(self, post_id, *, is_story=False):
                    return MediaItem(
                        post_id=post_id,
                        description="post",
                        video_urls=("https://cdn.example/video.mp4",),
                        author=ProfileIdentity(
                            username="current_name",
                            user_id=USER_ID,
                            sec_uid=SEC_UID,
                        ),
                    )

                def creator_data(self, username):
                    raise AssertionError(
                        f"complete direct identity looked up {username}"
                    )

                @staticmethod
                def identity_from_creator(creator):
                    user = creator["userInfo"]
                    return ProfileIdentity(
                        username=user["uniqueId"],
                        user_id=user["id"],
                        sec_uid=user["secUid"],
                    )

                def collect_posts(self, sec_uid, **kwargs):
                    raise AssertionError(
                        f"direct media scanned profile {sec_uid}"
                    )

            client = DirectClient()
            downloader = TikTokDownloader(
                settings(root, identify=True),
                client=client,
                profile_store=store,
            )

            result = downloader.run(
                "https://www.tiktok.com/@old_name/video/555555"
            )

            self.assertEqual(result, 0)
            current = store.find(parse_target(f"secuid:{SEC_UID}"))
            self.assertEqual(current.username, "current_name")
            old = store.find(parse_target("old_name"))
            self.assertEqual(old.user_id, stale.user_id)
            self.assertEqual(old.sec_uid, stale.sec_uid)

    def test_profile_output_is_contained_under_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            downloader = TikTokDownloader(
                settings(root), client=NoNetworkClient()
            )

            output = downloader._profile_output(ProfileIdentity(
                username="example",
                directory="..\\outside/child",
            ))

            self.assertTrue(output.is_relative_to(root.resolve()))
            self.assertEqual(output.parent, root.resolve())


if __name__ == "__main__":
    unittest.main()
