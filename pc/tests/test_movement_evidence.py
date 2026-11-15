"""Manual-control evidence is bounded, private off-game, and never injects input."""
import ast
import ctypes
import json
from pathlib import Path
import sys
from threading import Event
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import movement_evidence
from movement_evidence import MovementRecorder


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError('movement worker did not reach expected state')


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_schema_sanitization_foreground_privacy_and_cursor_reset(tmp_path):
    states = iter([
        dict(foreground_pid=42, keys={'W', 'password', 'ENTER'}, buttons={'right', 'middle'}, cursor=(10, 20)),
        dict(foreground_pid=42, keys={'D': True, 'A': 'private text'}, buttons={'left': True}, cursor=(13, 24)),
        dict(foreground_pid=99, keys={'S', 'private'}, buttons={'left'}, cursor=(9999, 8888), window_title='private'),
        dict(foreground_pid=42, keys={'S'}, buttons=set(), cursor=(100, 200)),
    ])
    recorder = MovementRecorder(tmp_path, lambda: next(states))
    recorder.INTERVAL = .001
    recorder.MAX_ROWS = 4
    recorder.start(tmp_path / 'session', 42)
    wait_for(lambda: not recorder.status()['running'])
    recorder.close()
    samples = rows(tmp_path / 'session/movement/samples.jsonl')
    assert len(samples) == 4
    assert set(samples[0]['keys']) == {'W', 'A', 'S', 'D'}
    assert samples[0]['keys']['W'] and not any(samples[0]['keys'][key] for key in ('A', 'S', 'D'))
    assert samples[0]['buttons'] == {'left': False, 'right': True}
    assert samples[1]['keys']['D'] is True and samples[1]['keys']['A'] is False
    assert samples[1]['cursor_delta'] == [3, 4] and samples[1]['camera_possible_change'] is True
    assert set(samples[2]) == {'epoch', 'utc', 'monotonic', 'active'}
    assert samples[2]['active'] is False
    assert samples[3]['cursor_delta'] is None and samples[3]['camera_possible_change'] is False
    assert all(isinstance(row['epoch'], float) and isinstance(row['monotonic'], float) for row in samples)
    assert all(samples[i]['monotonic'] <= samples[i+1]['monotonic'] for i in range(3))
    text = (tmp_path / 'session/movement/samples.jsonl').read_text(encoding='utf-8')
    assert all(secret not in text for secret in ('password', 'ENTER', 'middle', 'private', '9999', '8888'))
    manifest = json.loads((tmp_path / 'session/movement/manifest.json').read_text(encoding='utf-8'))
    assert manifest['schema_version'] == 1 and manifest['game_pid'] == 42
    assert manifest['input_sent'] is False and manifest['calibration_verified'] is False
    assert manifest['status']['active_rows'] == 3 and manifest['status']['inactive_rows'] == 1
    assert manifest['status']['stop_reason'] == 'row_limit'
    assert manifest['limits'] == dict(interval_seconds=.001, seconds=90., rows=4, jsonl_bytes=262144)


@pytest.mark.parametrize('which', ['rows', 'bytes', 'time'])
def test_each_bound_stops_and_finalizes(tmp_path, which):
    provider = lambda: dict(foreground_pid=42, keys={'W'}, buttons=set(), cursor=(1, 2))
    recorder = MovementRecorder(tmp_path, provider)
    recorder.INTERVAL = .001
    if which == 'rows':
        recorder.MAX_ROWS = 3
    elif which == 'bytes':
        recorder.MAX_BYTES = 600
    else:
        recorder.MAX_SECONDS = .015
    recorder.start(tmp_path / which, 42)
    wait_for(lambda: not recorder.status()['running'])
    recorder.close()
    state = recorder.status()
    expected = {'rows': 'row_limit', 'bytes': 'byte_limit', 'time': 'duration_limit'}[which]
    assert state['stop_reason'] == expected
    data = (tmp_path / which / 'movement/samples.jsonl').read_bytes()
    assert len(data) == state['bytes_written'] <= recorder.MAX_BYTES
    assert len(data.splitlines()) == state['rows'] <= recorder.MAX_ROWS
    assert state['finished_monotonic'] >= state['started_monotonic']
    assert {name for _, name in recorder.export_members()} == {'movement/manifest.json', 'movement/samples.jsonl'}


