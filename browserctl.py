#!/usr/bin/env python3
"""Private, fail-closed browser task controller."""
import errno, hashlib, json, math, os
from pathlib import Path
import pwd, re, secrets, signal, socket, stat, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request
SCHEMA, PROTOCOL = 1, '2025-03-26'
MAX, HTTP_TIMEOUT, IO_TIMEOUT = 16 * 1024 * 1024, 90, 2
BUILD = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CONFIG_KEYS = {'schema_version', 'hostname', 'user', 'human', 'agent', 'mcp_command'}
PROFILE_KEYS, REF_KEYS = {'edge_exe', 'profile', 'cdp_endpoint'}, {'target', 'startTarget', 'endTarget'}
TASK_KEYS = {'schema_version', 'state', 'owner', 'generation', 'phase', 'snapshot', 'operation', 'receipt', 'reason'}
PHASES = {'NEW', 'LIST', 'SELECT', 'SNAPSHOT', 'ARMED'}
FORBIDDEN = {'browser_tabs', 'browser_get_config', 'browser_close', 'browser_run_code', 'browser_run_code_unsafe'}
TAB_FLOW = {
    'tab_new': ({'NEW'}, 'new', 'LIST', True),
    'tab_list': ({'LIST', 'ARMED'}, 'list', 'SELECT', True),
    'tab_select': ({'SELECT'}, 'select', 'SNAPSHOT', True),
}

class Error(Exception):
    pass

def fail(message):
    raise Error(message)

def require(condition, message):
    if not condition:
        fail(message)

def stop_signal(*_):
    raise KeyboardInterrupt

def strict_load(text, label='JSON'):
    def constant(value):
        fail(f'{label} contains forbidden number {value}')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                fail(f'{label} contains duplicate key {key!r}')
            value[key] = item
        return value
    try:
        value = json.loads(text, parse_constant=constant, object_pairs_hook=pairs)
    except (json.JSONDecodeError, UnicodeError) as exc:
        fail(f'{label} is malformed: {exc}')
    validate_json(value, label)
    return value

def validate_json(value, label='JSON'):
    if isinstance(value, float) and (not math.isfinite(value)):
        fail(f'{label} contains a non-finite number')
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for item in children:
        validate_json(item, label)

def state_dir():
    path = Path(os.getenv('XDG_STATE_HOME', Path.home() / '.local/state')) / 'browserctl'
    path.mkdir(mode=448, parents=True, exist_ok=True)
    require(path.is_dir() and not path.is_symlink(),
            f'unsafe state directory: {path}')
    info = path.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 448,
            f'state directory must be owned by the user and mode 0700: {path}')
    return path

def socket_path():
    root = os.getenv('XDG_RUNTIME_DIR')
    require(root, 'XDG_RUNTIME_DIR is required')
    path = Path(root)
    info = path.stat()
    require(path.is_dir() and not path.is_symlink() and
            info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 63,
            'XDG_RUNTIME_DIR must be a private user directory')
    return path / 'browserctl.sock'

def remove_stale_socket(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == 384,
            f'unsafe existing socket path: {path}')
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(IO_TIMEOUT)
        try:
            status = probe.connect_ex(str(path))
        except (OSError, socket.timeout) as exc:
            fail(f'existing socket liveness is ambiguous: {exc}')
    if status == 0:
        fail('another browserctl daemon is already running')
    require(status in {errno.ECONNREFUSED, errno.ENOENT},
            f'existing socket liveness is ambiguous: errno {status}')
    path.unlink(missing_ok=True)

def read_private(path, label):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        fail(f'cannot open {label}: {exc}')
    info = os.fstat(fd)
    if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == 384):
        os.close(fd)
        fail(f'{label} must be owned by the user and mode 0600')
    try:
        with os.fdopen(fd, encoding='utf-8') as stream:
            return strict_load(stream.read(MAX + 1), label)
    except (OSError, UnicodeError) as exc:
        fail(f'cannot read {label}: {exc}')

def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def write_task(value):
    validate_task(value)
    directory = state_dir()
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.task.', dir=directory)
        os.fchmod(fd, 384)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / 'task.json')
        sync_dir(directory)
    except OSError as exc:
        fail(f'cannot durably write task journal: {exc}')
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)

def remove_task():
    try:
        (state_dir() / 'task.json').unlink()
        sync_dir(state_dir())
    except OSError as exc:
        fail(f'cannot durably remove task journal: {exc}')

def endpoint(value, label):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        fail(f'{label} is invalid: {exc}')
    require(parsed.scheme == 'http' and
            parsed.hostname in {'127.0.0.1', '::1'} and port is not None and
            1 <= port <= 65535 and not any((parsed.path, parsed.query,
            parsed.fragment, parsed.username, parsed.password)),
            f'{label} must be canonical numeric-loopback HTTP with a port')
    host = f'[{parsed.hostname}]' if parsed.hostname == '::1' else parsed.hostname
    require(value == f'http://{host}:{port}', f'{label} is not canonical')
    return parsed

