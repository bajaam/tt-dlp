import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp.client import TikTokClient
from tt_dlp.errors import TikTokError
from tt_dlp.models import MediaItem, ProfileIdentity, Settings


SEC_UID = "MS4wLjABAAAAabcdefghijklmnop"
USER_ID = "123456789"


def settings(*, cookies=None) -> Settings:
    return Settings(
        output=Path("downloads"),
        cookies=cookies,
        profile_store=None,
        limit=0,
        sleep=0.0,
        overwrite=False,
        dry_run=False,
        stories=False,
        identify=False,
    )


class ClientNormalizationTests(unittest.TestCase):
    def test_photo_address_variants_are_normalized(self):
        item = TikTokClient.normalize_item({
            "id": "123456",
            "desc": "photos",
            "imagePost": {
                "images": [
                    {"imageURL": "https://cdn.example/one.jpg"},
                    {"imageUrl": {"UrlList": [
                        "https://cdn.example/two.webp",
                        "https://cdn.example/two.webp",
                    ]}},
                    {"displayImage": {"urlList": []}},
                ]
            },
        })

        self.assertEqual(item.image_urls, (
            ("https://cdn.example/one.jpg",),
            ("https://cdn.example/two.webp",),
        ))
        self.assertTrue(item.is_photo)

    def test_embed_display_images_are_normalized(self):
        item = TikTokClient.normalize_item({
            "id": "123456",
            "text": "embed carousel",
            "imagePostInfo": {
                "displayImages": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "urlList": ["https://cdn.example/one.jpeg"],
                    },
                    {
                        "width": 1080,
                        "height": 1920,
                        "urlList": ["https://cdn.example/two.jpeg"],
                    },
                ],
            },
        })

        self.assertEqual(item.image_urls, (
            ("https://cdn.example/one.jpeg",),
            ("https://cdn.example/two.jpeg",),
        ))
        self.assertEqual(item.image_count, 2)

    def test_media_from_embed_reads_sibling_display_images(self):
        client = TikTokClient(settings())
        requested = []

        def embed_data(path):
            requested.append(path)
            return {
                "videoData": {
                    "itemInfos": {
                        "id": "123456",
                        "text": "embed carousel",
                        "video": {},
                    },
                    "imagePostInfo": {
                        "displayImages": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "urlList": ["https://cdn.example/one.jpeg"],
                            },
                            {
                                "width": 1080,
                                "height": 1920,
                                "urlList": ["https://cdn.example/two.jpeg"],
                            },
                        ],
                    },
                    "authorInfos": {
                        "uniqueId": "example",
                        "id": USER_ID,
                        "secUid": SEC_UID,
                    },
                },
            }

        client.embed_data = embed_data
        item = client.media_from_embed("123456")

        self.assertEqual(requested, ["/embed/v2/123456"])
        self.assertEqual(item.image_urls, (
            ("https://cdn.example/one.jpeg",),
            ("https://cdn.example/two.jpeg",),
        ))
        self.assertEqual(item.image_count, 2)
        self.assertEqual(item.author.username, "example")

    def test_video_urls_are_quality_ordered_and_deduplicated(self):
        urls = TikTokClient.video_urls({
            "bitrateInfo": [
                {"PlayAddr": {
                    "Width": 640,
                    "Height": 360,
                    "UrlList": ["https://cdn.example/low.mp4"],
                }},
                {"playAddr": {
                    "Width": 1920,
                    "Height": 1080,
                    "urlList": ["https://cdn.example/high.mp4"],
                }},
            ],
            "playAddr": {"urlList": ["https://cdn.example/high.mp4"]},
            "downloadAddr": "https://cdn.example/download.mp4",
        })

        self.assertEqual(urls, [
            "https://cdn.example/high.mp4",
            "https://cdn.example/low.mp4",
            "https://cdn.example/download.mp4",
        ])

    def test_malformed_items_and_mismatched_ids_are_rejected(self):
        for value in (None, [], {"id": "../escape"}, {"id": "1234"}):
            with self.subTest(value=value):
                with self.assertRaises(TikTokError):
                    TikTokClient.normalize_item(value)

        with self.assertRaisesRegex(TikTokError, "while requesting"):
            TikTokClient.normalize_item(
                {"id": "123456"}, expected_id="999999"
            )

    def test_identity_parser_requires_a_valid_username(self):
        valid = TikTokClient.identity_from_author({
            "uniqueId": "example",
            "id": USER_ID,
            "secUid": SEC_UID,
        })
        invalid = TikTokClient.identity_from_author({
            "uniqueId": "../outside",
            "id": USER_ID,
            "secUid": SEC_UID,
        })

        self.assertEqual(valid, ProfileIdentity(
            username="example", user_id=USER_ID, sec_uid=SEC_UID
        ))
        self.assertIsNone(invalid)


