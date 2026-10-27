"""Exercise the frozen HTTP app and stdio helper without calling a model."""
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
from enoy import __version__

root = Path(__file__).resolve().parent.parent
app = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / 'dist' / 'Enoy'
helper = app / 'Enoy-tools.exe'
event = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}
result = subprocess.run([str(helper), '--module', 'enoy.tools.mcp_server'], input=json.dumps(event) + '\n',
                        capture_output=True, text=True, timeout=20,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
assert result.returncode == 0, result.stderr
response = json.loads(result.stdout.splitlines()[0])
assert response['id'] == 1 and response['result']['serverInfo']['name'] == 'enoy'
data = Path(tempfile.mkdtemp(prefix='smoke-', dir=root / 'build'))
proc = subprocess.Popen([str(app / 'Enoy.exe'), '--no-window', '--data-dir', str(data)],
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
try:
    for _ in range(80):
        marker = data / 'instance.json'
        if marker.exists():
            info = json.loads(marker.read_text(encoding='utf-8'))
            break
        if proc.poll() is not None:
            raise RuntimeError('The frozen app exited before starting its local server.')
        time.sleep(.25)
    else:
        raise RuntimeError('The frozen app did not start within 20 seconds.')
    with urllib.request.urlopen(info['url'], timeout=5) as response:
        html = response.read().decode()
    assert 'mode-indicator' in html and 'workspace-context' in html
    token = re.search(r'name="enoy-token" content="([^"]+)"', html).group(1)
    request = urllib.request.Request(info['url'] + 'api/config', headers={'X-Enoy-Token': token})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.load(response)['config']['chat']['experience'] == 'chatgpt'
    request = urllib.request.Request(info['url'] + 'api/status', headers={'X-Enoy-Token': token})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.load(response)['version'] == __version__, 'Frozen app version does not match its release version.'
    with urllib.request.urlopen(info['url'] + 'static/appearance.js', timeout=5) as response:
        assert 'EnoyAppearance' in response.read().decode()
    request=urllib.request.Request(info['url']+'api/appearance',
        data=json.dumps({'mode':'code','preset':'clay','custom':{'accent':'#dc9c7e'}}).encode(),
        headers={'X-Enoy-Token':token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=5) as response:
        assert json.load(response)['colours']['code']['custom']['accent']=='#dc9c7e'
    with urllib.request.urlopen(info['url'] + 'static/styles.css', timeout=5) as response:
        assert 'data-mode="chat"' in response.read().decode()
    print('Packaged app, UI assets and stdio MCP helper passed smoke checks.')
finally:
    proc.terminate()
    proc.wait(timeout=10)