def load_config():
    path = Path(os.getenv('XDG_CONFIG_HOME', Path.home() / '.config')) / 'browserctl/host.json'
    value = read_private(path, 'host config')
    require(isinstance(value, dict) and set(value) == CONFIG_KEYS,
            'host config has an unsupported schema')
    require(value['schema_version'] == SCHEMA and
            value['hostname'] == socket.gethostname() and
            value['user'] == pwd.getpwuid(os.getuid()).pw_name,
            'host config identity does not match this host and user')
    for name in ('human', 'agent'):
        profile = value[name]
        require(isinstance(profile, dict) and set(profile) == PROFILE_KEYS,
                f'host config {name} profile has an unsupported schema')
        for key in ('edge_exe', 'profile'):
            if not isinstance(profile[key], str) or not Path(profile[key]).is_absolute():
                fail(f'host config {name}.{key} must be absolute')
        endpoint(profile['cdp_endpoint'], f'host config {name}.cdp_endpoint')
    require(Path(value['human']['profile']).resolve() !=
            Path(value['agent']['profile']).resolve() and
            value['human']['cdp_endpoint'] != value['agent']['cdp_endpoint'],
            'human and agent browser identities are ambiguous')
    command = value['mcp_command']
    require(isinstance(command, list) and len(command) == 3 and
            command[1:] == ['--yes', '@playwright/mcp@0.0.78'] and
            isinstance(command[0], str) and Path(command[0]).is_absolute(),
            'mcp_command must be [ABS_NPX, "--yes", "@playwright/mcp@0.0.78"]')
    require(Path(command[0]).is_file() and os.access(command[0], os.X_OK),
            'mcp_command executable is missing or not executable')
    return value

def display_profile(config):
    try:
        result = subprocess.run(['xrandr', '--query'], text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f'cannot inspect displays: {exc}')
    require(result.returncode == 0, f'xrandr failed: {result.stderr.strip()}')
    connected = [match.group(1) for line in result.stdout.splitlines() if (match := re.match('^(\\S+)\\s+connected(?:\\s|$)', line))]
    require(connected, 'xrandr reported no connected display')
    internal = ('eDP', 'LVDS', 'DSI')
    name = 'agent' if any(not output.startswith(internal) for output in connected) else 'human'
    return name, config[name]

def process_info(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'state': fields[0], 'ppid': int(fields[1]), 'pgrp': int(fields[2]),
                'session': int(fields[3]), 'start': fields[19]}
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None

def fd_targets(pid):
    targets = []
    try:
        entries = Path(f'/proc/{pid}/fd').iterdir()
        for entry in entries:
            try:
                targets.append(os.readlink(entry))
            except OSError:
                pass
    except OSError:
        pass
    return targets

def edge_identity(config):
    name, profile = display_profile(config)
    profile_path = Path(profile['profile'])
    if not Path(profile['edge_exe']).is_file() or not profile_path.is_dir():
        fail(f'selected {name} Edge executable or profile is missing')
    try:
        lock = os.readlink(profile_path / 'SingletonLock')
    except OSError as exc:
        fail(f'cannot read selected profile SingletonLock: {exc}')
    match = re.fullmatch(re.escape(config['hostname']) + r'-(\d+)', lock)
    if not match:
        fail('selected profile SingletonLock is malformed')
    pid = int(match.group(1))
    info = process_info(pid)
    try:
        owner = Path(f'/proc/{pid}').stat().st_uid
        exe = os.readlink(f'/proc/{pid}/exe')
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
        argv = [item.decode('utf-8') for item in argv]
    except (OSError, UnicodeError) as exc:
        fail(f'cannot inspect selected Edge process: {exc}')
    if owner != os.getuid() or not info or info['state'] == 'Z' or exe != profile['edge_exe']:
        fail('selected Edge PID owner, liveness, or executable is wrong')
    command = '\0'.join(argv)
    boundary = r'(?:^|[ \0])'
    if re.search(boundary + r'--type=', command):
        fail('selected Edge PID is not a top-level browser process')
    parsed = endpoint(profile['cdp_endpoint'], 'selected CDP endpoint')
    if not re.search(boundary + f'--remote-debugging-port={parsed.port}' + r'(?:$|[ \0])', command):
        fail(f'selected Edge PID lacks --remote-debugging-port={parsed.port}')
    user_dirs = re.findall(boundary + r'--user-data-dir=([^ \0]+)', command)
    if user_dirs and user_dirs != [profile['profile']]:
        fail('selected Edge PID has the wrong user-data-dir')
    targets = fd_targets(pid)
    other = config['agent' if name == 'human' else 'human']['profile'] + '/'
    if not any(target.startswith(profile['profile'] + '/') for target in targets) or any(target.startswith(other) for target in targets):
        fail('selected Edge file descriptors do not prove the exact profile')
    inode = listener_inode(parsed.hostname, parsed.port)
    if f'socket:[{inode}]' not in targets:
        fail('selected Edge PID does not own the exact CDP listener')
    return {'selector': name, 'pid': pid, 'start': info['start'], 'exe': exe,
            'profile': profile['profile'], 'cdp': profile['cdp_endpoint'],
            'listener': inode}

