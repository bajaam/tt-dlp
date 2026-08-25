import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp import cli as tt


class ConfigurationTests(unittest.TestCase):
    def test_user_config_is_discovered_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_dir = home / ".config" / "tt-dlp"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            args = tt.parse_args([])

            with patch.object(tt.Path, "home", return_value=home):
                discovered = tt._config_path(args)

            self.assertEqual(discovered, config_path)

    def test_environment_config_has_highest_priority(self):
        args = tt.parse_args([])
        with patch.dict(tt.os.environ, {"TT_DLP_CONFIG": "custom.json"}):
            discovered = tt._config_path(args)
        self.assertEqual(discovered, Path("custom.json"))

    def test_init_config_creates_named_config_and_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "settings.json"

            result = tt.initialize_config(config_path)

            self.assertEqual(result, 0)
            self.assertTrue(config_path.is_file())
            self.assertTrue((Path(directory) / "queue.txt").is_file())
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["queue_file"], "queue.txt")

    def test_config_and_queue_file_are_combined_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "queue.txt").write_text(
                "# comment\n@second\n@first\n", encoding="utf-8"
            )
            (root / "config.json").write_text(json.dumps({
                "output": "downloads",
                "queue": ["@first"],
                "queue_file": "queue.txt",
                "sleep": 3,
            }), encoding="utf-8")

            args = tt.parse_args(["--config", str(root / "config.json")])
            args, targets = tt.prepare_run(args)

            self.assertEqual(targets, ["@first", "@second"])
            self.assertEqual(args.sleep, 3.0)
            self.assertEqual(Path(args.output), root / "downloads")

    def test_stories_can_be_enabled_by_config_and_disabled_by_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "queue": ["example"],
                "stories": True,
            }), encoding="utf-8")

            enabled, _ = tt.prepare_run(tt.parse_args([
                "--config", str(config_path),
            ]))
            disabled, _ = tt.prepare_run(tt.parse_args([
                "--config", str(config_path), "--no-stories",
            ]))

            self.assertTrue(enabled.stories)
            self.assertFalse(disabled.stories)

    def test_netscape_cookie_file_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "cookies.txt"
            cookie_file.write_text(
                "# Netscape HTTP Cookie File\n"
                ".tiktok.com\tTRUE\t/\tTRUE\t2147483647\t"
                "sessionid\ttest-value\n",
                encoding="utf-8",
            )
            args = tt.parse_args(["--cookies", str(cookie_file), "@example"])
            args, targets = tt.prepare_run(args)
            downloader = tt.TikTokDownloader(args)

            self.assertEqual(targets, ["@example"])
            self.assertEqual(len(downloader.cookie_jar), 1)


