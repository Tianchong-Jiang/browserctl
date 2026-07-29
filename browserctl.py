#!/usr/bin/env python3
"""Small controller for one warm Playwright MCP browser session."""
import hashlib, json, math, os
from pathlib import Path
import pwd, re, secrets, signal, socket, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request
SCHEMA, PROTOCOL = 1, '2025-03-26'
MAX, HTTP_TIMEOUT, IO_TIMEOUT = 16 * 1024 * 1024, 90, 2
BUILD = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CONFIG_KEYS = {'schema_version', 'hostname', 'user', 'human', 'agent', 'mcp_command'}
PROFILE_KEYS = {'edge_exe', 'profile', 'cdp_endpoint'}
TASK_KEYS = {'schema_version', 'state', 'owner', 'generation',
             'operation', 'receipt', 'reason'}

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

    def floating(value):
        number = float(value)
        if not math.isfinite(number):
            fail(f'{label} contains non-finite number {value}')
        return number

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                fail(f'{label} contains duplicate key {key!r}')
            value[key] = item
        return value

    try:
        return json.loads(
            text, parse_constant=constant, parse_float=floating,
            object_pairs_hook=pairs)
    except (json.JSONDecodeError, UnicodeError) as exc:
        fail(f'{label} is malformed: {exc}')

def state_dir():
    path = Path(os.getenv('XDG_STATE_HOME', Path.home() / '.local/state')) / 'browserctl'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path

def socket_path():
    root = os.getenv('XDG_RUNTIME_DIR')
    require(root, 'XDG_RUNTIME_DIR is required')
    return Path(root) / 'browserctl.sock'

def remove_stale_socket(path):
    if not path.exists():
        return
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(IO_TIMEOUT)
        status = probe.connect_ex(str(path))
    if status == 0:
        fail('another browserctl daemon is already running')
    path.unlink(missing_ok=True)

def read_json(path, label):
    try:
        return strict_load(path.read_text(encoding='utf-8'), label)
    except (OSError, UnicodeError, IndexError, ValueError) as exc:
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
    value = read_json(path, 'host config')
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

def listener_inode(host, port):
    table, address = (
        ('/proc/net/tcp', '0100007F') if host == '127.0.0.1' else
        ('/proc/net/tcp6', '00000000000000000000000001000000'))
    local = f'{address}:{port:04X}'
    matches = []
    for line in Path(table).read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == local and fields[3] == '0A':
            matches.append(fields[9])
    require(len(matches) == 1,
            'selected CDP endpoint is not one exact loopback listener')
    return matches[0]

def tree_owns_listener(root, inode):
    target = f'socket:[{inode}]'
    pending, seen = [root], set()
    while pending:
        pid = pending.pop()
        require(type(pid) is int and pid > 0 and pid not in seen,
                'selected Edge process tree is malformed')
        seen.add(pid)
        process = Path(f'/proc/{pid}')
        try:
            child_tokens = (
                process / f'task/{pid}/children').read_text().split()
            descriptors = list((process / 'fd').iterdir())
        except OSError:
            return False
        require(all(token.isdecimal() and int(token) > 0
                    for token in child_tokens),
                'selected Edge child list is malformed')
        pending.extend(int(token) for token in child_tokens)
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) == target:
                    return True
            except OSError:
                pass
    return False

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
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        state, start = fields[0], fields[19]
        owner = Path(f'/proc/{pid}').stat().st_uid
        exe = os.readlink(f'/proc/{pid}/exe')
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
        argv = [item.decode('utf-8') for item in argv]
    except (OSError, UnicodeError, IndexError, ValueError) as exc:
        fail(f'cannot inspect selected Edge process: {exc}')
    if owner != os.getuid() or state == 'Z' or exe != profile['edge_exe']:
        fail('selected Edge PID owner, liveness, or executable is wrong')
    command = '\0'.join(argv)
    boundary = r'(?:^|[ \0])'
    if re.search(boundary + r'--type=', command):
        fail('selected Edge PID is not a top-level browser process')
    parsed = endpoint(profile['cdp_endpoint'], 'selected CDP endpoint')
    port_flag = f'--remote-debugging-port={parsed.port}'
    if not re.search(boundary + re.escape(port_flag) + r'(?:$|[ \0])', command):
        fail(f'selected Edge PID lacks {port_flag}')
    profile_flag = f"--user-data-dir={profile['profile']}"
    if not re.search(
            boundary + re.escape(profile_flag) + r'(?:$|[ \0])', command):
        fail('selected Edge PID has the wrong user-data-dir')
    inode = listener_inode(parsed.hostname, parsed.port)
    require(tree_owns_listener(pid, inode),
            'selected Edge process tree does not own the CDP listener')
    return {'selector': name, 'pid': pid, 'start': start, 'exe': exe,
            'profile': profile['profile'], 'cdp': profile['cdp_endpoint']}