def descendant(pid, root):
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == root:
            return True
        seen.add(pid)
        info = process_info(pid)
        if not info:
            return False
        pid = info['ppid']
    return False

def group_members(group):
    members = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        info = process_info(pid)
        if not info or info['state'] == 'Z' or info['session'] != group:
            continue
        try:
            owner = entry.stat().st_uid
        except FileNotFoundError:
            continue
        require(owner == os.getuid(), f'process group {group} has a foreign owner')
        members[pid] = info['start']
    return members

def signal_members(group, members, signum):
    for pid, start in members.items():
        info = process_info(pid)
        if not info:
            continue
        try:
            owner = Path(f'/proc/{pid}').stat().st_uid
        except FileNotFoundError:
            continue
        require(info['start'] == start and info['session'] == group and
                owner == os.getuid(),
                'MCP survivor identity changed; refusing to signal')
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

def wait_group_empty(group):
    deadline = time.monotonic() + IO_TIMEOUT
    while True:
        members = group_members(group)
        if not members or time.monotonic() >= deadline:
            return members
        time.sleep(.02)

def listener_owned_by(root, start, host, port):
    inode = listener_inode(host, port)
    target = f'socket:[{inode}]'
    owners = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or target not in fd_targets(int(entry.name)):
            continue
        pid = int(entry.name)
        info = process_info(pid)
        try:
            owner = entry.stat().st_uid
        except FileNotFoundError:
            continue
        require(info and info['state'] != 'Z' and owner == os.getuid() and
                descendant(pid, root),
                'MCP listener is not owned by its exact child tree')
        owners[pid] = info['start']
    require(owners, 'MCP listener owner is unprovable')
    leader = process_info(root)
    require(leader and leader['start'] == start and leader['pgrp'] == root and
            leader['session'] == root, 'MCP child identity changed')
    for pid, owner_start in owners.items():
        info = process_info(pid)
        require(info and info['start'] == owner_start and
                target in fd_targets(pid) and descendant(pid, root),
                'MCP listener ownership changed during validation')
    return inode

def reject_foreign_controller(cdp, allowed_root=0):
    markers = ('playwright-mcp', '@playwright/mcp', 'mcp_client.py',
               'start-controller.sh', 'controller.py')
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().decode('utf-8')
        except (OSError, UnicodeError):
            continue
        if cdp in argv and any(marker in argv for marker in markers) and not descendant(int(entry.name), allowed_root):
            fail(f'foreign or legacy browser controller PID {entry.name} uses selected CDP')

def check_state_files():
    unexpected = {path.name for path in state_dir().iterdir()} - {'task.json'}
    if unexpected:
        fail('legacy or unexpected browserctl state requires operator archival: ' +
             ', '.join(sorted(unexpected)))

def validate_task(value):
    if not isinstance(value, dict) or set(value) != TASK_KEYS or value.get('schema_version') != SCHEMA or value.get('state') not in {'READY', 'RUNNING', 'RESULT', 'RECOVERY'} or value.get('phase') not in PHASES or not isinstance(value.get('owner'), str) or not 0 < len(value['owner']) <= 200 or not re.fullmatch('[0-9a-f]{32}', value.get('generation', '')) or type(value.get('snapshot')) is not bool:
        fail('task journal is malformed or unsupported')
    if value['state'] == 'READY':
        good = value['operation'] is value['receipt'] is value['reason'] is None
    elif value['state'] == 'RUNNING':
        good = set(value['operation'] or {}) == {'id', 'method'} and value['receipt'] is value['reason'] is None
    elif value['state'] == 'RESULT':
        receipt = value['receipt'] or {}
        good = value['operation'] is value['reason'] is None and set(receipt) == {'id', 'request_id', 'stdout', 'stderr', 'exit_code', 'success', 'post'} and isinstance(receipt['id'], str) and isinstance(receipt['request_id'], str) and isinstance(receipt['stdout'], str) and isinstance(receipt['stderr'], str) and receipt['exit_code'] in {0, 1} and type(receipt['success']) is bool and isinstance(receipt['post'], dict)
    else:
        good = value['operation'] is value['receipt'] is None and bool(value['reason'])
    if not good:
        fail('task journal fields disagree with its state')
    return value

def read_task(missing=True):
    path = state_dir() / 'task.json'
    try:
        return validate_task(read_private(path, 'task journal'))
    except Error:
        if missing and (not path.exists()):
            return None
        raise

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, *_):
        return None
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def json_bytes(value):
    try:
        body = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        fail(f'request is not strict JSON: {exc}')
    require(len(body) <= MAX, 'request is too large')
    return body