class ClientPaginationTests(unittest.TestCase):
    def test_foreign_author_in_profile_page_is_rejected(self):
        client = TikTokClient(settings())
        client._request_json_api = lambda *args, **kwargs: {
            "itemList": [{
                "id": "123456",
                "author": {
                    "uniqueId": "other",
                    "id": "987654321",
                    "secUid": "MS4wLjABAAAAzyxwvutsrqponmlk",
                },
            }],
            "hasMorePrevious": False,
        }

        with self.assertRaisesRegex(TikTokError, "another creator"):
            client._scan_posts(
                SEC_UID, profile_url="https://www.tiktok.com/@example"
            )

    def test_nonempty_malformed_page_is_not_reported_as_zero_posts(self):
        client = TikTokClient(settings())
        client._request_json_api = lambda *args, **kwargs: {
            "itemList": [{"unexpected": "schema"}],
            "hasMorePrevious": False,
        }

        with self.assertRaisesRegex(TikTokError, "malformed post metadata"):
            client._scan_posts(
                SEC_UID, profile_url="https://www.tiktok.com/@example"
            )

    def test_three_consecutive_empty_pages_stop_pagination(self):
        client = TikTokClient(settings())
        calls = []

        def empty_page(path, params, **kwargs):
            calls.append((path, int(params["cursor"]), kwargs))
            return {"itemList": [], "hasMorePrevious": True}

        client._request_json_api = empty_page
        output = io.StringIO()
        with redirect_stdout(output):
            posts = client._scan_posts(
                SEC_UID, profile_url="https://www.tiktok.com/@example"
            )

        self.assertEqual(posts, [])
        self.assertEqual(len(calls), 3)
        self.assertIn("Stopping pagination", output.getvalue())
        self.assertGreater(calls[0][1], calls[1][1])
        self.assertGreater(calls[1][1], calls[2][1])

    def test_oldest_timestamp_is_used_instead_of_list_order(self):
        client = TikTokClient(settings())
        cursors = []
        responses = iter((
            {
                "itemList": [
                    {"id": "123456", "createTime": "1600000000"},
                    {"id": "123457", "createTime": "1700000000"},
                ],
                "hasMorePrevious": True,
            },
            {"itemList": [], "hasMorePrevious": False},
        ))

        def page(path, params, **kwargs):
            del path, kwargs
            cursors.append(int(params["cursor"]))
            return next(responses)

        client._request_json_api = page
        posts = client._scan_posts(
            SEC_UID, profile_url="https://www.tiktok.com/@example"
        )

        self.assertEqual([item.post_id for item in posts], ["123456", "123457"])
        self.assertEqual(cursors[1], 1_600_000_000_000)

    def test_authenticated_empty_scan_falls_back_to_anonymous(self):
        client = TikTokClient(settings())
        client.settings = settings(cookies=Path("configured-cookies.txt"))
        calls = []

        def scan(sec_uid, *, profile_url, anonymous=False):
            calls.append((sec_uid, profile_url, anonymous))
            if anonymous:
                return [MediaItem(post_id="123456", description="post")]
            return []

        client._scan_posts = scan
        posts = client.collect_posts(
            SEC_UID,
            profile_url="https://www.tiktok.com/@example",
            recent=[{"id": "999999"}],
            is_private=False,
        )

        self.assertEqual([item.post_id for item in posts], ["999999", "123456"])
        self.assertEqual([call[2] for call in calls], [False, True])


class ClientSafetyTests(unittest.TestCase):
    def test_failed_http_error_response_is_closed(self):
        client = TikTokClient(settings())
        body = io.BytesIO(b"Forbidden")
        error = HTTPError(
            "https://www.tiktok.com/test", 403, "Forbidden", {}, body
        )

        class FailingOpener:
            @staticmethod
            def open(request, timeout):
                del request, timeout
                raise error

        client.opener = FailingOpener()
        with self.assertRaisesRegex(TikTokError, "HTTP 403"):
            client.request("https://www.tiktok.com/test", attempts=1)

        self.assertTrue(body.closed)

    def test_story_for_a_different_creator_is_rejected(self):
        client = TikTokClient(settings())
        expected = ProfileIdentity(
            username="expected", user_id=USER_ID, sec_uid=SEC_UID
        )
        other = ProfileIdentity(
            username="other",
            user_id="987654321",
            sec_uid="MS4wLjABAAAAzyxwvutsrqponmlk",
        )
        client.story_ids = lambda user_id, **kwargs: ["555555"]
        client.media_from_embed = lambda post_id, **kwargs: MediaItem(
            post_id=post_id,
            description="story",
            video_urls=("https://cdn.example/story.mp4",),
            author=other,
            is_story=True,
        )

        with self.assertRaisesRegex(TikTokError, "another creator"):
            client.collect_stories(
                expected, profile_url="https://www.tiktok.com/@expected"
            )


if __name__ == "__main__":
    unittest.main()
