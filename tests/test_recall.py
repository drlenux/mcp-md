import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpt_parser_mcp.services.markdown import MarkdownError, MarkdownStore
from gpt_parser_mcp.services.recall import QmdRunner, RecallService
from qmd_fixture import create_qmd_fixture


class RecallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = MarkdownStore(self.root / 'data')
        self.store.new('Campaign')
        self.campaign = self.store.base_dir / 'Campaign'
        self.cache = self.campaign / '.mcp-recall'
        self.runner = QmdRunner(str(create_qmd_fixture(self.root)), self.root / 'models')
        self.service = RecallService(self.store, self.runner, chunk_bytes=256)

    def calls(self):
        return [json.loads(line)[2] for line in (self.cache / 'fixture_calls.jsonl').read_text().splitlines()]

    def test_fragments_preserve_utf8_crlf_empty_lines_and_huge_line_positions(self):
        raw = ('head\r\n\n' + '😀ж' * 1000 + '\r\nlast').encode()
        fragments = list(self.service._fragments(io.BytesIO(raw)))
        self.assertEqual(b''.join(fragment[0] for fragment in fragments), raw)
        previous = 0
        for data, first, last, begin, end in fragments:
            self.assertEqual(begin, previous)
            self.assertEqual(data, raw[begin:end])
            self.assertLessEqual(len(data), 259)
            self.assertEqual(first, raw[:begin].count(b'\n') + 1)
            self.assertEqual(last, raw[:end].count(b'\n') + (not data.endswith(b'\n')))
            data.decode('utf-8')
            previous = end

    def test_recall_returns_original_text_positions_and_reuses_unchanged_index(self):
        originals = {'notes': 'Дракон хранит ключ.\n' * 20, 'people': 'Мария знает тайну.\n'}
        for table, content in originals.items():
            (self.campaign / f'{table}.md').write_text(content)
        result = self.service.recall('Campaign', '__all__', 100)
        self.assertEqual(result.backend, 'qmd:query')
        self.assertFalse(result.truncated)
        self.assertEqual({r.table for r in result.matches}, set(originals))
        for match in result.matches:
            raw = originals[match.table].encode()
            self.assertEqual(match.text, raw[match.startByte:match.endByte].decode())
            self.assertEqual(match.startLine, raw[:match.startByte].count(b'\n') + 1)
            self.assertEqual(len(match.version), 64)
        self.service.recall('Campaign', 'Дракон', 1)
        self.assertEqual(self.calls(), ['update', 'embed', 'query', 'query'])
        restarted = RecallService(self.store, self.runner, chunk_bytes=256)
        restarted.recall('Campaign', 'Дракон', 1)
        self.assertEqual(self.calls()[-2:], ['query', 'query'])

    def test_append_update_and_delete_refresh_sources_and_remove_old_fragments(self):
        self.store.insert('Campaign', 'notes', 'old')
        self.service.recall('Campaign', 'old', 10)
        previous = set((self.cache / 'chunks').iterdir())
        self.store.insert('Campaign', 'notes', 'new')
        self.assertIn('new', self.service.recall('Campaign', 'new', 10).matches[0].text)
        self.assertFalse(previous & set((self.cache / 'chunks').iterdir()))
        self.store.update('Campaign', 'notes', [1], 'changed')
        self.assertEqual(self.service.recall('Campaign', 'old', 10).matches, [])
        self.store.insert('Campaign', 'other', 'second')
        self.service.recall('Campaign', 'second', 10)
        (self.campaign / 'notes.md').unlink()
        found = self.service.recall('Campaign', '__all__', 10)
        self.assertEqual({r.table for r in found.matches}, {'other'})
        with sqlite3.connect(self.cache / 'manifest.sqlite') as db:
            self.assertEqual(db.execute('SELECT name FROM sources').fetchall(), [('other',)])
        self.assertEqual(self.calls().count('update'), 5)

    def test_campaigns_are_isolated(self):
        self.store.insert('Campaign', 'notes', 'first')
        self.store.new('Other')
        self.store.insert('Other', 'notes', 'second')
        self.assertEqual(self.service.recall('Campaign', 'second', 10).matches, [])
        self.assertEqual(self.service.recall('Other', 'first', 10).matches, [])
        self.assertEqual(self.service.recall('Other', 'second', 10).matches[0].text, 'second\n')

    def test_chunk_configuration_change_rebuilds_derived_data(self):
        self.store.insert('Campaign', 'notes', 'x' * 1024)
        self.service.recall('Campaign', 'x', 10)
        before = len(list((self.cache / 'chunks').glob('*.md')))
        changed = RecallService(self.store, self.runner, chunk_bytes=512)
        changed.recall('Campaign', 'x', 10)
        after = len(list((self.cache / 'chunks').glob('*.md')))
        self.assertLess(after, before)
        self.assertEqual(self.calls().count('update'), 2)

    def test_byte_budget_is_explicit_and_fragments_include_partial_long_lines(self):
        self.store.insert('Campaign', 'notes', 'ж' * 500)
        self.store.max_result_bytes = 300
        result = self.service.recall('Campaign', '__all__', 10)
        self.assertTrue(result.truncated)
        self.assertGreater(len(result.matches), 0)
        self.assertLessEqual(sum(len(r.text.encode()) for r in result.matches), 300)
        self.assertTrue(all(r.startLine == r.endLine == 1 for r in result.matches))
        self.store.max_result_bytes = 1
        with self.assertRaisesRegex(MarkdownError, 'fragment exceeds'):
            self.service.recall('Campaign', '__all__', 10)

    def test_changed_source_during_query_is_rejected(self):
        self.store.insert('Campaign', 'notes', 'old')
        query = self.runner.query

        def mutate(directory, text, limit):
            result = query(directory, text, limit)
            self.store.update('Campaign', 'notes', [1], 'changed')
            return result

        with patch.object(self.runner, 'query', side_effect=mutate):
            with self.assertRaisesRegex(MarkdownError, 'Source changed'):
                self.service.recall('Campaign', '__all__', 10)
        self.assertEqual(self.service.recall('Campaign', '__all__', 10).matches[0].text, 'changed\n')

    def test_invalid_foreign_and_unknown_qmd_results_are_rejected(self):
        self.store.insert('Campaign', 'notes', 'first')
        invalid = [{}, [None], [{'file': 'qmd://foreign/id.md', 'score': 1}],
                   [{'file': 'qmd://campaign/id.md', 'score': float('nan')}],
                   [{'file': 'qmd://campaign/../id.md', 'score': 1}],
                   [{'file': 'qmd://campaign/missing.md', 'score': 1}]]
        for results in invalid:
            with self.subTest(results=results), patch.object(self.runner, 'query', return_value=results):
                with self.assertRaises(MarkdownError):
                    self.service.recall('Campaign', 'first', 10)
        self.assertEqual(self.service.recall('Campaign', 'first', 10).matches[0].text, 'first\n')
        self.assertEqual(self.calls().count('update'), 2)

    def test_empty_campaign_and_invalid_input(self):
        self.assertEqual(self.service.recall('Campaign', 'anything', 10).matches, [])
        for query, limit in [('', 10), (' ', 10), ('a\nb', 10), ('x', 0), ('x', 101), ('x', True)]:
            with self.assertRaises(MarkdownError):
                self.service.recall('Campaign', query, limit)

    def test_missing_qmd_reports_actionable_error(self):
        service = RecallService(self.store, QmdRunner(str(self.root / 'missing-qmd'), self.root))
        with self.assertRaisesRegex(MarkdownError, 'QMD is not installed'):
            service.recall('Campaign', 'x', 1)

    def test_process_errors_timeout_and_bad_json(self):
        self.store.insert('Campaign', 'notes', 'first')
        self.service.recall('Campaign', 'first', 1)
        for query, message in [('__failure__', 'fixture failure'), ('__badjson__', 'valid JSON')]:
            with self.assertRaisesRegex(MarkdownError, message):
                self.service.recall('Campaign', query, 1)
        self.runner.timeout = 0.1
        with self.assertRaisesRegex(MarkdownError, 'timed out'):
            self.service.recall('Campaign', '__timeout__', 1)
        self.runner.timeout = 300
        self.assertEqual(self.service.recall('Campaign', 'first', 1).matches[0].text, 'first\n')

    def test_legacy_qmd_empty_result_is_normalized(self):
        self.store.insert('Campaign', 'notes', 'first')
        self.assertEqual(self.service.recall('Campaign', '__legacy_empty__', 1).matches, [])

    def test_bm25_mode_does_not_embed_and_dash_query_is_one_argument(self):
        self.runner.mode = 'search'
        self.store.insert('Campaign', 'notes', '--literal query')
        result = self.service.recall('Campaign', '--literal query', 1)
        self.assertEqual(result.backend, 'qmd:search')
        self.assertEqual(result.matches[0].text, '--literal query\n')
        self.assertEqual(self.calls(), ['update', 'search'])

    def test_failed_refresh_is_retried_and_does_not_mark_index_synced(self):
        self.store.insert('Campaign', 'notes', 'first')
        with patch.object(self.runner, 'refresh', side_effect=MarkdownError('failed')):
            with self.assertRaisesRegex(MarkdownError, 'failed'):
                self.service.recall('Campaign', 'first', 1)
        with sqlite3.connect(self.cache / 'manifest.sqlite') as db:
            self.assertEqual(db.execute("SELECT * FROM state WHERE key='synced'").fetchall(), [])
        self.assertEqual(self.service.recall('Campaign', 'first', 1).matches[0].text, 'first\n')
        self.assertEqual(self.calls(), ['update', 'embed', 'query'])