def test_stop_is_prompt_pending_result_discarded_and_export_waits_for_worker(tmp_path):
    entered, release = Event(), Event()

    def provider():
        entered.set()
        release.wait(3)
        return dict(foreground_pid=42, keys={'W'}, cursor=(1, 2))

    recorder = MovementRecorder(tmp_path, provider)
    recorder.start(tmp_path / 'pending', 42)
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError):
            recorder.export_members()
        started = time.monotonic()
        recorder.stop()
        assert time.monotonic() - started < 1.2
        assert recorder.status()['running']
        with pytest.raises(RuntimeError):
            recorder.start(tmp_path / 'other', 42)
        with pytest.raises(RuntimeError):
            recorder.export_members()
    finally:
        release.set()
        recorder.close()
    assert recorder.status()['rows'] == 0
    assert recorder.status()['running'] is False
    assert rows(tmp_path / 'pending/movement/samples.jsonl') == []


def test_export_is_fixed_allowlist_reuse_is_rejected_and_errors_do_not_leak(tmp_path, monkeypatch):
    def provider():
        raise ValueError('private app text and secret')

    recorder = MovementRecorder(tmp_path, provider)
    assert recorder.export_members() == []
    recorder.start(tmp_path / 'error', 42)
    wait_for(lambda: not recorder.status()['running'])
    recorder.close()
    directory = tmp_path / 'error/movement'
    (directory / 'packet_settings.json').write_text('secret')
    (directory / 'other.jsonl').write_text('secret')
    assert {name for _, name in recorder.export_members()} == {'movement/manifest.json', 'movement/samples.jsonl'}
    assert recorder.status()['error'] == 'recording_error:ValueError'
    assert 'private app' not in (directory / 'manifest.json').read_text(encoding='utf-8')
    with pytest.raises(ValueError):
        recorder.start(tmp_path / 'error', 42)
    (tmp_path / 'empty/movement').mkdir(parents=True)
    with pytest.raises(ValueError):
        recorder.start(tmp_path / 'empty', 42)
    for pid in (0, -1, True, '42'):
        with pytest.raises(ValueError):
            recorder.start(tmp_path / 'invalid', pid)
    # Windows users need no symlink privilege to run the privacy tests.
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink',
                        lambda path: path == directory / 'samples.jsonl' or original(path))
    with pytest.raises(ValueError):
        recorder.export_members()


class FakeFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def fake_windows(monkeypatch, pids):
    state = SimpleNamespace(key_codes=[], cursor_calls=0, pids=iter(pids), last_pid=0)

    def foreground():
        state.last_pid = next(state.pids, state.last_pid)
        return state.last_pid

    def pid(handle, output):
        output._obj.value = handle
        return 1

    def key(code):
        state.key_codes.append(code)
        return 0x8000 if code in (0x57, 0x02) else 0

    def cursor(output):
        state.cursor_calls += 1
        output._obj.x, output._obj.y = 30, 40
        return 1

    library = SimpleNamespace(**{name: FakeFunction(fn) for name, fn in {
        'GetForegroundWindow': foreground, 'GetWindowThreadProcessId': pid,
        'GetAsyncKeyState': key, 'GetCursorPos': cursor,
    }.items()})
    monkeypatch.setattr(movement_evidence, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: library, raising=False)
    return state


def test_windows_reader_never_polls_other_foreground_and_uses_only_six_allowed_states(monkeypatch):
    state = fake_windows(monkeypatch, [99])
    assert movement_evidence._WindowsState(42)() == {'foreground_pid': 0}
    assert state.key_codes == [] and state.cursor_calls == 0
    state = fake_windows(monkeypatch, [42])
    source = movement_evidence._WindowsState(42)()
    assert source == dict(foreground_pid=42, keys={'W'}, buttons={'right'}, cursor=(30, 40))
    assert state.key_codes == [0x57, 0x41, 0x53, 0x44, 0x01, 0x02]
    assert state.cursor_calls == 1


def test_windows_focus_change_discards_state_and_stops_polling(monkeypatch):
    state = fake_windows(monkeypatch, [42, 42, 99])
    assert movement_evidence._WindowsState(42)() == {'foreground_pid': 0}
    assert state.key_codes == [0x57] and state.cursor_calls == 0
    state = fake_windows(monkeypatch, [42] * 8 + [99])
    assert movement_evidence._WindowsState(42)() == {'foreground_pid': 0}
    assert state.cursor_calls == 1


def test_module_windows_surface_has_no_input_writers_or_hooks():
    tree = ast.parse(Path(movement_evidence.__file__).read_text(encoding='utf-8'))
    native_names = {node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == 'user'}
    assert native_names == {'GetForegroundWindow', 'GetWindowThreadProcessId', 'GetAsyncKeyState', 'GetCursorPos'}
    assert MovementRecorder.INTERVAL == .05
    assert MovementRecorder.MAX_SECONDS == 90.
    assert MovementRecorder.MAX_ROWS == 2000
    assert MovementRecorder.MAX_BYTES == 256 * 1024
