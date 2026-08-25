import io
import sys
import unittest
from contextlib import redirect_stdout
from http.cookiejar import Cookie
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
            "video": {
                "playAddr": {
                    "urlList": ["https://cdn.example/not-a-carousel.mp4"],
                },
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
        })

        self.assertEqual(item.image_urls, (
            ("https://cdn.example/one.jpeg",),
            ("https://cdn.example/two.jpeg",),
        ))
        self.assertEqual(item.image_count, 2)
        self.assertEqual(item.video_urls, ())

    def test_empty_declared_image_post_never_becomes_mp4(self):
        item = TikTokClient.normalize_item({
            "id": "123456",
            "imagePost": {},
            "video": {
                "playAddr": {
                    "urlList": ["https://cdn.example/fake.mp4"],
                },
            },
        })

        self.assertTrue(item.is_photo)
        self.assertEqual(item.image_count, 1)
        self.assertEqual(item.image_urls, ())
        self.assertEqual(item.video_urls, ())

    def test_populated_photo_container_wins_over_empty_alias(self):
        item = TikTokClient.normalize_item({
            "id": "123456",
            "imagePost": {"title": "sparse alias"},
            "imagePostInfo": {
                "displayImages": [{
                    "urlList": ["https://cdn.example/photo.jpg"],
                }],
            },
        })

        self.assertEqual(item.image_urls, (
            ("https://cdn.example/photo.jpg",),
        ))
        self.assertEqual(item.image_count, 1)

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
    def test_profile_api_photo_replaces_duplicate_embed_placeholder(self):
        client = TikTokClient(settings())
        canonical = MediaItem(
            post_id="123456",
            description="canonical photo",
            image_urls=(("https://cdn.example/photo.jpg",),),
            image_count=1,
        )
        client._scan_posts = lambda *args, **kwargs: [canonical]

        def unexpected_embed(post_id, **kwargs):
            raise AssertionError(f"unexpected embed refresh for {post_id}")

        client.media_from_embed = unexpected_embed
        posts = client.collect_posts(
            SEC_UID,
            profile_url="https://www.tiktok.com/@example",
            recent=[{
                "id": "123456",
                "desc": "placeholder",
                "video": {
                    "playAddr": {
                        "urlList": ["https://cdn.example/fake.mp4"],
                    },
                },
            }],
        )

        self.assertEqual(posts, [canonical])
        self.assertTrue(posts[0].is_photo)

    def test_fallback_only_post_is_resolved_before_download(self):
        client = TikTokClient(settings())
        resolved = MediaItem(
            post_id="123456",
            description="resolved photo",
            image_urls=(("https://cdn.example/photo.jpg",),),
            image_count=1,
        )
        client._scan_posts = lambda *args, **kwargs: []
        refreshed = []

        def media_from_embed(post_id, **kwargs):
            refreshed.append((post_id, kwargs))
            return resolved

        client.media_from_embed = media_from_embed
        posts = client.collect_posts(
            SEC_UID,
            profile_url="https://www.tiktok.com/@example",
            recent=[{"id": "123456", "video": {}}],
        )

        self.assertEqual(posts, [resolved])
        self.assertEqual(refreshed, [("123456", {})])

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
        class AuthenticatedClient(TikTokClient):
            @property
            def has_cookies(self):
                return True

        client = AuthenticatedClient(settings())
        calls = []
        client.media_from_embed = lambda post_id, **kwargs: MediaItem(
            post_id=post_id,
            description="resolved fallback",
            video_urls=("https://cdn.example/resolved.mp4",),
        )

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


