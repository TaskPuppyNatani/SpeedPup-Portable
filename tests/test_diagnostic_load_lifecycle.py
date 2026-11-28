"""Phase 3D.2: load-process lifecycle isolation and failure classification.

Every fixture here is either an in-process monkeypatched transport or a local
``python -c`` child script. The real load module is never launched, so no live
discovery, download or upload traffic can be generated from this module.
"""
import contextlib
import io
import json
import subprocess
import sys
import time
from threading import Event

import pytest

from speedpup.core import bufferbloat
from speedpup.core import diagnostic_load as load
from speedpup.core.diagnostic_ping import PingSample


@pytest.fixture(autouse=True)
def isolate_child_and_disable_probes(monkeypatch):
    """Isolation must extend into child processes: prove no real load module is ever spawned."""
    real_popen = subprocess.Popen
    executed = []

    def guarded_popen(command, **kwargs):
        executed.append(' '.join(str(part) for part in command))
        if 'speedpup.core.diagnostic_load' in executed[-1]:
            raise AssertionError('real load subprocess is forbidden in this test')
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', guarded_popen)
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', guarded_popen)
    monkeypatch.setattr(bufferbloat, 'ping_once', lambda *_: PingSample(7))
    yield
    real_load_child = [cmd for cmd in executed if 'speedpup.core.diagnostic_load' in cmd]
    assert not real_load_child, f'fixture isolation broken: {real_load_child}'


