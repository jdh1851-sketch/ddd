"""Coordinate-window evidence stays bounded and separate from packet/control state."""
import ast
import ctypes
import hashlib
import io
import json
from pathlib import Path
import sys
from threading import Event
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import packet_evidence
from packet_evidence import EvidenceRecorder


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('evidence worker did not reach expected state')


def test_native_pixels_timestamps_hashes_and_fixed_export_allowlist(tmp_path):
    rgb = np.full((31, 47, 3), (11, 27, 93), dtype=np.uint8)
    provider = lambda: (rgb, dict(source='synthetic', window_title='좌표 미니맵',
                                  client_rect=[5, 8, 47, 31], private_settings='do not export'))
    recorder = EvidenceRecorder(tmp_path, provider, interval=.01)
    recorder.start(tmp_path / 'session')
    try:
        wait_for(lambda: recorder.status()['captured'] >= 2)
    finally:
        recorder.close()
    directory = tmp_path / 'session' / 'evidence'
    (directory / 'packet_settings.json').write_text('secret')
    (directory / 'frames' / 'other.png').write_text('not a captured frame')
    members = recorder.export_members()
    names = [name for _, name in members]
    assert 'evidence/manifest.json' in names and 'evidence/events.jsonl' in names
    assert not any('packet_settings' in name or 'other.png' in name for name in names)
    assert all(not Path(name).is_absolute() and '..' not in Path(name).parts and '\\' not in name for name in names)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status']['game_input_allowed'] is False
    assert manifest['scope'] == 'existing_coordinate_window_only'
    assert 'private_settings' not in json.dumps(manifest)
    for name, record in manifest['images'].items():
        data = (directory / name).read_bytes()
        assert record['sha256'] == hashlib.sha256(data).hexdigest()
        assert record['capture_started_epoch'] <= record['captured_epoch'] <= record['capture_finished_epoch']
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (47, 31)
            assert image.getpixel((0, 0)) == (11, 27, 93)


def test_ring_and_journal_are_bounded_and_slots_have_current_frame_identity(tmp_path):
    count = 0

    def provider():
        nonlocal count
        count += 1
        return np.full((24, 32, 3), count % 255, np.uint8), {}

    recorder = EvidenceRecorder(tmp_path, provider, interval=.01, image_slots=2,
                                max_events=4, max_event_bytes=2048)
    recorder.start(tmp_path / 'bounded')
    try:
        wait_for(lambda: recorder.status()['captured'] >= 9)
    finally:
        recorder.close()
    recorder.export_members()
    directory = tmp_path / 'bounded' / 'evidence'
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert len(manifest['images']) == 2
    assert len(list((directory / 'frames').glob('*.png'))) == 2
    for name, record in manifest['images'].items():
        with Image.open(directory / name) as image:
            assert image.getpixel((0, 0))[0] == record['frame_id'] % 255
    journal = (directory / 'events.jsonl').read_bytes()
    records = [json.loads(line) for line in journal.splitlines()]
    assert len(journal) <= 2048 and len(records) <= 4
    assert records[-1]['event'] == 'session_stopped'
    assert not list(directory.rglob('*.tmp'))


def test_unavailable_window_skips_then_recovers_without_stopping_other_work(tmp_path):
    calls = 0

    def provider():
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise ValueError('다른 창이 좌표 창을 가리고 있습니다.')
        return np.zeros((24, 32, 3), np.uint8), {}

    recorder = EvidenceRecorder(tmp_path, provider, interval=.01)
    recorder.start(tmp_path / 'recover')
    try:
        wait_for(lambda: recorder.status()['captured'] >= 1)
        assert recorder.status()['running'] and recorder.status()['error'] is None
    finally:
        recorder.close()
    status = recorder.status()
    assert status['skipped'] == 3 and status['window_state'] == 'available'
    records = [json.loads(line) for line in (tmp_path / 'recover/evidence/events.jsonl').read_text(encoding='utf-8').splitlines()]
    skipped = [row for row in records if row['event'] == 'skipped']
    assert len(skipped) == 3 and all('가리고' in row['reason'] for row in skipped)


@pytest.mark.parametrize('shape,limit,reason', [
    ((2049, 2, 3), 2097152, 'dimension_limit'),
    ((24, 32, 3), 10, 'image_byte_limit'),
])
def test_invalid_dimensions_and_image_byte_limits_do_not_write_images(tmp_path, shape, limit, reason):
    recorder = EvidenceRecorder(tmp_path, lambda: (np.zeros(shape, np.uint8), {}),
                                interval=.01, max_image_bytes=limit)
    recorder.start(tmp_path / reason)
    try:
        wait_for(lambda: recorder.status()['skipped'] >= 1)
    finally:
        recorder.close()
    assert recorder.status()['captured'] == 0
    assert recorder.status()['last_skip_reason'] == reason
    assert not any(name.endswith('.png') for _, name in recorder.export_members())


