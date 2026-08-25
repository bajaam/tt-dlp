import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp import cli
from tt_dlp.errors import TikTokError
from tt_dlp.models import Settings


class CommandLineTests(unittest.TestCase):
    def test_boolean_optional_flags_have_explicit_false_forms(self):
        args = cli.parse_args([
            "--no-overwrite", "--no-dry-run", "--no-stories", "example",
        ])

        self.assertFalse(args.overwrite)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.stories)

    def test_main_continues_after_one_target_error(self):
        settings = Settings(
            output=Path("downloads"),
            cookies=None,
            profile_store=None,
            limit=0,
            sleep=0.0,
            overwrite=False,
            dry_run=True,
            stories=False,
            identify=False,
        )

        class FakeDownloader:
            def __init__(self, received):
                self.settings = received

            def run(self, target):
                if target == "bad":
                    raise TikTokError("broken target")
                return 0

        output = io.StringIO()
        error = io.StringIO()
        with (
            patch.object(cli, "prepare_run", return_value=(settings, ["bad", "good"])),
            patch.object(cli, "TikTokDownloader", FakeDownloader),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            result = cli.main([])

        self.assertEqual(result, 1)
        self.assertIn("1 succeeded, 1 failed", output.getvalue())
        self.assertIn("Error for bad: broken target", error.getvalue())

    def test_main_returns_keyboard_interrupt_exit_code(self):
        with (
            patch.object(cli, "prepare_run", side_effect=KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = cli.main(["example"])

        self.assertEqual(result, 130)


if __name__ == "__main__":
    unittest.main()