def decode_mcp(body, content_type):
    require(len(body) <= MAX, 'MCP response is too large')
    try:
        text = body.decode()
    except UnicodeDecodeError as exc:
        fail(f'MCP response is not UTF-8: {exc}')
    media = content_type.split(';', 1)[0].lower()
    if media == 'application/json':
        payloads = [text] if text.strip() else []
    elif media == 'text/event-stream':
        payloads, event = ([], [])
        for line in text.splitlines() + ['']:
            if line.startswith('data:'):
                event.append(line[5:].lstrip())
            elif not line and event:
                payloads.append('\n'.join(event))
                event = []
    else:
        fail(f'MCP returned unsupported Content-Type {content_type!r}')
    require(len(payloads) == 1, 'MCP returned empty or ambiguous JSON')
    value = strict_load(payloads[0], 'MCP response')
    require(isinstance(value, dict), 'MCP response is not an object')
    return value

def valid_session(value):
    return isinstance(value, str) and re.fullmatch(r'[!-~]{1,256}', value) is not None

class MCP:
    def __init__(self, config, identity):
        self.config = config
        self.profile = config[identity['selector']]
        self.child = None
        self.child_start = None
        self.session = None
        self.next_id = 1
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}/mcp'

    def start(self):
        authority = f'127.0.0.1:{self.port}'
        argv = self.config['mcp_command'] + ['--cdp-endpoint', self.profile['cdp_endpoint'], '--shared-browser-context', '--caps=config', '--host', '127.0.0.1', '--allowed-hosts', authority, '--port', str(self.port)]
        env = {key: value for key, value in os.environ.items() if not key.startswith('PLAYWRIGHT_MCP_')}
        env['PATH'] = str(Path(argv[0]).parent) + ':/usr/local/bin:/usr/bin:/bin'
        env['PLAYWRIGHT_MCP_PING_TIMEOUT_MS'] = '0'
        try:
            self.child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            fail(f'cannot start pinned MCP command: {exc}')
        info = process_info(self.child.pid)
        self.child_start = info['start'] if info else None
        require(info and info['pgrp'] == self.child.pid and
                info['session'] == self.child.pid,
                'MCP child is not its exact process-group and session leader')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                fail(f'MCP exited during startup ({self.child.returncode})')
            with socket.socket() as probe:
                probe.settimeout(0.05)
                if probe.connect_ex(('127.0.0.1', self.port)) == 0:
                    listener_owned_by(
                        self.child.pid, self.child_start, '127.0.0.1', self.port)
                    self.initialize()
                    return
            time.sleep(0.05)
        fail('MCP did not open its loopback listener')

    def post(self, message, initializing=False):
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if self.session:
            headers.update({'Mcp-Session-Id': self.session, 'Mcp-Protocol-Version': PROTOCOL})
        request = urllib.request.Request(self.url, json_bytes(message), headers, method='POST')
        try:
            with HTTP.open(request, timeout=HTTP_TIMEOUT) as response:
                require(response.geturl() == self.url and
                        response.status in {200, 202},
                        'MCP returned a redirect or unexpected status')
                body = response.read(MAX + 1)
                returned_session = response.headers.get('Mcp-Session-Id')
                content_type = response.headers.get('Content-Type', '')
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode(errors='replace')
            fail(f'MCP HTTP {exc.code}: {detail}')
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            fail(f'MCP request failed: {exc}')
        if initializing:
            require(valid_session(returned_session),
                    'MCP initialize omitted its session')
            self.session = returned_session
        elif returned_session != self.session and not (response.status == 202 and returned_session is None):
            fail('MCP response changed or omitted its session')
        if response.status == 202:
            require(not body, 'MCP notification response was not empty')
            return None
        return decode_mcp(body, content_type)

    def initialize(self):
        identifier = self.next_id
        response = self.post({'jsonrpc': '2.0', 'id': identifier, 'method': 'initialize', 'params': {'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'browserctl', 'version': '1'}}}, True)
        result = self.result(response, identifier)
        require(isinstance(result, dict) and
                result.get('protocolVersion') == PROTOCOL,
                'MCP initialize result is incompatible')
        self.next_id += 1
        self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
        tools = self.call_raw('tools/list', {})
        require(any(tool.get('name') == 'browser_tabs'
                    for tool in tools.get('tools', [])), 'MCP omits browser_tabs')

    @staticmethod
    def result(response, identifier):
        require(isinstance(response, dict) and
                response.get('jsonrpc') == '2.0' and
                response.get('id') == identifier and
                (('result' in response) != ('error' in response)) and
                set(response) == {'jsonrpc', 'id',
                                  'result' if 'result' in response else 'error'},
                'MCP response correlation or envelope is malformed')
        if 'error' in response:
            error = response['error']
            if not isinstance(error, dict) or not {'code', 'message'} <= set(error) or type(error['code']) is not int or (not isinstance(error['message'], str)):
                fail('MCP error is malformed')
            raise RemoteError(json.dumps(error, sort_keys=True))
        return response['result']

    def call_raw(self, method, params):
        require(self.child and self.child.poll() is None, 'MCP process died')
        identifier = self.next_id
        self.next_id += 1
        return self.result(self.post({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params}), identifier)

    def tools(self):
        result = self.call_raw('tools/list', {})
        require(isinstance(result, dict) and
                isinstance(result.get('tools'), list),
                'MCP tools/list result is malformed')
        result['tools'] = [tool for tool in result['tools'] if isinstance(tool, dict) and tool.get('name') not in FORBIDDEN]
        return result

    def call(self, name, arguments):
        result = self.call_raw('tools/call', {'name': name, 'arguments': arguments})
        require(isinstance(result, dict) and
                isinstance(result.get('content'), list) and
                all(valid_block(block) for block in result['content']) and
                ('isError' not in result or type(result['isError']) is bool),
                'MCP tool result is malformed')
        if result.get('isError'):
            raise RemoteError(json.dumps(result, sort_keys=True))
        return result

    def close(self):
        if not self.child:
            return
        pid = self.child.pid
        leader = process_info(pid)
        if leader and (leader['start'] != self.child_start or
                       leader['pgrp'] != pid or leader['session'] != pid):
            fail('MCP group-leader identity changed; refusing to signal')
        members = group_members(pid)
        if members:
            signal_members(pid, members, signal.SIGTERM)
            survivors = wait_group_empty(pid)
            if survivors:
                deadline = time.monotonic() + IO_TIMEOUT
                while survivors and time.monotonic() < deadline:
                    signal_members(pid, survivors, signal.SIGKILL)
                    time.sleep(.02)
                    survivors = group_members(pid)
                require(not survivors,
                        'MCP process group survived SIGKILL')
        try:
            self.child.wait(IO_TIMEOUT)
        except subprocess.TimeoutExpired:
            if group_members(pid):
                fail('MCP process group did not exit within its bound')
            else:
                fail('MCP child did not become waitable within its bound')

class RemoteError(Error):
    pass

def valid_block(value):
    if not isinstance(value, dict) or value.get('type') == 'text':
        return isinstance(value, dict) and isinstance(value.get('text'), str)
    if value.get('type') != 'image' or value.get('mimeType') not in {'image/png', 'image/jpeg'} or not isinstance(value.get('data'), str):
        return False
    return bool(value['data'])

def has_ref(value):
    if isinstance(value, dict):
        return any((key in REF_KEYS and isinstance(item, str) and re.fullmatch('(?:f\\d+)?e\\d+', item) or has_ref(item) for key, item in value.items()))
    return isinstance(value, list) and any((has_ref(item) for item in value))

def new_task(owner, generation):
    if not isinstance(owner, str) or not 0 < len(owner) <= 200 or any((ord(char) < 32 or ord(char) == 127 for char in owner)):
        fail('OWNER must be 1-200 printable characters')
    return {'schema_version': SCHEMA, 'state': 'READY', 'owner': owner,
            'generation': generation, 'phase': 'NEW', 'snapshot': False,
            'operation': None, 'receipt': None, 'reason': None}

def recovery(task, reason):
    task.update({'state': 'RECOVERY', 'operation': None, 'receipt': None, 'snapshot': False, 'reason': reason[:1000]})
    write_task(task)

class Daemon:
    def __init__(self):
        self.config = load_config()
        self.generation = secrets.token_hex(16)
        self.identity = None
        self.mcp = None
        self.server = None

    def reconcile(self):
        task = read_task()
        if not task or task['generation'] == self.generation:
            return
        if task['state'] in {'READY', 'RUNNING'}:
            recovery(task, 'daemon generation changed; uncertain work is never replayed')

    def require_ready(self):
        task = read_task(False)
        if task['state'] == 'RESULT':
            fail('result pending; run result')
        if task['state'] != 'READY':
            fail('task requires recovery')
        if task['generation'] != self.generation:
            recovery(task, 'daemon generation changed')
            fail('task requires recovery')
        return task

    def validate_identity(self, task=None):
        try:
            current = edge_identity(self.config)
            if current != self.identity:
                fail('selected display or Edge identity changed')
        except Error as exc:
            if task:
                recovery(task, f'browser identity drift: {exc}')
            raise

    def dispatch(self, task, request_id, method, action, post,
                 clear_snapshot=False):
        self.validate_identity(task)
        if clear_snapshot:
            task['snapshot'] = False
        task.update({'state': 'RUNNING', 'operation': {'id': request_id, 'method': method}})
        write_task(task)
        success = False
        try:
            value = action()
            stdout, stderr, code = (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n', '', 0)
            success = True
        except RemoteError as exc:
            stdout, stderr, code = ('', f'browserctl: {exc}\n', 1)
        except Error as exc:
            recovery(task, str(exc))
            raise
        receipt = {'id': secrets.token_hex(16), 'request_id': request_id,
                   'stdout': stdout, 'stderr': stderr, 'exit_code': code,
                   'success': success, 'post': post if success else {}}
        try:
            json_bytes(receipt)
        except Error:
            reason = (f'operation {method!r} completed but its result exceeded '
                      'the durable limit; do not replay')
            recovery(task, reason)
            fail(reason)
        task.update({'state': 'RESULT', 'operation': None, 'receipt': receipt})
        write_task(task)
        return receipt

    def snapshot(self):
        result = self.mcp.call('browser_snapshot', {})
        if not any(block.get('type') == 'image' or
                   block.get('type') == 'text' and block.get('text', '').strip()
                   for block in result['content']):
            raise RemoteError('browser_snapshot returned no usable content')
        return result

    def tab_command(self, command, args, task, request_id):
        expected, action, next_phase, clears = TAB_FLOW[command]
        if task['phase'] not in expected:
            fail(f'tab {action} is required in phase {" or ".join(sorted(expected))}')
        arguments = {'action': action}
        if command == 'tab_select':
            if len(args) != 1 or type(args[0]) is not int or args[0] < 0:
                fail('tab select requires a nonnegative INDEX')
            arguments['index'] = args[0]
        else:
            require(not args, f'tab {action} takes no arguments')
        return self.dispatch(
            task, request_id, f'tab {action}',
            lambda: self.mcp.call('browser_tabs', arguments),
            {'phase': next_phase}, clears)

    def command(self, request):
        if not isinstance(request, dict) or set(request) != {'id', 'command', 'args', 'build'} or (not isinstance(request['id'], str)) or (not isinstance(request['command'], str)) or (not isinstance(request['args'], list)) or (request['build'] != BUILD):
            fail('client request schema is malformed')
        rid, command, args = (request['id'], request['command'], request['args'])
        if command == 'status':
            task = read_task()
            return ({'build': BUILD, 'pid': os.getpid(), 'generation': self.generation,
                     'state': 'IDLE' if task is None else task['state'],
                     'owner': None if task is None else task['owner'],
                     'phase': None if task is None else task['phase']}, None)
        if command == 'task_begin':
            if len(args) != 1 or read_task() is not None:
                fail('task begin requires IDLE and exactly one OWNER')
            self.validate_identity()
            task = new_task(args[0], self.generation)
            write_task(task)
            return ({'state': 'READY', 'owner': task['owner']}, None)
        if command == 'result':
            require(not args, 'result takes no arguments')
            task = read_task(False)
            if task['state'] != 'RESULT':
                fail('no result is pending')
            return (task['receipt'], task['receipt']['id'])
        if command == 'task_recover':
            require(not args, 'task recover takes no arguments')
            task = read_task(False)
            if task['state'] != 'RECOVERY':
                fail('task is not in RECOVERY')
            if task['generation'] == self.generation:
                fail('restart the daemon before recovery')
            self.validate_identity()
            task.update({'state': 'READY', 'generation': self.generation,
                         'phase': 'LIST', 'snapshot': False, 'reason': None})
            write_task(task)
            return ({'state': 'READY', 'next': 'tab list, then tab select INDEX'}, None)
        if command == 'task_end':
            require(not args, 'task end takes no arguments')
            task = self.require_ready()
            remove_task()
            return ({'state': 'IDLE', 'owner': task['owner']}, None)
        task = self.require_ready()
        if command in TAB_FLOW:
            receipt = self.tab_command(command, args, task, rid)
        elif command == 'tools':
            require(not args, 'tools takes no arguments')
            receipt = self.dispatch(task, rid, 'tools', self.mcp.tools, {})
        elif command == 'call':
            if len(args) != 2 or not isinstance(args[0], str) or (not args[0]) or (not isinstance(args[1], dict)):
                fail('call requires TOOL and a JSON object')
            name, arguments = args
            if name in FORBIDDEN or 'unsafe' in name:
                fail(f'tool {name!r} is forbidden')
            if name == 'browser_snapshot':
                if arguments or task['phase'] not in {'SNAPSHOT', 'ARMED'}:
                    fail('browser_snapshot is required after tab select')
                receipt = self.dispatch(
                    task, rid, 'call browser_snapshot', self.snapshot,
                    {'phase': 'ARMED', 'snapshot': True})
                return (receipt, receipt['id'])
            if task['phase'] != 'ARMED':
                fail('complete new/list/select/snapshot before general calls')
            if has_ref(arguments) and not task['snapshot']:
                fail('snapshot reference requires a fresh browser_snapshot')
            receipt = self.dispatch(
                task, rid, f'call {name}',
                lambda: self.mcp.call(name, arguments), {}, True)
        else:
            fail('unknown daemon command')
        return (receipt, receipt['id'])

    def acknowledge(self, receipt_id):
        task = read_task(False)
        if task['state'] != 'RESULT' or task['receipt']['id'] != receipt_id:
            fail('result acknowledgement mismatch')
        if task['generation'] != self.generation:
            recovery(task, 'result belonged to an old daemon generation')
            return
        if task['receipt']['success']:
            task.update(task['receipt']['post'])
        task.update({'state': 'READY', 'receipt': None})
        write_task(task)

    def serve(self):
        for signum in (signal.SIGTERM, signal.SIGINT): signal.signal(signum, stop_signal)
        verify_service(starting=True)
        check_state_files()
        self.identity = edge_identity(self.config)
        reject_foreign_controller(self.identity['cdp'])
        self.mcp = MCP(self.config, self.identity)
        path = socket_path()
        remove_stale_socket(path)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        os.chmod(path, 384)
        self.server.listen(8)
        self.server.settimeout(0.2)
        try:
            self.mcp.start()
            self.reconcile()
            task = read_task()
            self.validate_identity(task if task and task['state'] != 'RESULT' else None)
            notify_ready()
            while True:
                if self.mcp.child.poll() is not None:
                    task = read_task()
                    if task and task['state'] != 'RESULT':
                        recovery(task, 'MCP process died')
                    fail('MCP process died')
                try:
                    connection, _ = self.server.accept()
                except socket.timeout:
                    continue
                with connection:
                    request = None
                    try:
                        connection.settimeout(IO_TIMEOUT)
                        request = recv_message(connection)
                        value, receipt_id = self.command(request)
                        send_message(connection, {'id': request['id'], 'ok': True, 'value': value, 'build': BUILD})
                        if receipt_id:
                            connection.settimeout(IO_TIMEOUT)
                            ack = recv_message(connection)
                            if ack != {'ack': receipt_id}:
                                fail('client acknowledgement mismatch')
                            self.acknowledge(receipt_id)
                            send_message(connection, {'acked': receipt_id, 'build': BUILD})
                    except (Error, OSError, socket.timeout) as exc:
                        try:
                            send_message(connection, {'id': request.get('id') if isinstance(request, dict) else None, 'ok': False, 'error': str(exc), 'build': BUILD})
                        except OSError:
                            pass
        finally:
            self.mcp.close()
            self.server.close()
            path.unlink(missing_ok=True)

def send_message(stream, value):
    body = json_bytes(value)
    if b'\n' in body:
        fail('protocol message contains a newline')
    stream.sendall(body + b'\n')

def recv_message(stream):
    body = bytearray()
    while len(body) <= MAX:
        chunk = stream.recv(min(65536, MAX + 1 - len(body)))
        if not chunk:
            fail('protocol peer disconnected')
        body.extend(chunk)
        if b'\n' in chunk:
            line, rest = bytes(body).split(b'\n', 1)
            if rest:
                fail('protocol peer sent trailing data')
            try:
                text = line.decode('utf-8')
            except UnicodeDecodeError as exc:
                fail(f'protocol message is not UTF-8: {exc}')
            value = strict_load(text, 'protocol message')
            return value
    fail('protocol message is too large')

def client(command, args, quiet=False):
    request_id = secrets.token_hex(16)
    request = {'id': request_id, 'command': command, 'args': args, 'build': BUILD}
    stream = socket.socket(socket.AF_UNIX)
    try:
        stream.settimeout(IO_TIMEOUT)
        stream.connect(str(socket_path()))
        send_message(stream, request)
        stream.settimeout(HTTP_TIMEOUT + IO_TIMEOUT)
        response = recv_message(stream)
        if not isinstance(response, dict) or set(response) not in ({'id', 'ok', 'value', 'build'}, {'id', 'ok', 'error', 'build'}) or response.get('build') != BUILD or (response.get('ok') not in {True, False}):
            fail('daemon response schema is malformed')
        if response['id'] != request_id:
            fail('daemon response correlation mismatch')
        if not response['ok']:
            fail(response['error'])
        value = response['value']
        if command in {'tab_new', 'tab_list', 'tab_select', 'tools', 'call', 'result'}:
            if not isinstance(value, dict) or not {'id', 'stdout', 'stderr', 'exit_code'} <= set(value):
                fail('daemon result receipt is malformed')
            sys.stdout.write(value['stdout'])
            sys.stderr.write(value['stderr'])
            sys.stdout.flush()
            sys.stderr.flush()
            send_message(stream, {'ack': value['id']})
            stream.settimeout(IO_TIMEOUT)
            if recv_message(stream) != {'acked': value['id'], 'build': BUILD}:
                fail('daemon acknowledgement confirmation mismatch')
            return value['exit_code']
        if quiet:
            return value
        print(json.dumps(value, sort_keys=True))
        return 0
    except OSError as exc:
        fail(f'cannot contact stable browserctl daemon: {exc}')
    finally:
        stream.close()

def listener_inode(host, port):
    found = []
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            address, raw_port = fields[1].rsplit(':', 1)
            if int(raw_port, 16) == port and fields[3] == '0A':
                found.append((table, address, fields[9]))
    expected = ('/proc/net/tcp', '0100007F') if host == '127.0.0.1' else ('/proc/net/tcp6', '00000000000000000000000001000000')
    exact = [inode for table, address, inode in found if (table, address) == expected]
    if len(found) != 1 or len(exact) != 1:
        fail('CDP port is not an exact sole numeric-loopback listener')
    return exact[0]

def verify_service(starting=False):
    source = Path(__file__).with_name('browserctl-controller.service')
    config_root = Path(os.getenv('XDG_CONFIG_HOME', Path.home() / '.config'))
    installed = config_root / 'systemd/user/browserctl-controller.service'
    try:
        if source.read_bytes() != installed.read_bytes():
            fail('installed service is not byte-identical to the repository unit')
        mode = stat.S_IMODE(installed.stat().st_mode)
    except OSError as exc:
        fail(f'cannot verify installed service: {exc}')
    if installed.stat().st_uid != os.getuid() or mode != 420:
        fail('installed service must be owned by the user and mode 0644')
    result = subprocess.run(['systemctl', '--user', 'show', 'browserctl-controller.service', '-p', 'FragmentPath', '-p', 'UnitFileState', '-p', 'ActiveState', '-p', 'MainPID'], text=True, capture_output=True, timeout=10)
    if result.returncode:
        fail(f'cannot inspect stable service: {result.stderr.strip()}')
    fields = dict((line.split('=', 1) for line in result.stdout.splitlines() if '=' in line))
    allowed = {'activating'} if starting else {'active', 'inactive'}
    if fields.get('FragmentPath') != str(installed) or fields.get('UnitFileState') != 'static' or fields.get('ActiveState') not in allowed:
        fail('service is transient, legacy, enabled, or loaded from the wrong path')
    try:
        main_pid = int(fields['MainPID'])
    except (KeyError, ValueError):
        fail('service MainPID is malformed')
    expected_argv = [str(Path(__file__).resolve()).encode(), b'daemon']
    if starting:
        argv = Path('/proc/self/cmdline').read_bytes().split(b'\0')[:-1]
        if not os.getenv('NOTIFY_SOCKET') or main_pid != os.getpid() or argv[1:] != expected_argv:
            fail('daemon is not the exact stable service MainPID')
        return {'active': False, 'fragment': str(installed), 'build': BUILD,
                'main_pid': main_pid}
    active = fields.get('ActiveState') == 'active'
    if active:
        try:
            status = client('status', [], True)
            argv = Path(f'/proc/{main_pid}/cmdline').read_bytes().split(b'\x00')[:-1]
        except (OSError, ValueError, KeyError) as exc:
            fail(f'cannot verify active implementation: {exc}')
        if status.get('build') != BUILD or status.get('pid') != main_pid or argv[1:] != expected_argv:
            fail('active service does not run this exact implementation')
    return {'active': active, 'fragment': str(installed), 'build': BUILD,
            'main_pid': main_pid}

def notify_ready():
    address = os.environ['NOTIFY_SOCKET']
    if address.startswith('@'):
        address = '\0' + address[1:]
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        notify.connect(address)
        notify.sendall(b'READY=1\nSTATUS=browserctl warm and validated')
    except OSError as exc:
        fail(f'cannot notify stable service readiness: {exc}')
    finally:
        notify.close()

def doctor():
    config = load_config()
    check_state_files()
    service = verify_service()
    identity = edge_identity(config)
    reject_foreign_controller(identity['cdp'],
                              service['main_pid'] if service['active'] else 0)
    print(json.dumps({'ok': True, 'edge_pid': identity['pid'],
                      'profile': identity['profile'], 'service': service},
                     sort_keys=True))

SIMPLE_CLI = {
    ('status',): ('status', []), ('task', 'recover'): ('task_recover', []),
    ('task', 'end'): ('task_end', []), ('tab', 'new'): ('tab_new', []),
    ('tab', 'list'): ('tab_list', []), ('tools',): ('tools', []),
    ('result',): ('result', [])
}

def parse_cli(argv):
    if argv == ['daemon']:
        Daemon().serve()
        return 0
    if argv == ['doctor']:
        doctor()
        return 0
    if tuple(argv) in SIMPLE_CLI:
        return client(*SIMPLE_CLI[tuple(argv)])
    if len(argv) == 3 and argv[:2] == ['task', 'begin']:
        return client('task_begin', [argv[2]])
    if len(argv) == 3 and argv[:2] == ['tab', 'select']:
        if not argv[2].isdigit():
            fail('INDEX must be a nonnegative integer')
        return client('tab_select', [int(argv[2])])
    if len(argv) == 3 and argv[0] == 'call':
        arguments = strict_load(argv[2], 'tool arguments')
        if not isinstance(arguments, dict):
            fail('tool arguments must be a JSON object')
        return client('call', [argv[1], arguments])
    fail('usage: browserctl.py doctor|status|task begin OWNER|task recover|task end|tab new|tab list|tab select INDEX|tools|call TOOL JSON|result')
if __name__ == '__main__':
    try:
        raise SystemExit(parse_cli(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Error as exc:
        print(f'browserctl: {exc}', file=sys.stderr)
        raise SystemExit(1)