def test_total_image_budget_caps_all_slots(tmp_path):
    rgb = np.zeros((24, 32, 3), np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format='PNG')
    one_size = len(buffer.getvalue())
    recorder = EvidenceRecorder(tmp_path, lambda: (rgb, {}), interval=.01,
                                max_total_image_bytes=one_size, image_slots=30)
    recorder.start(tmp_path / 'total')
    try:
        wait_for(lambda: recorder.status()['skipped'] >= 3)
    finally:
        recorder.close()
    assert recorder.status()['image_files'] == 1
    assert recorder.status()['image_bytes'] <= one_size
    assert recorder.status()['last_skip_reason'] == 'total_image_byte_limit'


def test_pending_capture_cannot_export_or_leak_a_late_result(tmp_path):
    entered, release = Event(), Event()

    def provider():
        entered.set()
        assert release.wait(2)
        return np.zeros((24, 32, 3), np.uint8), {}

    recorder = EvidenceRecorder(tmp_path, provider)
    started = time.monotonic()
    recorder.start(tmp_path / 'pending')
    assert time.monotonic() - started < .2
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError):
            recorder.export_members()
        with pytest.raises(RuntimeError):
            recorder.start(tmp_path / 'other-session')
        recorder.stop()
    finally:
        release.set()
        recorder.close()
    assert recorder.status()['captured'] == 0
    assert recorder.status()['last_skip_reason'] == 'stopped_during_capture'
    assert not any(name.endswith('.png') for _, name in recorder.export_members())


def test_storage_failure_is_reported_without_raising_from_worker(tmp_path):
    blocked = tmp_path / 'blocked'
    blocked.write_text('existing user file')
    recorder = EvidenceRecorder(tmp_path, lambda: (np.zeros((24, 32, 3), np.uint8), {}))
    recorder.start(blocked)
    wait_for(lambda: not recorder.status()['running'])
    recorder.close()
    assert recorder.status()['error']
    assert recorder.export_members() == []
    assert blocked.read_text() == 'existing user file'


class _FakeFunction:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


def _fake_user32(monkeypatch, *, obscured_after_grab=False, count=1):
    state = SimpleNamespace(obscured=False, grabs=[])

    def enum(callback, _):
        for index in range(count):
            callback(42 + index, 0)
        return 1

    def title(_, output, size):
        output.value = '좌표 미니맵'
        return len(output.value)

    def client_rect(_, output):
        rect = output._obj
        rect.left = rect.top = 0
        rect.right, rect.bottom = 120, 140
        return 1

    def client_screen(_, output):
        output._obj.x, output._obj.y = 5, 8
        return 1

    lib = SimpleNamespace(**{name: _FakeFunction(fn) for name, fn in {
        'EnumWindows': enum, 'GetWindowTextLengthW': lambda _: 10, 'GetWindowTextW': title,
        'IsWindowVisible': lambda _: 1, 'IsWindow': lambda _: 1, 'IsIconic': lambda _: 0,
        'GetClientRect': client_rect, 'ClientToScreen': client_screen,
        'WindowFromPoint': lambda point: 99 if state.obscured else 77,
        'GetAncestor': lambda handle, _: 42 if handle == 77 else 99,
    }.items()})
    monkeypatch.setattr(packet_evidence, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **kw: lib, raising=False)
    monkeypatch.setattr(ctypes, 'WINFUNCTYPE', lambda *a: lambda fn: fn, raising=False)

    def grab(**kwargs):
        state.grabs.append(kwargs)
        if obscured_after_grab:
            state.obscured = True
        return Image.new('RGB', (120, 140), (10, 20, 30))

    monkeypatch.setattr(packet_evidence.ImageGrab, 'grab', grab)
    return state


def test_windows_provider_uses_only_checked_coordinate_client_bbox(monkeypatch):
    state = _fake_user32(monkeypatch)
    rgb, metadata = packet_evidence.capture_coordinate_window()
    assert state.grabs == [dict(bbox=(5, 8, 125, 148), all_screens=True)]
    assert rgb.shape == (140, 120, 3)
    assert metadata['client_rect'] == [5, 8, 120, 140]


@pytest.mark.parametrize('count', [0, 2])
def test_missing_or_ambiguous_coordinate_window_never_captures(monkeypatch, count):
    state = _fake_user32(monkeypatch, count=count)
    with pytest.raises(ValueError):
        packet_evidence.capture_coordinate_window()
    assert state.grabs == []


def test_window_covered_during_grab_is_discarded(monkeypatch):
    state = _fake_user32(monkeypatch, obscured_after_grab=True)
    with pytest.raises(ValueError, match='가리고'):
        packet_evidence.capture_coordinate_window()
    assert len(state.grabs) == 1


def test_evidence_module_has_no_game_or_network_mutation_api():
    tree = ast.parse(Path(packet_evidence.__file__).read_text(encoding='utf-8'))
    forbidden = {'SendInput', 'keybd_event', 'mouse_event', 'SetCursorPos', 'pcap_sendpacket',
                 'pcap_inject', 'connect', 'send', 'sendall', 'SetWindowPos', 'SetWindowTextW'}
    assert not any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree))