class DownloaderTests(unittest.TestCase):
    def test_empty_authenticated_scan_retries_without_cookies(self):
        args = tt.parse_args(["example"])
        args.cookies = "configured-cookies.txt"
        args, _ = tt.prepare_run(args)
        downloader = object.__new__(tt.TikTokDownloader)
        downloader.args = args
        downloader.opener = object()
        downloader.anonymous_opener = object()
        authenticated_opener = downloader.opener
        calls = []

        def iter_posts(sec_uid):
            calls.append((sec_uid, downloader.opener))
            if downloader.opener is downloader.anonymous_opener:
                yield {"id": "123"}

        downloader._iter_posts = iter_posts
        posts = downloader._collect_posts(
            "creator-id", public_embed_has_posts=True, is_private=False
        )

        self.assertEqual(posts, [{"id": "123"}])
        self.assertEqual(len(calls), 2)
        self.assertIs(downloader.opener, authenticated_opener)

    def test_private_profile_does_not_retry_without_cookies(self):
        args = tt.parse_args(["example"])
        args.cookies = "configured-cookies.txt"
        args, _ = tt.prepare_run(args)
        downloader = object.__new__(tt.TikTokDownloader)
        downloader.args = args
        downloader.opener = object()
        downloader.anonymous_opener = object()
        calls = []
        downloader._iter_posts = lambda sec_uid: calls.append(sec_uid) or iter(())

        posts = downloader._collect_posts(
            "creator-id", public_embed_has_posts=True, is_private=True
        )

        self.assertEqual(posts, [])
        self.assertEqual(calls, ["creator-id"])

    def test_empty_cursor_probe_is_not_counted_as_profile_page(self):
        args = tt.parse_args(["example", "--dry-run"])
        args, _ = tt.prepare_run(args)
        downloader = tt.TikTokDownloader(args)
        responses = iter((
            {"itemList": [], "hasMorePrevious": True},
            {
                "itemList": [{"id": "123", "createTime": "1770000000"}],
                "hasMorePrevious": False,
            },
        ))
        downloader._request_api = lambda params: next(responses)
        output = io.StringIO()

        with redirect_stdout(output):
            posts = list(downloader._iter_posts("creator-id"))

        self.assertEqual([post["id"] for post in posts], ["123"])
        self.assertIn("Profile page 1: 1 new posts (1 total)", output.getvalue())
        self.assertNotIn("Profile page 1: 0", output.getvalue())

    def test_failed_http_response_is_closed_immediately(self):
        args = tt.parse_args(["example"])
        args, _ = tt.prepare_run(args)
        downloader = tt.TikTokDownloader(args)
        body = io.BytesIO(b"Forbidden")
        error = HTTPError(
            "https://www.tiktok.com/test", 403, "Forbidden", {}, body
        )

        class FailingOpener:
            @staticmethod
            def open(request, timeout):
                del request, timeout
                raise error

        downloader.opener = FailingOpener()
        with self.assertRaisesRegex(tt.TikTokError, "HTTP 403"):
            downloader._request(
                "https://www.tiktok.com/test", attempts=1
            )

        self.assertTrue(body.closed)

    def test_recursive_profile_id_lookup(self):
        args = tt.parse_args(["@example"])
        args, _ = tt.prepare_run(args)
        downloader = tt.TikTokDownloader(args)
        downloader.username = "example"

        result = downloader._find_profile_id({
            "nested": [{"uniqueId": "example", "secUid": "creator-id"}]
        })

        self.assertEqual(result, "creator-id")

    def test_recursive_profile_identity_includes_numeric_id(self):
        args = tt.parse_args(["@example"])
        args, _ = tt.prepare_run(args)
        downloader = tt.TikTokDownloader(args)
        downloader.username = "example"

        result = downloader._find_profile_identity({
            "nested": [{
                "uniqueId": "example",
                "secUid": "creator-id",
                "id": "12345",
            }]
        })

        self.assertEqual(result, {"secUid": "creator-id", "id": "12345"})

    def test_collect_stories_resolves_ids_through_embed(self):
        args = tt.parse_args(["example", "--stories", "--dry-run"])
        args, _ = tt.prepare_run(args)
        downloader = tt.TikTokDownloader(args)
        calls = []
        downloader._request_story_api = lambda path, params: (
            calls.append((path, params)) or {
                "storyIdListStructs": [{
                    "authorId": "42",
                    "storyIds": ["100", "101"],
                }]
            }
        )
        downloader._embed_data = lambda path: {
            "videoData": {
                "itemInfos": {
                    "id": path.rsplit("/", 1)[-1],
                    "text": "story text",
                    "video": {"urls": ["https://cdn.example/video.mp4"]},
                }
            }
        }

        stories = downloader._collect_stories("42")

        self.assertEqual([item["id"] for item in stories], ["100", "101"])
        self.assertEqual(
            stories[0]["video"]["playAddr"]["urlList"],
            ["https://cdn.example/video.mp4"],
        )
        self.assertEqual(calls, [(
            "/api/story/user/story_list/", {"authorIds": "42"}
        )])

    def test_story_photo_embed_is_kept_as_carousel(self):
        image_post = {
            "title": "photo story",
            "images": [{
                "imageURL": {"urlList": ["https://cdn.example/one.jpg"]}
            }],
        }
        story = tt.TikTokDownloader._story_from_embed("100", {
            "videoData": {
                "itemInfos": {"id": "100", "text": ""},
                "imagePostInfo": image_post,
            }
        })

        self.assertEqual(story["imagePost"], image_post)
        self.assertEqual(story["desc"], "story")

    def test_filename_includes_carousel_number(self):
        name = tt.TikTokDownloader._filename(
            "123", "description", "jpg", number=2
        )
        self.assertEqual(name, "123_02 description.jpg")


if __name__ == "__main__":
    unittest.main()
