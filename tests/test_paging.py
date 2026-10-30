import base64
import json
import os
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from gpt_parser_mcp.services.line_index import BLOCK_SIZE, HEADER_SIZE, LineIndex
from gpt_parser_mcp.services.markdown import MarkdownError, MarkdownStore


class PagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MarkdownStore(Path(temporary.name))
        self.store.new('Campaign')
        self.directory = self.store.base_dir / 'Campaign'
        self.path = self.directory / 'notes.md'
        self.index_path = self.directory / '.notes.md.lines'

    def test_list_tables_only_returns_usable_direct_markdown_files(self):
        for name in ['notes.md', 'Русский.md', 'a.md', 'ignored.txt', '.md', '..md']:
            (self.directory / name).touch()
        (self.directory / 'folder.md').mkdir()
        (self.directory / 'link.md').symlink_to(self.path)
        (self.directory / 'folder.md' / 'nested.md').touch()
        self.assertEqual(self.store.list_tables('Campaign'), ['a', 'notes', 'Русский'])
        with self.assertRaises(FileNotFoundError):
            self.store.list_tables('Missing')

    def test_index_boundaries_crlf_and_unterminated_lines(self):
        for count in [0, 1, 255, 256, 257, 512, 513, 1000]:
            for terminated in [False, True]:
                with self.subTest(count=count, terminated=terminated):
                    lines = [f'строка {i}' for i in range(1, count + 1)]
                    data = '\r\n'.join(lines) + ('\r\n' if terminated and lines else '')
                    self.path.write_bytes(data.encode())
                    for start in [1, 255, 256, 257, 511, 512, 513, 998, 1100]:
                        self.assertEqual(self.store.get('Campaign', 'notes', start, 3), lines[start - 1:start + 2])

    def test_index_survives_restart_and_avoids_rebuilding(self):
        self.path.write_text(''.join(f'строка {i}\n' for i in range(1, 10001)))
        self.assertEqual(self.store.get('Campaign', 'notes', 9999, 2), ['строка 9999', 'строка 10000'])
        self.assertLess(self.index_path.stat().st_size, 1024)
        original = self.index_path.stat().st_mtime_ns
        restarted = MarkdownStore(self.store.base_dir)
        with patch.object(LineIndex, '_build', side_effect=AssertionError('unexpected rebuild')):
            self.assertEqual(restarted.get('Campaign', 'notes', 501, 1), ['строка 501'])
        self.assertEqual(self.index_path.stat().st_mtime_ns, original)

    def test_index_invalidates_after_append_replace_and_same_size_external_edit(self):
        self.path.write_text('first\n' * 600)
        self.store.get('Campaign', 'notes', 500, 1)
        self.store.insert('Campaign', 'notes', 'appended')
        self.assertEqual(self.store.get('Campaign', 'notes', 601, 1), ['appended'])
        self.store.update('Campaign', 'notes', [1], 'long replacement of the first line')
        self.assertEqual(self.store.get('Campaign', 'notes', 601, 1), ['appended'])
        # Same inode, byte size and restored mtime; ctime still invalidates offsets.
        self.path.write_bytes(b'a\n' * 600)
        self.store.get('Campaign', 'notes', 500, 1)
        before = self.path.stat()
        with self.path.open('r+b') as stream:
            stream.write(b'aa')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.store.get('Campaign', 'notes', 600, 1), [])

    def test_corrupt_and_truncated_cache_are_rebuilt(self):
        self.path.write_text('line\n' * 1000)
        for contents in [b'broken', b'[]'.ljust(HEADER_SIZE, b' '), b'']:
            self.index_path.write_bytes(contents)
            self.assertEqual(self.store.get('Campaign', 'notes', 999, 1), ['line'])
        self.index_path.write_bytes(self.index_path.read_bytes()[:-1])
        self.assertEqual(self.store.get('Campaign', 'notes', 999, 1), ['line'])

    def test_index_memory_is_bounded_for_a_huge_line(self):
        with self.path.open('wb') as stream:
            for _ in range(24):
                stream.write(b'x' * BLOCK_SIZE)
            stream.write(b'\n' + b'small\n' * 300)
        tracemalloc.start()
        try:
            self.assertEqual(self.store.get('Campaign', 'notes', 300, 2), ['small', 'small'])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * BLOCK_SIZE)

    def test_search_pages_cross_index_boundary_without_duplicates_or_gaps(self):
        lines = [f'{i} — [match]' if i % 7 == 0 else f'{i} nope' for i in range(1, 1801)]
        self.path.write_bytes('\r\n'.join(lines).encode())
        cursor = None
        results = []
        versions = set()
        while True:
            page = self.store.search_page('Campaign', 'notes', '[match]', 13, cursor)
            results.extend((item.lineId, item.text) for item in page.matches)
            versions.add(page.version)
            cursor = page.nextCursor
            if cursor is None:
                break
        self.assertEqual(results, [(i, line) for i, line in enumerate(lines, 1) if i % 7 == 0])
        self.assertEqual(len(versions), 1)
        self.assertTrue(self.index_path.is_file())

    def test_page_byte_budget_returns_resumable_short_pages(self):
        self.path.write_bytes('ёж\nёж\nёж\n'.encode())
        store = MarkdownStore(self.store.base_dir, max_result_bytes=6)
        cursor = None
        for line_id in range(1, 4):
            page = store.search_page('Campaign', 'notes', '', 10, cursor)
            self.assertEqual([(r.lineId, r.text) for r in page.matches], [(line_id, 'ёж')])
            cursor = page.nextCursor
        self.assertIsNone(cursor)
        self.path.write_text('oversized\n')
        with self.assertRaisesRegex(MarkdownError, 'Matching line exceeds'):
            store.search_page('Campaign', 'notes', '', 10)

    def test_empty_no_match_blank_lines_literal_queries_and_final_page(self):
        for raw, query, expected in [
            (b'', '', []), (b'aa\n', 'absent', []),
            (b'\n\nlast', '', [(1, ''), (2, ''), (3, 'last')]),
            (b'-flag\na.b\naxb\n', '-flag', [(1, '-flag')]),
            (b'-flag\na.b\naxb\n', 'a.b', [(2, 'a.b')]),
        ]:
            self.path.write_bytes(raw)
            page = self.store.search_page('Campaign', 'notes', query, max(1, len(expected)))
            self.assertEqual([(r.lineId, r.text) for r in page.matches], expected)
            self.assertIsNone(page.nextCursor)

    def test_cursor_scope_validation_and_file_changes(self):
        self.path.write_text('x\nx\nx\n')
        cursor = self.store.search_page('Campaign', 'notes', 'x', 1).nextCursor
        self.store.insert('Campaign', 'other', 'x')
        self.store.new('Other')
        self.store.insert('Other', 'notes', 'x')
        for name, table, query, token in [
            ('Campaign', 'notes', 'different', cursor),
            ('Campaign', 'other', 'x', cursor), ('Other', 'notes', 'x', cursor),
            *[('Campaign', 'notes', 'x', bad) for bad in ['', 'bad', 'x' * 3000,
                base64.urlsafe_b64encode(json.dumps([]).encode()).decode()]],
        ]:
            with self.subTest(name=name, table=table, query=query), self.assertRaises(MarkdownError):
                self.store.search_page(name, table, query, 1, token)
        self.store.insert('Campaign', 'notes', 'x')
        with self.assertRaisesRegex(MarkdownError, 'File changed'):
            self.store.search_page('Campaign', 'notes', 'x', 1, cursor)

    def test_invalid_page_limits_and_query_are_rejected(self):
        self.path.touch()
        for limit in [0, -1, True, 1.5, 10001]:
            with self.subTest(limit=limit), self.assertRaises(MarkdownError):
                self.store.search_page('Campaign', 'notes', '', limit)
        for query in ['a\nb', '\r', '\x00']:
            with self.assertRaises(MarkdownError):
                self.store.search_page('Campaign', 'notes', query, 1)
