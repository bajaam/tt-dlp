import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp import cli as tt


class ConfigurationTests(unittest.TestCase):
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
    def test_recursive_profile_id_lookup(self):
        args = tt.parse_args(["@example"])
        args, _ = tt.prepare_run(args)
        downloader = tt.TikTokDownloader(args)
        downloader.username = "example"

        result = downloader._find_profile_id({
            "nested": [{"uniqueId": "example", "secUid": "creator-id"}]
        })

        self.assertEqual(result, "creator-id")

    def test_filename_includes_carousel_number(self):
        name = tt.TikTokDownloader._filename(
            "123", "description", "jpg", number=2
        )
        self.assertEqual(name, "123_02 description.jpg")


if __name__ == "__main__":
    unittest.main()