def valid_receipt(receipt, legacy=False):
    keys = {'id', 'request_id', 'stdout', 'stderr', 'exit_code'}
    if legacy:
        keys |= {'success', 'post'}
    valid = (
        isinstance(receipt, dict) and set(receipt) == keys and
        all(isinstance(receipt[key], str)
            for key in ('id', 'request_id', 'stdout', 'stderr')) and
        type(receipt['exit_code']) is int and
        receipt['exit_code'] in {0, 1})
    return valid and (not legacy or
                      type(receipt['success']) is bool and
                      isinstance(receipt['post'], dict))

def validate_task(value):
    valid = (
        isinstance(value, dict) and set(value) == TASK_KEYS and
        value.get('schema_version') == SCHEMA and
        isinstance(value.get('state'), str) and
        value.get('state') in {'READY', 'RUNNING', 'RESULT', 'RECOVERY'} and
        isinstance(value.get('owner'), str) and
        0 < len(value['owner']) <= 200 and
        isinstance(value.get('generation'), str) and
        re.fullmatch('[0-9a-f]{32}', value.get('generation', '')))
    if not valid:
        fail('task journal is malformed or unsupported')
    if value['state'] == 'READY':
        good = value['operation'] is value['receipt'] is value['reason'] is None
    elif value['state'] == 'RUNNING':
        operation = value['operation']
        good = (
            isinstance(operation, dict) and
            set(operation) == {'id', 'method'} and
            all(isinstance(operation[key], str) for key in ('id', 'method')) and
            value['receipt'] is value['reason'] is None)
    elif value['state'] == 'RESULT':
        good = (value['operation'] is value['reason'] is None and
                valid_receipt(value['receipt']))
    else:
        good = value['operation'] is value['receipt'] is None and bool(value['reason'])
    if not good:
        fail('task journal fields disagree with its state')
    return value

def migrate_task(value):
    if not isinstance(value, dict) or set(value) != TASK_KEYS | {
            'phase', 'snapshot'}:
        return value
    migrated = dict(value)
    phase, snapshot = migrated.pop('phase'), migrated.pop('snapshot')
    require(isinstance(phase, str) and
            phase in {'NEW', 'LIST', 'SELECT', 'SNAPSHOT', 'ARMED'} and
            type(snapshot) is bool, 'task journal is malformed or unsupported')
    if migrated.get('state') == 'RESULT':
        require(valid_receipt(migrated.get('receipt'), legacy=True),
                'legacy result receipt is malformed')
        receipt = dict(migrated['receipt'])
        del receipt['success'], receipt['post']
        migrated['receipt'] = receipt
    validate_task(migrated)
    write_task(migrated)
    return migrated

