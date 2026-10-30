"""TEST DOUBLE ONLY: QMD CLI protocol fixture, without semantic search or models."""
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
assert args[:2] == ['--index', 'campaign'], args
directory = Path.cwd()
config = json.loads((Path(os.environ['QMD_CONFIG_DIR']) / 'campaign.yml').read_text())
chunks = Path(config['collections']['campaign']['path'])
assert chunks == directory / 'chunks'
assert config['collections']['campaign']['pattern'] == '*.md'
assert Path(os.environ['INDEX_PATH']) == directory / 'qmd.sqlite'
assert Path(os.environ['XDG_CACHE_HOME']).is_absolute()
with (directory / 'fixture_calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')

command = args[2]
if command == 'update':
    Path(os.environ['INDEX_PATH']).touch()
elif command == 'embed':
    pass
else:
    assert command in {'query', 'search'}, args
    assert args[3:6] == ['-c', 'campaign', '-n'], args
    assert args[7:9] == ['--json', '--'], args
    query = args[9]
    if query == '__timeout__':
        time.sleep(10)
    elif query == '__badjson__':
        print('not json')
    elif query == '__legacy_empty__':
        print('No results found.')
    elif query == '__failure__':
        print('fixture failure', file=sys.stderr)
        sys.exit(42)
    else:
        limit = int(args[6])
        results = []
        for path in sorted(chunks.glob('*.md')):
            if query == '__all__' or query in path.read_text():
                results.append({'file': f'qmd://campaign/{path.name}', 'score': 0.9,
                                'snippet': 'This fake snippet MUST NOT be used as source text.'})
        print(json.dumps(results[:limit]))