class ClientStoryTests(unittest.TestCase):
    def test_current_story_endpoint_is_paginated_and_normalized(self):
        client = TikTokClient(settings())
        calls = []
        responses = iter((
            {
                "itemList": [{
                    "id": "123456",
                    "desc": "video story",
                    "video": {
                        "playAddr": {
                            "urlList": ["https://cdn.example/story.mp4"],
                        },
                    },
                    "author": {
                        "uniqueId": "example",
                        "id": USER_ID,
                        "secUid": SEC_UID,
                    },
                }],
                "HasMoreAfter": True,
                "MaxCursor": "100",
            },
            {
                "itemList": [{
                    "id": "123457",
                    "desc": "photo story",
                    "video": {
                        "playAddr": {
                            "urlList": ["https://cdn.example/fake.mp4"],
                        },
                    },
                    "imagePost": {
                        "images": [{
                            "imageURL": {
                                "urlList": [
                                    "https://cdn.example/story-photo.jpg",
                                ],
                            },
                        }],
                    },
                    "author": {
                        "uniqueId": "example",
                        "id": USER_ID,
                        "secUid": SEC_UID,
                    },
                }],
                "HasMoreAfter": False,
                "MaxCursor": "200",
            },
        ))

        def story_page(path, params, **kwargs):
            calls.append((path, dict(params), kwargs))
            return next(responses)

        client._request_json_api = story_page
        stories = client.story_items(
            USER_ID, profile_url="https://www.tiktok.com/@example"
        )

        self.assertEqual([item.post_id for item in stories], [
            "123456", "123457",
        ])
        self.assertTrue(all(item.is_story for item in stories))
        self.assertFalse(stories[0].is_photo)
        self.assertTrue(stories[1].is_photo)
        self.assertEqual(stories[1].video_urls, ())
        self.assertEqual([call[0] for call in calls], [
            "/api/story/item_list/", "/api/story/item_list/",
        ])
        self.assertEqual([call[1]["cursor"] for call in calls], ["0", "100"])
        self.assertEqual(calls[0][1]["authorId"], USER_ID)
        self.assertEqual(calls[0][1]["loadBackward"], "false")
        self.assertEqual(calls[0][1]["count"], "5")

    def test_nonadvancing_story_cursor_refuses_partial_scan(self):
        client = TikTokClient(settings())
        calls = []

        def repeated_page(path, params, **kwargs):
            calls.append((path, params, kwargs))
            return {
                "itemList": [],
                "HasMoreAfter": True,
                "MaxCursor": "0",
            }

        client._request_json_api = repeated_page
        with self.assertRaisesRegex(TikTokError, "non-advancing cursor"):
            client.story_items(
                USER_ID, profile_url="https://www.tiktok.com/@example"
            )

        self.assertEqual(len(calls), 1)

    def test_story_string_false_stops_after_current_page(self):
        client = TikTokClient(settings())
        calls = []

        def final_page(path, params, **kwargs):
            calls.append((path, params, kwargs))
            return {
                "itemList": [],
                "HasMoreAfter": "false",
                "MaxCursor": "0",
            }

        client._request_json_api = final_page
        stories = client.story_items(
            USER_ID, profile_url="https://www.tiktok.com/@example"
        )

        self.assertEqual(stories, [])
        self.assertEqual(len(calls), 1)


class ClientSafetyTests(unittest.TestCase):
    def test_only_tiktok_domain_cookies_enable_authenticated_requests(self):
        client = TikTokClient(settings())

        def cookie(domain):
            return Cookie(
                version=0,
                name="session",
                value="value",
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )

        client.cookie_jar.set_cookie(cookie(".example.com"))
        self.assertFalse(client.has_cookies)
        client.cookie_jar.set_cookie(cookie(".tiktok.com"))
        self.assertTrue(client.has_cookies)

    def test_short_url_uses_crawler_agent_and_preserves_story_query(self):
        client = TikTokClient(settings())
        calls = []
        final_url = (
            "https://www.tiktok.com/@example/video/123456"
            "?story_type=1"
        )

        class RedirectResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            @staticmethod
            def geturl():
                return final_url

        def request(url, **kwargs):
            calls.append((url, kwargs))
            return RedirectResponse()

        client.request = request
        resolved = client.resolve_short_url(
            "https://www.tiktok.com/t/ZM12345/"
        )

        self.assertEqual(resolved, final_url)
        self.assertEqual(
            calls[0][1]["headers"]["User-Agent"],
            "facebookexternalhit/1.1",
        )

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
        client.story_items = lambda user_id, **kwargs: [MediaItem(
            post_id="555555",
            description="story",
            video_urls=("https://cdn.example/story.mp4",),
            author=other,
            is_story=True,
        )]

        with self.assertRaisesRegex(TikTokError, "another creator"):
            client.collect_stories(
                expected, profile_url="https://www.tiktok.com/@expected"
            )


if __name__ == "__main__":
    unittest.main()
