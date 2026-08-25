import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp import config as config_module
from tt_dlp.cli import parse_args
from tt_dlp.errors import TikTokError


class ConfigurationTests(unittest.TestCase):
    @staticmethod
    def write_config(path: Path, **values) -> Path:
        config = {
            "output": "downloads",
            "cookies_file": "",
            "queue_file": "",
            "identity_file": "profiles.json",
            "queue": [],
            "sleep": 2.0,
            "limit": 0,
            "overwrite": False,
            "dry_run": False,
            "stories": False,
        }
        config.update(values)
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_environment_config_settings_apply_to_explicit_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self.write_config(
                root / "config.json",
                output="archive",
                sleep=4.5,
                stories=True,
                queue=["configured_profile"],
            )
            args = parse_args(["explicit_profile"])

            with patch.dict(
                os.environ, {"TT_DLP_CONFIG": str(config_path)}, clear=False
            ):
                settings, targets = config_module.prepare_run(args)

            self.assertEqual(targets, ["explicit_profile"])
            self.assertEqual(settings.output, root / "archive")
            self.assertEqual(settings.sleep, 4.5)
            self.assertTrue(settings.stories)

    def test_missing_stories_setting_defaults_to_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.write_config(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("stories")
            path.write_text(json.dumps(data), encoding="utf-8")

            settings, _ = config_module.prepare_run(parse_args([
                "--config", str(path), "profile",
            ]))

            self.assertTrue(settings.stories)

    def test_explicit_target_uses_config_defaults_without_running_saved_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self.write_config(
                root / "config.json",
                queue=["saved_one", "saved_two"],
                overwrite=True,
            )
            args = parse_args([
                "--config", str(config_path), "one_off_profile",
            ])

            settings, targets = config_module.prepare_run(args)

            self.assertEqual(targets, ["one_off_profile"])
            self.assertTrue(settings.overwrite)

    def test_config_paths_are_relative_to_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self.write_config(
                root / "config.json",
                output="archive",
                cookies_file="secrets/cookies.txt",
                identity_file="state/profiles.json",
            )

            settings, _ = config_module.prepare_run(parse_args([
                "--config", str(config_path), "profile",
            ]))

            self.assertEqual(settings.output, root / "archive")
            self.assertEqual(settings.cookies, root / "secrets" / "cookies.txt")
            self.assertEqual(
                settings.profile_store, root / "state" / "profiles.json"
            )

    def test_command_line_paths_are_relative_to_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            cli_dir = root / "working"
            config_dir.mkdir()
            cli_dir.mkdir()
            config_path = self.write_config(config_dir / "config.json")
            args = parse_args([
                "--config", str(config_path),
                "--output", "cli-archive",
                "--cookies", "cli-cookies.txt",
                "--profile-store", "cli-profiles.json",
                "profile",
            ])

            with patch.object(config_module.Path, "cwd", return_value=cli_dir):
                settings, _ = config_module.prepare_run(args)

            self.assertEqual(settings.output, cli_dir / "cli-archive")
            self.assertEqual(settings.cookies, cli_dir / "cli-cookies.txt")
            self.assertEqual(settings.profile_store, cli_dir / "cli-profiles.json")

    def test_explicit_queue_file_is_relative_to_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            cli_dir = root / "working"
            config_dir.mkdir()
            cli_dir.mkdir()
            config_path = self.write_config(config_dir / "config.json")
            (cli_dir / "manual-queue.txt").write_text(
                "# comment\nfirst\nsecond\n", encoding="utf-8"
            )
            args = parse_args([
                "--config", str(config_path),
                "--queue-file", "manual-queue.txt",
            ])

            with patch.object(config_module.Path, "cwd", return_value=cli_dir):
                _, targets = config_module.prepare_run(args)

            self.assertEqual(targets, ["first", "second"])

    def test_invalid_boolean_config_values_are_rejected(self):
        for key, value in (
            ("overwrite", "false"),
            ("dry_run", 0),
            ("stories", "true"),
        ):
            with self.subTest(key=key, value=value):
                with tempfile.TemporaryDirectory() as directory:
                    path = self.write_config(
                        Path(directory) / "config.json", **{key: value}
                    )
                    args = parse_args(["--config", str(path), "profile"])

                    with self.assertRaisesRegex(TikTokError, key):
                        config_module.prepare_run(args)

    def test_boolean_is_not_accepted_as_a_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory) / "config.json", limit=True
            )
            args = parse_args(["--config", str(path), "profile"])

            with self.assertRaisesRegex(TikTokError, "limit"):
                config_module.prepare_run(args)

    def test_cli_false_overrides_true_config_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory) / "config.json",
                stories=True,
                overwrite=True,
                dry_run=True,
            )
            args = parse_args([
                "--config", str(path),
                "--no-stories", "--no-overwrite", "--no-dry-run",
                "profile",
            ])

            settings, _ = config_module.prepare_run(args)

            self.assertFalse(settings.stories)
            self.assertFalse(settings.overwrite)
            self.assertFalse(settings.dry_run)

    def test_unknown_config_keys_are_reported_and_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory) / "config.json", typo_option=True
            )
            error = io.StringIO()

            with redirect_stderr(error):
                settings, _ = config_module.prepare_run(parse_args([
                    "--config", str(path), "profile",
                ]))

            self.assertIn("typo_option", error.getvalue())
            self.assertFalse(settings.stories)

    def test_config_queue_requires_only_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory) / "config.json", queue=["valid", None]
            )

            with self.assertRaisesRegex(TikTokError, "list of strings"):
                config_module.prepare_run(parse_args(["--config", str(path)]))

    def test_saved_queue_and_queue_file_are_combined_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "queue.txt").write_text(
                "# comment\nsecond\nfirst\n", encoding="utf-8"
            )
            path = self.write_config(
                root / "config.json",
                queue=["first"],
                queue_file="queue.txt",
            )

            _, targets = config_module.prepare_run(parse_args([
                "--config", str(path),
            ]))

            self.assertEqual(targets, ["first", "second"])

    def test_initialize_config_creates_identity_setting_and_preserves_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.txt"
            queue.write_text("existing-profile\n", encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                result = config_module.initialize_config(root / "config.json")

            data = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(data["identity_file"], "profiles.json")
            self.assertTrue(data["stories"])
            self.assertEqual(queue.read_text(encoding="utf-8"), "existing-profile\n")


if __name__ == "__main__":
    unittest.main()
