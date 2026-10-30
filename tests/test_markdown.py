import os
import stat
import tempfile
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from gpt_parser_mcp.services.markdown import CHUNK_SIZE, MarkdownError, MarkdownStore


class MarkdownStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = MarkdownStore(self.root / "campaigns")
        self.store.new("Моя кампания")
        self.path = self.store.base_dir / "Моя кампания" / "notes.md"

    def test_campaign_creation_is_idempotent_and_listing_is_sorted(self) -> None:
        self.store.insert("Моя кампания", "notes", "keep")
        self.store.new("Моя кампания")
        self.store.new("Alpha")
        (self.store.base_dir / "unrelated.txt").write_text("text")
        self.assertEqual(self.store.list(), ["Alpha", "Моя кампания"])
        self.assertEqual(self.store.get_all("Моя кампания", "notes"), ["keep"])
        self.assertEqual(MarkdownStore(self.root / "absent").list(), [])

    def test_insert_creates_file_and_separates_unterminated_last_line(self) -> None:
        self.store.insert("Моя кампания", "notes", "первая")
        self.path.write_bytes(b"first without newline")
        self.store.insert("Моя кампания", "notes", "вторая")
        self.store.insert("Моя кампания", "notes", "")
        self.assertEqual(self.store.get_all("Моя кампания", "notes"), ["first without newline", "вторая", ""])

    def test_get_preserves_content_and_handles_eof(self) -> None:
        self.path.write_bytes("  один  \r\n\r\nтри\nчетыре".encode())
        self.assertEqual(self.store.get("Моя кампания", "notes", 1, 2), ["  один  ", ""])
        self.assertEqual(self.store.get("Моя кампания", "notes", 3, 99), ["три", "четыре"])
        self.assertEqual(self.store.get("Моя кампания", "notes", 99, 1), [])
        self.assertEqual(self.store.get("Моя кампания", "notes", 1, 0), [])

    def test_empty_file(self) -> None:
        self.path.touch()
        self.assertEqual(self.store.get_all("Моя кампания", "notes"), [])
        self.assertEqual(self.store.search("Моя кампания", "notes", ""), [])
        with self.assertRaises(MarkdownError):
            self.store.update("Моя кампания", "notes", [1], "replacement")
        self.assertEqual(self.path.read_bytes(), b"")

    def test_grep_search_is_literal_and_preserves_matching_lines(self) -> None:
        lines = ["a.*b", "axxb", "-n Привет", "a.*b", "last Привет"]
        self.path.write_text("\n".join(lines), encoding="utf-8")
        self.assertEqual(self.store.search("Моя кампания", "notes", ".*"), ["a.*b", "a.*b"])
        self.assertEqual(self.store.search("Моя кампания", "notes", "-n"), ["-n Привет"])
        self.assertEqual(self.store.search("Моя кампания", "notes", "Привет"), ["-n Привет", "last Привет"])
        self.assertEqual(self.store.search("Моя кампания", "notes", "привет"), [])
        self.assertEqual(self.store.search("Моя кампания", "notes", ""), lines)

    def test_update_preserves_newlines_permissions_and_unselected_bytes(self) -> None:
        self.path.write_bytes("one\r\ntwo\nтри".encode())
        self.path.chmod(0o640)
        self.store.update("Моя кампания", "notes", [3, 1, 1], "новая")
        self.assertEqual(self.path.read_bytes(), "новая\r\ntwo\nновая".encode())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.store.update("Моя кампания", "notes", [], "unused")
        self.assertEqual(self.store.get("Моя кампания", "notes", 2, 1), ["two"])

    def test_invalid_update_keeps_original_and_removes_temporary_file(self) -> None:
        original = b"one\ntwo\n"
        self.path.write_bytes(original)
        for ids in ([1, 3], [0], [-1], [True], [1.5]):
            with self.subTest(ids=ids), self.assertRaises(MarkdownError):
                self.store.update("Моя кампания", "notes", ids, "changed")
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.glob(".mcp-*.tmp")), [])

    def test_failed_replace_keeps_original_and_removes_temporary_file(self) -> None:
        self.path.write_bytes(b"original\n")
        with patch("gpt_parser_mcp.services.markdown.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.store.update("Моя кампания", "notes", [1], "changed")
        self.assertEqual(self.path.read_bytes(), b"original\n")
        self.assertEqual(list(self.path.parent.glob(".mcp-*.tmp")), [])

    def test_invalid_names_and_multiline_values_cannot_write(self) -> None:
        for name in ("", "..", "../outside", "/absolute", "a/b", "a\\b", "a\x00b"):
            with self.subTest(name=name), self.assertRaises(MarkdownError):
                self.store.new(name)
            with self.subTest(table=name), self.assertRaises(MarkdownError):
                self.store.insert("Моя кампания", name, "bad")
        for line in ("a\nb", "a\rb", "a\x00b"):
            with self.subTest(line=line), self.assertRaises(MarkdownError):
                self.store.insert("Моя кампания", "notes", line)
        self.assertFalse(self.path.exists())

    def test_symlinks_cannot_escape_storage(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        external_file = outside / "secret.md"
        external_file.write_text("keep", encoding="utf-8")
        (self.store.base_dir / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.new("linked")
        with self.assertRaises(OSError):
            self.store.insert("linked", "secret", "bad")
        self.path.symlink_to(external_file)
        with self.assertRaises(OSError):
            self.store.insert("Моя кампания", "notes", "bad")
        with self.assertRaises(OSError):
            self.store.get_all("Моя кампания", "notes")
        self.assertEqual(external_file.read_text(), "keep")
        self.assertNotIn("linked", self.store.list())

    def test_missing_campaign_or_table_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.insert("absent", "notes", "text")
        with self.assertRaises(FileNotFoundError):
            self.store.get_all("Моя кампания", "absent")
        with self.assertRaises(FileNotFoundError):
            self.store.search("Моя кампания", "absent", "query")

    def test_concurrent_writers_on_separate_instances_do_not_lose_updates(self) -> None:
        self.path.write_text("old\n" * 20, encoding="utf-8")

        def write(index: int) -> None:
            store = MarkdownStore(self.store.base_dir)
            store.update("Моя кампания", "notes", [index + 1], f"updated-{index}")
            store.insert("Моя кампания", "notes", f"appended-{index}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(20)))
        lines = self.store.get_all("Моя кампания", "notes")
        self.assertEqual(lines[:20], [f"updated-{i}" for i in range(20)])
        self.assertCountEqual(lines[20:], [f"appended-{i}" for i in range(20)])

    def test_large_file_with_giant_line_uses_bounded_memory(self) -> None:
        # A 24 MiB individual line also catches accidentally unbounded readline().
        with self.path.open("wb") as destination:
            block = b"x" * CHUNK_SIZE
            for _ in range(24):
                destination.write(block)
            destination.write(b"\nsecond\nlast\n")
        tracemalloc.start()
        try:
            self.assertEqual(self.store.get("Моя кампания", "notes", 2, 1), ["second"])
            self.store.update("Моя кампания", "notes", [2], "changed")
            self.store.update("Моя кампания", "notes", [1], "small")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * CHUNK_SIZE)
        self.assertEqual(self.path.read_bytes(), b"small\nchanged\nlast\n")

    def test_crlf_at_chunk_boundary_is_preserved(self) -> None:
        with self.path.open("wb") as destination:
            destination.write(b"x" * (CHUNK_SIZE - 1) + b"\r\nsecond")
        self.store.update("Моя кампания", "notes", [1], "new")
        self.assertEqual(self.path.read_bytes(), b"new\r\nsecond")

    def test_nonregular_table_is_rejected_without_blocking(self) -> None:
        os.mkfifo(self.path)
        with self.assertRaises(MarkdownError):
            self.store.get_all("Моя кампания", "notes")

    def test_result_limits_reject_instead_of_truncating_and_get_can_page(self) -> None:
        self.path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        store = MarkdownStore(self.store.base_dir, max_result_bytes=8, max_result_lines=2)
        self.assertEqual(store.get("Моя кампания", "notes", 1, 2), ["one", "two"])
        self.assertEqual(store.get("Моя кампания", "notes", 3, 1), ["three"])
        for operation in (
            lambda: store.get_all("Моя кампания", "notes"),
            lambda: store.get("Моя кампания", "notes", 1, 3),
            lambda: store.search("Моя кампания", "notes", ""),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(MarkdownError, "no partial result"):
                operation()
        self.path.write_bytes(b"\n\n\n")
        with self.assertRaises(MarkdownError):
            store.get_all("Моя кампания", "notes")
        self.path.write_bytes(b"x" * (2 * CHUNK_SIZE))
        with self.assertRaises(MarkdownError):
            store.get("Моя кампания", "notes", 1, 1)
