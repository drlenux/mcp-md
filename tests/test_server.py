import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from gpt_parser_mcp import __version__
from gpt_parser_mcp.config import Settings
from gpt_parser_mcp.server import create_server
from qmd_fixture import create_qmd_fixture


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base_dir = Path(temporary.name) / 'data'

    async def assert_server_contract(self, client: Client, expected_name: str) -> None:
        tools = await client.list_tools()
        self.assertEqual(
            {tool.name for tool in tools.tools},
            {'health_check', 'new', 'list', 'listTables', 'get', 'insert', 'search',
             'searchPage', 'recall', 'update', 'getAll'},
        )
        self.assertTrue(tools.tools[0].annotations.read_only_hint)
        self.assertIsNotNone(tools.tools[0].output_schema)

        health = await client.call_tool('health_check', {})
        self.assertFalse(health.is_error)
        self.assertEqual(
            health.structured_content,
            {'status': 'ok', 'name': expected_name, 'version': __version__},
        )

        resources = await client.list_resources()
        self.assertEqual([str(resource.uri) for resource in resources.resources], ['server://info'])
        resource = await client.read_resource('server://info')
        self.assertEqual(resource.contents[0].mime_type, 'application/json')
        self.assertEqual(json.loads(resource.contents[0].text), health.structured_content)

        prompts = await client.list_prompts()
        self.assertEqual([prompt.name for prompt in prompts.prompts], ['diagnose_server'])
        prompt = await client.get_prompt('diagnose_server')
        self.assertEqual(prompt.messages[0].role, 'user')
        self.assertIn('health_check', prompt.messages[0].content.text)
        self.assertIn('server://info', prompt.messages[0].content.text)

        listing = await client.call_tool('list')
        self.assertEqual(listing.structured_content, {'result': []})
        for _ in range(2):
            created = await client.call_tool('new', {'name': 'Campaign'})
            self.assertFalse(created.is_error)
        self.assertEqual((await client.call_tool('list')).structured_content, {'result': ['Campaign']})
        table = {'name': 'Campaign', 'table': 'notes'}
        for line in ['first', 'Привет [world]', 'last']:
            inserted = await client.call_tool('insert', {**table, 'line': line})
            self.assertFalse(inserted.is_error)
        page = await client.call_tool('get', {**table, 'lineId': 2, 'countLine': 1})
        self.assertEqual(page.structured_content, {'result': ['Привет [world]']})
        search = await client.call_tool('search', {**table, 'q': '[world]'})
        self.assertEqual(search.structured_content, {'result': ['Привет [world]']})
        tables = await client.call_tool('listTables', {'name': 'Campaign'})
        self.assertEqual(tables.structured_content, {'result': ['notes']})
        found = await client.call_tool('searchPage', {**table, 'q': '', 'limit': 2})
        self.assertFalse(found.is_error)
        first = found.structured_content
        self.assertEqual(first['matches'], [{'lineId': 1, 'text': 'first'}, {'lineId': 2, 'text': 'Привет [world]'}])
        last = await client.call_tool('searchPage', {**table, 'q': '', 'limit': 2, 'cursor': first['nextCursor']})
        self.assertEqual(last.structured_content['matches'], [{'lineId': 3, 'text': 'last'}])
        self.assertIsNone(last.structured_content['nextCursor'])
        updated = await client.call_tool('update', {**table, 'lineId': [1, 3], 'line': 'changed'})
        self.assertFalse(updated.is_error)
        stale = await client.call_tool('searchPage', {**table, 'q': '', 'limit': 2, 'cursor': first['nextCursor']})
        self.assertTrue(stale.is_error)
        self.assertIn('File changed', stale.content[0].text)
        all_lines = await client.call_tool('getAll', table)
        self.assertEqual(all_lines.structured_content, {'result': ['changed', 'Привет [world]', 'changed']})
        invalid = await client.call_tool('update', {**table, 'lineId': [1, 999], 'line': 'bad'})
        self.assertTrue(invalid.is_error)
        self.assertIn('nothing was updated', invalid.content[0].text)
        self.assertEqual((await client.call_tool('getAll', table)).structured_content, all_lines.structured_content)
        invalid_id = await client.call_tool('get', {**table, 'lineId': 0, 'countLine': 1})
        self.assertTrue(invalid_id.is_error)

    async def test_in_memory_contract(self) -> None:
        server = create_server(Settings(server_name='memory-test', base_dir=self.base_dir))
        async with asyncio.timeout(15), Client(server, raise_exceptions=True) as client:
            await self.assert_server_contract(client, 'memory-test')

    async def test_server_instances_do_not_share_dependencies(self) -> None:
        first = create_server(Settings(server_name='first'))
        second = create_server(Settings(server_name='second'))
        async with asyncio.timeout(15), Client(first) as a, Client(second) as b:
            self.assertEqual((await a.call_tool('health_check')).structured_content['name'], 'first')
            self.assertEqual((await b.call_tool('health_check')).structured_content['name'], 'second')

    async def test_configured_result_limit_is_reported_as_tool_error(self) -> None:
        server = create_server(Settings(base_dir=self.base_dir, max_result_bytes=4))
        async with asyncio.timeout(15), Client(server) as client:
            await client.call_tool('new', {'name': 'Campaign'})
            table = {'name': 'Campaign', 'table': 'notes'}
            await client.call_tool('insert', {**table, 'line': 'oversized'})
            for tool, args in [('getAll', table), ('search', {**table, 'q': ''})]:
                result = await client.call_tool(tool, args)
                self.assertTrue(result.is_error)
                self.assertIn('no partial result', result.content[0].text)

    async def test_stdio_from_another_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / 'config'
            config_directory.mkdir()
            env_file = config_directory / '.env'
            env_file.write_text('BASE_DIR=./files\nMCP_SERVER_NAME=stdio-test\n', encoding='utf-8')
            parameters = StdioServerParameters(
                command=sys.executable,
                args=['-m', 'gpt_parser_mcp', '--transport', 'stdio', '--env-file', str(env_file)],
                cwd=directory,
                env={'MCP_LOG_LEVEL': 'WARNING'},
            )
            async with asyncio.timeout(20), Client(parameters) as client:
                await self.assert_server_contract(client, 'stdio-test')
            self.assertTrue((config_directory / 'files' / 'Campaign' / 'notes.md').is_file())

    async def test_streamable_http_contract(self) -> None:
        server = create_server(Settings(server_name='http-test', base_dir=self.base_dir))
        app = server.streamable_http_app(stateless_http=True, json_response=True)
        async with asyncio.timeout(20), app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                transport = streamable_http_client('http://127.0.0.1:8000/mcp', http_client=http)
                async with Client(transport) as client:
                    await self.assert_server_contract(client, 'http-test')

    async def test_recall_through_stdio_with_explicit_cli_test_double(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = create_qmd_fixture(root)
            env_file = root / '.env'
            env_file.write_text(f'BASE_DIR=./data\nMCP_QMD_COMMAND={fixture}\nMCP_QMD_CACHE_HOME=./models\n')
            parameters = StdioServerParameters(
                command=sys.executable,
                args=['-m', 'gpt_parser_mcp', '--env-file', str(env_file)],
                cwd=directory, env={'MCP_LOG_LEVEL': 'WARNING'},
            )
            async with asyncio.timeout(20), Client(parameters) as client:
                await client.call_tool('new', {'name': 'Campaign'})
                await client.call_tool('insert', {'name': 'Campaign', 'table': 'people', 'line': 'Мария хранит ключ.'})
                result = await client.call_tool('recall', {'name': 'Campaign', 'q': 'ключ', 'limit': 2})
                self.assertFalse(result.is_error)
                data = result.structured_content
                self.assertEqual(data['backend'], 'qmd:query')
                self.assertEqual(data['matches'][0]['table'], 'people')
                self.assertEqual(data['matches'][0]['text'], 'Мария хранит ключ.\n')
                self.assertEqual(data['matches'][0]['startLine'], 1)
                invalid = await client.call_tool('recall', {'name': 'Campaign', 'q': 'ключ', 'limit': 0})
                self.assertTrue(invalid.is_error)

    async def test_missing_recall_dependency_does_not_break_file_tools(self) -> None:
        server = create_server(Settings(base_dir=self.base_dir, qmd_command='/nonexistent/qmd'))
        async with asyncio.timeout(15), Client(server) as client:
            await client.call_tool('new', {'name': 'Campaign'})
            result = await client.call_tool('recall', {'name': 'Campaign', 'q': 'key', 'limit': 1})
            self.assertTrue(result.is_error)
            self.assertIn('QMD is not installed', result.content[0].text)
            tables = await client.call_tool('listTables', {'name': 'Campaign'})
            self.assertEqual(tables.structured_content, {'result': []})
