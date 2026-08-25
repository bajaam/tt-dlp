import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp.errors import TikTokError
from tt_dlp.models import TargetKind
from tt_dlp.targets import parse_target


SEC_UID = "MS4wLjABAAAAabcdefghijklmnop"


class TargetParsingTests(unittest.TestCase):
    def test_username_forms_are_normalized(self):
        plain = parse_target("example_user")
        at_name = parse_target("@example_user")

        self.assertEqual(plain.kind, TargetKind.USERNAME)
        self.assertEqual(plain.username, "example_user")
        self.assertEqual(at_name.username, "example_user")
        self.assertEqual(at_name.display, "@example_user")

    def test_profile_and_media_urls_preserve_target_kind(self):
        profile = parse_target("https://www.tiktok.com/@example")
        video = parse_target(
            "https://www.tiktok.com/@example/video/1234567890?lang=en"
        )
        photo = parse_target(
            "https://m.tiktok.com/@example/photo/1234567891"
        )
        story = parse_target(
            "https://tiktok.com/@example/story/1234567892"
        )

        self.assertIsNone(profile.media_kind)
        self.assertEqual((video.media_kind, video.post_id), ("video", "1234567890"))
        self.assertEqual((photo.media_kind, photo.post_id), ("photo", "1234567891"))
        self.assertEqual((story.media_kind, story.post_id), ("story", "1234567892"))

    def test_shared_video_urls_with_story_markers_are_stories(self):
        story_type = parse_target(
            "https://www.tiktok.com/@example/video/1234567890?story_type=1"
        )
        aweme_type = parse_target(
            "https://www.tiktok.com/@example/video/1234567891?aweme_type=40"
        )
        ordinary = parse_target(
            "https://www.tiktok.com/@example/video/1234567892?story_type=0"
        )

        self.assertEqual(story_type.media_kind, "story")
        self.assertEqual(aweme_type.media_kind, "story")
        self.assertEqual(ordinary.media_kind, "video")

    def test_share_media_urls_use_embed_author_identity(self):
        video = parse_target(
            "https://www.tiktok.com/share/video/1234567890"
        )
        photo_story = parse_target(
            "https://www.tiktok.com/share/photo/1234567891?story_type=1"
        )

        self.assertEqual(video.username, "")
        self.assertEqual((video.media_kind, video.post_id), (
            "video", "1234567890",
        ))
        self.assertEqual((photo_story.media_kind, photo_story.post_id), (
            "story", "1234567891",
        ))

    def test_stable_identifier_forms_are_unambiguous(self):
        stable = parse_target(f"ttid:123456789:{SEC_UID}")
        sec_uid = parse_target(f"secuid:{SEC_UID}")
        user_id = parse_target("userid:123456789")
        uid_alias = parse_target("uid:123456789")

        self.assertEqual(stable.kind, TargetKind.STABLE_ID)
        self.assertEqual(stable.user_id, "123456789")
        self.assertEqual(stable.sec_uid, SEC_UID)
        self.assertEqual(stable.display, f"ttid:123456789:{SEC_UID}")
        self.assertEqual(sec_uid.kind, TargetKind.SEC_UID)
        self.assertEqual(sec_uid.sec_uid, SEC_UID)
        self.assertEqual(user_id.kind, TargetKind.USER_ID)
        self.assertEqual(uid_alias.user_id, user_id.user_id)

    def test_bare_digits_remain_a_possible_username(self):
        target = parse_target("123456789")

        self.assertEqual(target.kind, TargetKind.USERNAME)
        self.assertEqual(target.username, "123456789")

    def test_numeric_profile_url_remains_a_numeric_username(self):
        target = parse_target("https://www.tiktok.com/@123456789")

        self.assertEqual(target.kind, TargetKind.USERNAME)
        self.assertEqual(target.username, "123456789")

    def test_short_links_are_kept_for_later_resolution(self):
        target = parse_target("https://vm.tiktok.com/ZM12345/")
        web_target = parse_target("https://www.tiktok.com/t/ZM67890/")

        self.assertEqual(target.kind, TargetKind.SHORT_URL)
        self.assertEqual(target.short_url, "https://vm.tiktok.com/ZM12345/")
        self.assertEqual(web_target.kind, TargetKind.SHORT_URL)
        self.assertEqual(
            web_target.short_url, "https://www.tiktok.com/t/ZM67890/"
        )

    def test_non_tiktok_and_lookalike_hosts_are_rejected(self):
        invalid = (
            "https://example.com/@person/video/123456",
            "https://www.tiktok.com.evil.example/@person/video/123456",
            "https://www.tiktok.com@evil.example/@person/video/123456",
            "https://vm.tiktok.com.evil.example/ZM12345/",
        )

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(TikTokError):
                    parse_target(value)

    def test_traversal_and_path_separator_inputs_are_rejected(self):
        invalid = (
            "@..\\outside",
            "@../outside",
            "..",
            "person..name",
            "@person?query",
            "@person#fragment",
            "https://www.tiktok.com/@%2e%2e/video/123456",
            "https://www.tiktok.com/@person%5coutside/video/123456",
        )

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(TikTokError):
                    parse_target(value)

    def test_extra_or_malformed_url_paths_are_rejected(self):
        invalid = (
            "https://www.tiktok.com/",
            "https://www.tiktok.com/person",
            "https://www.tiktok.com/@person/live/123456",
            "https://www.tiktok.com/@person/video/not-digits",
            "https://www.tiktok.com/@person/video/123456/extra",
            "https://vm.tiktok.com/",
        )

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(TikTokError):
                    parse_target(value)

    def test_invalid_stable_identifiers_are_rejected(self):
        invalid = (
            "userid:abc",
            "userid:1234",
            "secuid:short",
            f"ttid:not-digits:{SEC_UID}",
            "ttid:123456:short",
            f"ttid:123456:{SEC_UID}:extra",
        )

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(TikTokError):
                    parse_target(value)


if __name__ == "__main__":
    unittest.main()