def _child(monkeypatch, script):
    original = subprocess.Popen
    children = []

    def spawn(*_args, **kwargs):
        child = original([sys.executable, '-c', script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', spawn)
    return children


def _run(direction='upload'):
    return bufferbloat.run_load_stage(direction, '127.0.0.1', time.monotonic(), Event(), lambda *_: None)


def _child_events(direction='download'):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        load.main(direction, 8 * 1024 * 1024)
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_child_budget_stays_inside_the_supervising_deadline():
    """The child must always report before the supervisor is allowed to kill it."""
    assert load.CHILD_BUDGET_SECONDS + 5.0 < bufferbloat.SUPERVISOR_SECONDS
    assert load.DISCOVERY_SECONDS + load.LOAD_SECONDS + load.CLEANUP_SECONDS < load.CHILD_BUDGET_SECONDS


def test_supervisor_deadline_expiration_is_named_and_reaped(monkeypatch):
    monkeypatch.setattr(bufferbloat, 'SUPERVISOR_SECONDS', 2.0)
    children = _child(monkeypatch, 'import time; time.sleep(30)')
    started = time.monotonic()
    result = _run()
    assert time.monotonic() - started < 4.0
    assert not result.adequate
    assert 'supervising deadline' in result.reason
    assert children[0].poll() is not None


def test_launch_or_import_failure_is_named(monkeypatch):
    children = _child(monkeypatch,
                      "import sys; print('ModuleNotFoundError: No module named \\'speedpup\\'', file=sys.stderr); sys.exit(1)")
    result = _run()
    assert 'could not start' in result.reason
    assert 'ModuleNotFoundError' not in result.reason
    assert 'exit status 1' in result.reason
    assert children[0].poll() == 1


def test_launch_exception_keeps_only_the_failure_classification(monkeypatch):
    def fail_to_launch(*args, **kwargs):
        raise OSError("private fixture /Users/load-test?token=fixture-launch-token")

    monkeypatch.setattr(bufferbloat.subprocess, "Popen", fail_to_launch)
    result = _run()
    assert result.reason == "Load process could not start"
    assert result.failure_codes == ("child_launch_failed",)


def test_premature_child_exit_is_named(monkeypatch):
    _child(monkeypatch, 'import json,sys,time;'
                        'print(json.dumps({"kind":"activity","at":time.monotonic(),"bytes":4096}),flush=True); sys.exit(0)')
    result = _run()
    assert 'ended before reporting results' in result.reason
    assert 'supervising deadline' not in result.reason
    assert 'could not start' not in result.reason


def test_unclean_worker_cleanup_is_named(monkeypatch):
    _child(monkeypatch, 'import json,time;'
                        'print(json.dumps({"kind":"done","at":time.monotonic(),"bytes":2000000,'
                        '"accepted":2000000,"errors":[],"clean":False,"exit_reason":"completed"}),flush=True)')
    result = _run()
    assert 'cleanup window' in result.reason


def test_supervisor_reports_discovery_timeout_as_discovery_not_endpoint_failure(monkeypatch):
    """The user-facing reason must name discovery, even though an error event is present."""
    _child(monkeypatch, 'import json,time;'
                        'print(json.dumps({"kind":"error","message":"test endpoint discovery exceeded its time allowance"}),flush=True);'
                        'print(json.dumps({"kind":"done","at":time.monotonic(),"bytes":0,"accepted":0,'
                        '"errors":[],"clean":True,"exit_reason":"discovery-timeout"}),flush=True)')
    result = _run('upload')
    assert 'Test endpoint discovery exceeded its time allowance' in result.reason
    assert 'Load endpoint failure' not in result.reason


def test_child_reports_discovery_timeout_instead_of_overrunning(monkeypatch):
    monkeypatch.setattr(load, 'DISCOVERY_SECONDS', 1.0)

    class Engine:
        def __init__(self, **_): pass

        def discover_servers(self):
            time.sleep(10)
            return [object()]

        def select_best_server(self, servers):  # pragma: no cover - unreachable
            raise AssertionError('selection must not run after a discovery timeout')

    monkeypatch.setattr(load, 'SpeedTestEngine', Engine)
    started = time.monotonic()
    events = _child_events()
    wall = time.monotonic() - started
    done = [event for event in events if event['kind'] == 'done'][0]
    assert wall < 3.0
    assert done['exit_reason'] == 'discovery-timeout'
    assert done['clean'] and done['errors'] == []
    message = [event for event in events if event['kind'] == 'error'][0]['message']
    assert 'discovery exceeded its time allowance' in message
    adequate, reason = bufferbloat.classify_load(done, message, 0.0)
    assert not adequate and 'discovery exceeded its time allowance' in reason
    assert 'Load endpoint failure' not in reason


def test_endpoint_setup_stall_is_bounded_by_the_child(monkeypatch):
    """A resolver-style stall cannot overrun the stage: setup is bounded, not left to DNS."""

    class Engine:
        def __init__(self, **_): pass

        def discover_servers(self): return [object()]

        def select_best_server(self, servers):
            from types import SimpleNamespace
            return SimpleNamespace(download_url='https://fixture.invalid/dl',
                                   upload_url='https://fixture.invalid/ul'), 1

    class StalledSetup:
        def __init__(self):
            self._create_connection = lambda address, timeout=2.0, source_address=None: time.sleep(10)

        def request(self, *args, **kwargs): self._create_connection(('fixture.invalid', 443), 2.0)
        def getresponse(self): raise RuntimeError('unused')
        def putrequest(self, *args): pass
        def putheader(self, *args): pass
        def endheaders(self): self._create_connection(('fixture.invalid', 443), 2.0)
        def send(self, data): pass
        def close(self): pass

    monkeypatch.setattr(load, 'SpeedTestEngine', Engine)
    monkeypatch.setattr(load, '_connection', lambda url, timeout: StalledSetup())
    started = time.monotonic()
    events = _child_events('upload')
    wall = time.monotonic() - started
    done = [event for event in events if event['kind'] == 'done'][0]
    assert wall < load.LOAD_SECONDS + load.CLEANUP_SECONDS + 2.0
    assert done['errors'] and 'setup exceeded its time allowance' in done['errors'][0]
    adequate, reason = bufferbloat.classify_load(done, done['errors'][0], 0.0)
    assert not adequate and 'Load endpoint failure' in reason and 'setup exceeded its time allowance' in reason


def test_worker_cleanup_is_bounded_once_not_cumulative(monkeypatch):
    """Cleanup used to wait 3 x 2.1 s in series; it now has one bounded window."""
    monkeypatch.setattr(load, 'LOAD_SECONDS', 1.0)
    monkeypatch.setattr(load, 'RAMP_INTERVAL', 0.1)

    class Engine:
        def __init__(self, **_): pass

        def discover_servers(self): return [object()]

        def select_best_server(self, servers):
            from types import SimpleNamespace
            return SimpleNamespace(download_url='https://fixture.invalid/dl',
                                   upload_url='https://fixture.invalid/ul'), 1

    class BlockingSend:
        def __init__(self):
            self._create_connection = lambda *args, **kwargs: None

        def putrequest(self, *args): pass
        def putheader(self, *args): pass
        def endheaders(self): pass
        def send(self, data): time.sleep(6.0)
        def getresponse(self): raise RuntimeError('unused')
        def close(self): pass

    monkeypatch.setattr(load, 'SpeedTestEngine', Engine)
    monkeypatch.setattr(load, '_connection', lambda url, timeout: BlockingSend())
    started = time.monotonic()
    events = _child_events('upload')
    wall = time.monotonic() - started
    done = [event for event in events if event['kind'] == 'done'][0]
    assert done['clean'] is False
    assert wall < load.LOAD_SECONDS + load.CLEANUP_SECONDS + 1.0
    assert wall > load.LOAD_SECONDS + 1.0  # the bounded window was actually used
    adequate, reason = bufferbloat.classify_load(done, None, 0.0)
    assert not adequate and 'cleanup window' in reason


def test_child_still_reports_a_clean_completed_run(monkeypatch):
    """The lifecycle guards must not suppress a normal successful stage."""

    class Engine:
        def __init__(self, **_): pass

        def discover_servers(self): return [object()]

        def select_best_server(self, servers):
            from types import SimpleNamespace
            return SimpleNamespace(download_url='https://fixture.invalid/dl',
                                   upload_url='https://fixture.invalid/ul'), 1

    class Response:
        status = 200

        def read(self, n): return b'x' * n

    class Fixture:
        def __init__(self):
            self._create_connection = lambda *args, **kwargs: None

        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass

    monkeypatch.setattr(load, 'SpeedTestEngine', Engine)
    monkeypatch.setattr(load, '_connection', lambda url, timeout: Fixture())
    monkeypatch.setattr(load, 'LOAD_SECONDS', 1.2)
    monkeypatch.setattr(load, 'RAMP_INTERVAL', 0.1)
    events = _child_events('download')
    done = [event for event in events if event['kind'] == 'done'][0]
    assert done['clean'] and not done['errors'] and done['exit_reason'] == 'completed'
    assert done['bytes'] > 0


def test_end_of_load_window_is_not_an_endpoint_failure(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(load, '_discover', lambda _: SimpleNamespace(upload_url='https://fixture.invalid/ul'))
    monkeypatch.setattr(load, 'LOAD_SECONDS', .35)
    monkeypatch.setattr(load, 'WORKERS', 1)

    class Response:
        status = 200
        def read(self, _): return b''
    class Connection:
        def __init__(self): self._create_connection = lambda *args: (time.sleep(.08), object())[1]
        def putrequest(self, *_): pass
        def putheader(self, *_): pass
        def endheaders(self): self._create_connection(('fixture.invalid', 443), 2.0)
        def send(self, _): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(load, '_connection', lambda *_: Connection())
    done = next(event for event in _child_events('upload') if event['kind'] == 'done')
    assert done['bytes'] > 0
    assert done['errors'] == []


def test_setup_socket_returning_after_timeout_is_closed():
    from threading import Event
    returned = Event()
    class Socket:
        def __init__(self): self.closed = Event()
        def close(self): self.closed.set()
    sock = Socket()
    def connect():
        time.sleep(.1)
        returned.set()
        return sock
    with pytest.raises(load.LoadWindowExpired):
        load._run_within(.01, connect, on_late_result=lambda result: result.close())
    assert returned.wait(1) and sock.closed.wait(1)


def test_setup_connection_closes_socket_created_after_load_window(monkeypatch):
    from threading import Event
    class Socket:
        def __init__(self): self.closed = Event()
        def close(self): self.closed.set()
    sock = Socket()
    class Connection:
        def __init__(self): self._create_connection = self.connect
        def connect(self, *_):
            time.sleep(.1)
            return sock
    monkeypatch.setattr(load, '_connection', lambda *_: Connection())
    connection = load._setup_connection('https://fixture.invalid/', 2.0, time.monotonic() + .02)
    with pytest.raises(load.LoadWindowFinished):
        connection._create_connection(('fixture.invalid', 443), 2.0)
    assert sock.closed.wait(1)


def test_late_result_is_rejected_even_if_worker_finishes_before_caller_checks(monkeypatch):
    from threading import Thread
    original_join = Thread.join
    def delayed_join(thread, timeout=None):
        original_join(thread, timeout)
        time.sleep(.08)  # caller is descheduled after its deadline
    monkeypatch.setattr(Thread, 'join', delayed_join)
    closed = Event()
    def connect():
        time.sleep(.04)
        return object()
    with pytest.raises(load.LoadWindowExpired):
        load._run_within(.01, connect, on_late_result=lambda _: closed.set())
    assert closed.wait(1)


def test_timely_socket_is_closed_if_caller_resumes_after_deadline(monkeypatch):
    from threading import Thread
    original_join = Thread.join
    def delayed_join(thread, timeout=None):
        original_join(thread, timeout)
        time.sleep(.05)
    monkeypatch.setattr(Thread, 'join', delayed_join)
    closed = Event()
    with pytest.raises(load.LoadWindowExpired):
        load._run_within(.02, lambda: object(), on_late_result=lambda _: closed.set())
    assert closed.is_set()


def test_timely_endpoint_error_is_not_hidden_by_delayed_caller(monkeypatch):
    from threading import Thread
    original_join = Thread.join
    def delayed_join(thread, timeout=None):
        original_join(thread, timeout)
        time.sleep(.05)
    monkeypatch.setattr(Thread, 'join', delayed_join)
    def fail():
        raise OSError('endpoint rejected')
    with pytest.raises(OSError, match='endpoint rejected'):
        load._run_within(.02, fail)


def test_supervisor_timeout_never_accepts_done_from_live_child():
    done = {'clean': True, 'bytes': 256 * 1024 * 1024, 'accepted': 256 * 1024 * 1024,
            'errors': [], 'budget_exhausted': True, 'exit_reason': 'completed'}
    adequate, reason = bufferbloat.classify_load(done, None, 7.4, timed_out=True)
    assert not adequate and 'supervising deadline' in reason


def test_budget_exhaustion_with_sufficient_coverage_remains_eligible():
    done = {'clean': True, 'bytes': 256 * 1024 * 1024, 'accepted': 256 * 1024 * 1024,
            'errors': [], 'budget_exhausted': True, 'exit_reason': 'completed'}
    adequate, reason = bufferbloat.classify_load(done, None, 7.4)
    assert adequate and 'budget exhausted' in reason


def test_expired_discovery_budget_never_launches_action():
    calls = []
    with pytest.raises(load.LoadWindowExpired):
        load._run_within(0, lambda: calls.append(True), 'discovery')
    assert not calls


def test_setup_action_does_not_start_after_thread_start_delay(monkeypatch):
    from threading import Thread
    original_start = Thread.start
    def delayed_start(thread):
        time.sleep(.04)
        original_start(thread)
    monkeypatch.setattr(Thread, 'start', delayed_start)
    calls = []
    with pytest.raises(load.LoadWindowExpired):
        load._run_within(.01, lambda: calls.append(True))
    assert not calls


def test_done_then_stuck_child_is_reaped_and_not_graded(monkeypatch):
    monkeypatch.setattr(bufferbloat, 'SUPERVISOR_SECONDS', 5.0)
    children = _child(monkeypatch, 'import json,time;'
                      'print(json.dumps({"kind":"done","at":time.monotonic(),"bytes":2000000,'
                      '"accepted":2000000,"errors":[],"clean":True,"exit_reason":"completed"}),flush=True);'
                      'time.sleep(30)')
    start = time.monotonic()
    result = _run()
    assert time.monotonic() - start < 3
    assert not result.adequate and 'did not exit' in result.reason
    assert children[0].poll() is not None


def test_discovery_timeout_with_stuck_child_is_promptly_reaped(monkeypatch):
    children = _child(monkeypatch, 'import json,time,threading;'
                      'threading.Thread(target=lambda: time.sleep(30)).start();'
                      'print(json.dumps({"kind":"done","at":time.monotonic(),"bytes":0,'
                      '"accepted":0,"errors":[],"clean":True,"exit_reason":"discovery-timeout"}),flush=True)')
    start = time.monotonic()
    result = _run()
    assert time.monotonic() - start < 3
    assert not result.adequate and 'discovery exceeded' in result.reason
    assert 'child did not exit and was reaped' in result.reason
    assert children[0].poll() is not None


def test_real_discovery_executor_stall_is_reaped(monkeypatch):
    # Run the real selection executor in a child, but replace its resolver-like
    # probe with a sleep: no server discovery or traffic occurs.
    script = '''
import time
from types import SimpleNamespace
from speedpup.core import diagnostic_load as load
from speedpup.core.speedtest import SpeedTestEngine
class Engine(SpeedTestEngine):
    def discover_servers(self): return [SimpleNamespace()]
    def probe_latency(self, *args, **kwargs): time.sleep(30)
load.SpeedTestEngine = Engine
load.DISCOVERY_SECONDS = .1
load.main('upload', 24 * 1024 * 1024)
'''
    children = _child(monkeypatch, script)
    start = time.monotonic()
    result = _run()
    assert time.monotonic() - start < 3
    assert not result.adequate and 'discovery exceeded' in result.reason
    assert children[0].poll() is not None


def test_child_failing_after_done_cannot_qualify_for_grade():
    done = {'clean': True, 'bytes': 2_000_000, 'accepted': 2_000_000,
            'errors': [], 'exit_reason': 'completed'}
    adequate, reason = bufferbloat.classify_load(done, None, 6, exit_code=1)
    assert not adequate and 'exit status 1' in reason


def test_invalid_child_result_does_not_expose_raw_value(monkeypatch):
    _child(monkeypatch, """print('[]')
print('{"kind":"done","clean":true,"bytes":"token=fixture-invalid-status"}')
""")
    result = _run()
    assert not result.adequate and 'missing_child_result' in result.failure_codes
    assert 'ended before reporting results' in result.reason
    assert 'fixture-invalid-status' not in result.reason


def test_real_supervisor_replays_native_partial_worker_timeout(monkeypatch):
    # Child emits controlled counters only. Its totals match the native trace;
    # worker 1 fails at 2 s while workers 2 and 3 continue through 7.6 s.
    script = '''
import json,time
started=time.monotonic()
for i in range(39):
    due=started+i*.2
    time.sleep(max(0,due-time.monotonic()))
    a=2500000*min(i,10)//10
    b=5000000*max(0,i-2)//36
    c=6000416*max(0,i-2)//36
    accepted=5869344*max(0,i-2)//36
    print(json.dumps({'kind':'activity','at':time.monotonic(),'bytes':a+b+c,
                      'workers':{'1':[a,a], '2':[b,b], '3':[c,accepted]}}),flush=True)
print(json.dumps({'kind':'load_end','at':time.monotonic()}),flush=True)
print(json.dumps({'kind':'done','at':time.monotonic(),'bytes':13500416,'accepted':13369344,
                  'clean':True,'exit_reason':'completed','budget_exhausted':False,
                  'errors':['endpoint setup exceeded its time allowance'],'workers_started':3,
                  'worker_results':{'1':'setup_timeout','2':'completed','3':'completed'},
                  'workers':{'1':[2500000,2500000],'2':[5000000,5000000],
                             '3':[6000416,5869344]}}),flush=True)
'''
    children = _child(monkeypatch, script)
    result = _run('upload')
    assert children[0].poll() == 0
    assert result.adequate and result.attempted >= 7 and result.successful == result.attempted
    assert sum(sample.loaded_latency_ms is not None for sample in result.samples) >= 3
    assert result.qualifications and 'setup timed out' in result.reason