def read_task(missing=True):
    path = state_dir() / 'task.json'
    try:
        return validate_task(migrate_task(read_json(path, 'task journal')))
    except Error:
        if missing and (not path.exists()):
            return None
        raise

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
        self.session = None
        self.next_id = 1
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}/mcp'

    def start(self):
        authority = f'127.0.0.1:{self.port}'
        argv = self.config['mcp_command'] + [
            '--cdp-endpoint', self.profile['cdp_endpoint'],
            '--shared-browser-context', '--caps=config',
            '--host', '127.0.0.1', '--allowed-hosts', authority,
            '--port', str(self.port)]
        env = {key: value for key, value in os.environ.items() if not key.startswith('PLAYWRIGHT_MCP_')}
        env['PATH'] = str(Path(argv[0]).parent) + ':/usr/local/bin:/usr/bin:/bin'
        env['PLAYWRIGHT_MCP_PING_TIMEOUT_MS'] = '0'
        try:
            self.child = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            fail(f'cannot start pinned MCP command: {exc}')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                fail(f'MCP exited during startup ({self.child.returncode})')
            with socket.socket() as probe:
                probe.settimeout(0.05)
                if probe.connect_ex(('127.0.0.1', self.port)) == 0:
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
        params = {
            'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'browserctl', 'version': '1'}}
        response = self.post({
            'jsonrpc': '2.0', 'id': identifier,
            'method': 'initialize', 'params': params}, True)
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
            if (not isinstance(error, dict) or
                    not {'code', 'message'} <= set(error) or
                    type(error['code']) is not int or
                    not isinstance(error['message'], str)):
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
        result['tools'] = [tool for tool in result['tools']
                           if isinstance(tool, dict) and
                           tool.get('name') != 'browser_close']
        return result

    def call(self, name, arguments):
        result = self.call_raw('tools/call', {'name': name, 'arguments': arguments})
        require(isinstance(result, dict) and
                isinstance(result.get('content'), list) and
                ('isError' not in result or type(result['isError']) is bool),
                'MCP tool result is malformed')
        if result.get('isError'):
            raise RemoteError(json.dumps(result, sort_keys=True))
        return result

    def close(self):
        if not self.child:
            return
        pid = self.child.pid
        if self.child.poll() is not None:
            self.child.wait()
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.child.wait(IO_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(pid, signal.SIGKILL)
            self.child.wait(IO_TIMEOUT)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

class RemoteError(Error):
    pass

def new_task(owner, generation):
    if not isinstance(owner, str) or not 0 < len(owner) <= 200 or any((ord(char) < 32 or ord(char) == 127 for char in owner)):
        fail('OWNER must be 1-200 printable characters')
    return {'schema_version': SCHEMA, 'state': 'READY', 'owner': owner,
            'generation': generation, 'operation': None, 'receipt': None,
            'reason': None}

def recovery(task, reason):
    task.update({'state': 'RECOVERY', 'operation': None, 'receipt': None,
                 'reason': reason[:1000]})
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
        if task['state'] == 'READY':
            task['generation'] = self.generation
            write_task(task)
        elif task['state'] == 'RUNNING':
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

    def dispatch(self, task, request_id, method, action):
        self.validate_identity(task)
        task.update({'state': 'RUNNING', 'operation': {'id': request_id, 'method': method}})
        write_task(task)
        try:
            value = action()
            stdout, stderr, code = (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n', '', 0)
        except RemoteError as exc:
            stdout, stderr, code = ('', f'browserctl: {exc}\n', 1)
        except Error as exc:
            recovery(task, str(exc))
            raise
        receipt = {'id': secrets.token_hex(16), 'request_id': request_id,
                   'stdout': stdout, 'stderr': stderr, 'exit_code': code}
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

    def command(self, request):
        valid = (
            isinstance(request, dict) and
            set(request) == {'id', 'command', 'args', 'build'} and
            isinstance(request['id'], str) and
            isinstance(request['command'], str) and
            isinstance(request['args'], list) and request['build'] == BUILD)
        if not valid:
            fail('client request schema is malformed')
        rid, command, args = (request['id'], request['command'], request['args'])
        if command == 'status':
            task = read_task()
            return ({'build': BUILD, 'pid': os.getpid(), 'generation': self.generation,
                     'state': 'IDLE' if task is None else task['state'],
                     'owner': None if task is None else task['owner']}, None)
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
                         'reason': None})
            write_task(task)
            return ({'state': 'READY'}, None)
        if command == 'task_end':
            require(not args, 'task end takes no arguments')
            task = read_task(False)
            if task['state'] == 'RESULT':
                fail('result pending; run result')
            if task['state'] not in {'READY', 'RECOVERY'}:
                fail('task operation is still running')
            remove_task()
            return ({'state': 'IDLE', 'owner': task['owner']}, None)
        task = self.require_ready()
        if command == 'tools':
            require(not args, 'tools takes no arguments')
            receipt = self.dispatch(task, rid, 'tools', self.mcp.tools)
        elif command == 'call':
            if len(args) != 2 or not isinstance(args[0], str) or (not args[0]) or (not isinstance(args[1], dict)):
                fail('call requires TOOL and a JSON object')
            name, arguments = args
            if name == 'browser_close':
                fail(f'tool {name!r} is forbidden')
            receipt = self.dispatch(
                task, rid, f'call {name}',
                lambda: self.mcp.call(name, arguments))
        else:
            fail('unknown daemon command')
        return (receipt, receipt['id'])

    def acknowledge(self, receipt_id):
        task = read_task(False)
        if task['state'] != 'RESULT' or task['receipt']['id'] != receipt_id:
            fail('result acknowledgement mismatch')
        task.update({'state': 'READY', 'generation': self.generation,
                     'receipt': None})
        write_task(task)

    def serve(self):
        for signum in (signal.SIGTERM, signal.SIGINT): signal.signal(signum, stop_signal)
        verify_service(starting=True)
        self.identity = edge_identity(self.config)
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
                            send_message(connection, {
                                'id': request.get('id')
                                if isinstance(request, dict) else None,
                                'ok': False, 'error': str(exc),
                                'build': BUILD})
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
        valid = (
            isinstance(response, dict) and
            set(response) in ({'id', 'ok', 'value', 'build'},
                              {'id', 'ok', 'error', 'build'}) and
            response.get('build') == BUILD and
            response.get('ok') in {True, False})
        if not valid:
            fail('daemon response schema is malformed')
        if response['id'] != request_id:
            fail('daemon response correlation mismatch')
        if not response['ok']:
            fail(response['error'])
        value = response['value']
        if command in {'tools', 'call', 'result'}:
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

def verify_service(starting=False):
    result = subprocess.run(
        ['systemctl', '--user', 'show', 'browserctl-controller.service',
         '-p', 'ActiveState', '-p', 'MainPID'],
        text=True, capture_output=True, timeout=10)
    if result.returncode:
        fail(f'cannot inspect browserctl service: {result.stderr.strip()}')
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines()
                  if '=' in line)
    try:
        main_pid = int(fields['MainPID'])
    except (KeyError, ValueError):
        fail('service MainPID is malformed')
    if starting:
        require(fields.get('ActiveState') == 'activating' and
                os.getenv('NOTIFY_SOCKET') and main_pid == os.getpid(),
                'daemon must be the starting browserctl service MainPID')
        return {'active': False, 'build': BUILD, 'main_pid': main_pid}
    require(fields.get('ActiveState') in {'active', 'inactive'},
            'browserctl service state is unexpected')
    active = fields.get('ActiveState') == 'active'
    if active:
        status = client('status', [], True)
        require(status.get('build') == BUILD and
                status.get('pid') == main_pid,
                'active service runs a different browserctl build')
    return {'active': active, 'build': BUILD, 'main_pid': main_pid}

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
    service = verify_service()
    identity = edge_identity(config)
    print(json.dumps({'ok': True, 'edge_pid': identity['pid'],
                      'profile': identity['profile'], 'service': service},
                     sort_keys=True))

