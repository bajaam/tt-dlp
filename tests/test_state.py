import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tt_dlp.errors import TikTokError
from tt_dlp.models import ProfileIdentity
from tt_dlp.state import ProfileStore, safe_directory_name
from tt_dlp.targets import parse_target


SEC_UID = "MS4wLjABAAAAabcdefghijklmnop"
OTHER_SEC_UID = "MS4wLjABAAAAzyxwvutsrqponmlk"
USER_ID = "123456789"


class ProfileStoreTests(unittest.TestCase):
    def test_profile_round_trip_can_be_found_by_every_stable_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            saved = store.update(ProfileIdentity(
                username="current_name",
                user_id=USER_ID,
                sec_uid=SEC_UID,
            ))
            loaded = ProfileStore(path)

            self.assertEqual(saved.directory, "current_name")
            for raw in (
                "current_name",
                f"userid:{USER_ID}",
                f"secuid:{SEC_UID}",
                f"ttid:{USER_ID}:{SEC_UID}",
            ):
                with self.subTest(raw=raw):
                    self.assertEqual(loaded.find(parse_target(raw)), saved)

    def test_username_rename_keeps_folder_and_old_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            original = store.update(ProfileIdentity(
                username="old_name",
                user_id=USER_ID,
                sec_uid=SEC_UID,
            ))
            renamed = store.update(
                ProfileIdentity(
                    username="new_name",
                    user_id=USER_ID,
                    sec_uid=SEC_UID,
                ),
                requested_alias="old_name",
            )
            loaded = ProfileStore(path)

            self.assertEqual(original.directory, "old_name")
            self.assertEqual(renamed.directory, "old_name")
            self.assertIn("old_name", renamed.aliases)
            self.assertEqual(
                loaded.find(parse_target("old_name")).username, "new_name"
            )
            self.assertEqual(
                loaded.find(parse_target("new_name")).directory, "old_name"
            )

    def test_state_file_has_a_version_and_no_temporary_file_is_left(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profiles.json"
            ProfileStore(path).update(ProfileIdentity(
                username="example",
                user_id=USER_ID,
                sec_uid=SEC_UID,
            ))

            data = json.loads(path.read_text(encoding="utf-8"))
            leftovers = list(root.glob(".profiles.json.*.tmp"))
            self.assertEqual(data["version"], 1)
            self.assertEqual(len(data["profiles"]), 1)
            self.assertEqual(leftovers, [])

    def test_store_without_a_path_still_resolves_in_memory(self):
        store = ProfileStore(None)
        saved = store.update(ProfileIdentity(
            username="example",
            user_id=USER_ID,
            sec_uid=SEC_UID,
        ))

        self.assertEqual(store.find(parse_target(f"secuid:{SEC_UID}")), saved)

    def test_corrupt_or_unsupported_state_is_rejected(self):
        invalid_documents = (
            {"version": 999, "profiles": []},
            {"version": 1, "profiles": {}},
            {"version": 1, "profiles": [{"username": ".."}]},
            {"version": 1, "profiles": [{"username": "ok", "aliases": "bad"}]},
        )

        for document in invalid_documents:
            with self.subTest(document=document):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "profiles.json"
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(TikTokError):
                        ProfileStore(path)

    def test_conflicting_stable_identifiers_do_not_replace_saved_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            store.update(ProfileIdentity(
                username="example",
                user_id=USER_ID,
                sec_uid=SEC_UID,
            ))

            with self.assertRaisesRegex(
                TikTokError, "(?i:conflict|different)"
            ):
                store.update(ProfileIdentity(
                    username="imposter",
                    user_id=USER_ID,
                    sec_uid=OTHER_SEC_UID,
                ))

            preserved = ProfileStore(path).find(parse_target(f"userid:{USER_ID}"))
            self.assertEqual(preserved.sec_uid, SEC_UID)

    def test_composite_lookup_requires_both_stable_fields(self):
        store = ProfileStore(None)
        store.update(ProfileIdentity(
            username="example",
            user_id=USER_ID,
            sec_uid=SEC_UID,
        ))

        self.assertIsNone(store.find(parse_target(
            f"ttid:{USER_ID}:{OTHER_SEC_UID}"
        )))

    def test_reused_username_gets_exact_match_and_unique_directory(self):
        store = ProfileStore(None)
        first = store.update(ProfileIdentity(
            username="old_name",
            user_id=USER_ID,
            sec_uid=SEC_UID,
        ))
        renamed = store.update(ProfileIdentity(
            username="new_name",
            user_id=USER_ID,
            sec_uid=SEC_UID,
        ))
        second = store.update(ProfileIdentity(
            username="old_name",
            user_id="987654321",
            sec_uid=OTHER_SEC_UID,
        ))

        self.assertEqual(first.directory, renamed.directory)
        self.assertNotEqual(second.directory, renamed.directory)
        self.assertEqual(
            store.find(parse_target("old_name")).user_id,
            "987654321",
        )


class SafeDirectoryTests(unittest.TestCase):
    def test_path_separators_are_replaced_within_one_component(self):
        value = safe_directory_name("..\\outside/child")

        self.assertNotIn("/", value)
        self.assertNotIn("\\", value)
        self.assertNotIn(value, {"", ".", ".."})

    def test_empty_parent_and_windows_device_names_are_safe(self):
        with self.assertRaises(TikTokError):
            safe_directory_name("..")
        self.assertEqual(safe_directory_name("CON"), "CON_")
        self.assertEqual(safe_directory_name("nul"), "nul_")


if __name__ == "__main__":
    unittest.main()