SIMPLE_CLI = {
    ('status',): ('status', []), ('task', 'recover'): ('task_recover', []),
    ('task', 'end'): ('task_end', []), ('tools',): ('tools', []),
    ('result',): ('result', [])}

def tab_cli(argv):
    if len(argv) not in {2, 3} or argv[0] != 'tab' or argv[1] not in {
            'new', 'list', 'select', 'close'}:
        return None
    action, arguments = argv[1], {'action': argv[1]}
    if action in {'select', 'close'}:
        if len(argv) != 3 or not argv[2].isdigit():
            fail(f'tab {action} requires a nonnegative INDEX')
        arguments['index'] = int(argv[2])
    elif len(argv) != 2:
        fail(f'tab {action} takes no arguments')
    return client('call', ['browser_tabs', arguments])

def parse_cli(argv):
    if argv == ['daemon']:
        Daemon().serve()
        return 0
    if argv == ['doctor']:
        doctor()
        return 0
    if tuple(argv) in SIMPLE_CLI:
        return client(*SIMPLE_CLI[tuple(argv)])
    tab_result = tab_cli(argv)
    if tab_result is not None:
        return tab_result
    if len(argv) == 3 and argv[:2] == ['task', 'begin']:
        return client('task_begin', [argv[2]])
    if len(argv) == 3 and argv[0] == 'call':
        arguments = strict_load(argv[2], 'tool arguments')
        if not isinstance(arguments, dict):
            fail('tool arguments must be a JSON object')
        return client('call', [argv[1], arguments])
    fail(
        'usage: browserctl.py doctor|status|task begin OWNER|task recover|'
        'task end|tab new|tab list|tab select INDEX|tab close INDEX|tools|'
        'call TOOL JSON|result')
if __name__ == '__main__':
    try:
        raise SystemExit(parse_cli(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Error as exc:
        print(f'browserctl: {exc}', file=sys.stderr)
        raise SystemExit(1)
