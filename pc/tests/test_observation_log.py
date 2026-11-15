"""Portable diagnostic archives must remain bounded and valid during observation."""
import io
import json
from pathlib import Path, PurePosixPath
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import zipfile

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observation_log import ObservationLog


def _jsonl(directory):
    files = list(directory.glob('events*.jsonl'))
    records = []
    for path in files:
        records.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line)
    return files, records


def test_export_reports_ocr_completion_separately_from_display(tmp_path):
    log = ObservationLog(tmp_path / 'diagnostics')
    log.event('completed', scan_id=1, coordinates=[-1.88, 162.35], element='불', processing_seconds=4.1)
    log.event('stale', scan_id=1, reason='capture_age')
    log.event('completed', scan_id=2, coordinates=[-1.87, 162.35], element='불', processing_seconds=.9)
    log.event('displayed', scan_id=2)
    log.event('error', scan_id=3, message='OCR unavailable')
    destination = tmp_path / 'diagnostics.zip'
    summary = log.export(destination)
    assert log.error is None
    counts = summary['event_counts']
    assert {kind: counts[kind] for kind in ('completed', 'displayed', 'stale', 'error')} == {
        'completed': 2, 'displayed': 1, 'stale': 1, 'error': 1,
    }
    with zipfile.ZipFile(destination) as archive:
        assert json.loads(archive.read('summary.json'))['event_counts'] == counts
        assert isinstance(json.loads(archive.read('manifest.json')), dict)
    _, records = _jsonl(log.directory)
    completion = next(row for row in records if row.get('scan_id') == 1)
    assert completion['coordinates'] == [-1.88, 162.35]


def test_event_rotation_bounds_disk_usage_without_broken_json(tmp_path):
    log = ObservationLog(tmp_path / 'diagnostics', max_events_bytes=1024)
    for sequence in range(100):
        log.event('completed', scan_id=sequence, text='인식 검사 ' * 10)
    files, records = _jsonl(log.directory)
    assert 1 <= len(files) <= 3
    assert all(path.stat().st_size <= 1024 for path in files)
    assert records
    assert any(row.get('scan_id') == 99 for row in records)
    assert len(records) < 100
    summary = log.export(tmp_path / 'rotated.zip')
    # Counters describe the session, including events rotated out of the archive.
    assert summary['event_counts']['completed'] == 100


def test_export_allowlist_and_bounded_coordinate_window_frames(tmp_path):
    log = ObservationLog(tmp_path / 'diagnostics', image_slots=2)
    rgb = np.full((45, 63, 3), (20, 40, 60), dtype=np.uint8)
    names = [log.frame(index, rgb) for index in range(5)]
    assert all(name is not None for name in names)
    assert len(set(names)) <= 2
    for name in names:
        member = PurePosixPath(name)
        assert not member.is_absolute() and '..' not in member.parts
        assert member.parts[0] == 'frames'
    log.event('completed', scan_id=4)
    for name in ('secret.txt', 'abyss.log', 'packet_settings.json'):
        (log.directory / name).write_text('must not be exported', encoding='utf-8')
    (log.directory / 'frames' / 'secret.txt').write_text('not a frame', encoding='utf-8')
    destination = log.directory / 'support.zip'
    log.export(destination)
    assert log.error is None
    with zipfile.ZipFile(destination) as archive:
        members = archive.namelist()
        assert 'manifest.json' in members and 'summary.json' in members
        for name in members:
            member = PurePosixPath(name)
            assert not member.is_absolute() and '..' not in member.parts and '\\' not in name
            assert name in ('manifest.json', 'summary.json', 'README.txt') or (
                len(member.parts) == 1 and member.name.startswith('events') and member.suffix == '.jsonl'
            ) or (len(member.parts) == 2 and member.parts[0] == 'frames' and member.suffix == '.png')
        frames = [name for name in members if name.startswith('frames/')]
        assert 1 <= len(frames) <= 2
        for name in frames:
            with Image.open(io.BytesIO(archive.read(name))) as image:
                assert image.size == (63, 45)
                assert image.convert('RGB').getpixel((0, 0)) == (20, 40, 60)


def test_failed_export_preserves_existing_target(tmp_path, monkeypatch):
    log = ObservationLog(tmp_path / 'diagnostics')
    log.event('completed', scan_id=1)
    destination = tmp_path / 'support.zip'
    destination.write_bytes(b'previous diagnostic archive')
    original = zipfile.ZipFile.writestr
    calls = 0

    def fail_while_writing(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError('simulated disk full')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'writestr', fail_while_writing)
    with pytest.raises(OSError, match='simulated disk full'):
        log.export(destination)
    assert log.error
    assert destination.read_bytes() == b'previous diagnostic archive'
    assert not list(tmp_path.glob('support.zip.*.tmp'))


def test_unwritable_directory_reports_error_without_crashing_observation(tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('existing file', encoding='utf-8')
    log = ObservationLog(blocked)
    log.event('error', scan_id=1, message='test')
    assert log.error
    assert blocked.read_text(encoding='utf-8') == 'existing file'


def test_export_during_recording_contains_complete_json_and_images(tmp_path):
    log = ObservationLog(tmp_path / 'diagnostics', max_events_bytes=4096, image_slots=2)
    start = Event()
    rgb = np.full((24, 32, 3), 17, dtype=np.uint8)

    def record(offset):
        assert start.wait(2)
        for index in range(40):
            log.event('completed', scan_id=offset + index)
            if index % 10 == 0:
                log.frame(offset + index, rgb)

    with ThreadPoolExecutor(max_workers=3) as pool:
        writers = [pool.submit(record, offset) for offset in (0, 1000)]
        start.set()
        for index in range(5):
            destination = tmp_path / f'concurrent-{index}.zip'
            log.export(destination)
            with zipfile.ZipFile(destination) as archive:
                assert archive.testzip() is None
                json.loads(archive.read('manifest.json'))
                json.loads(archive.read('summary.json'))
                for name in archive.namelist():
                    if name.endswith('.jsonl'):
                        for line in archive.read(name).decode('utf-8').splitlines():
                            json.loads(line)
                    elif name.endswith('.png'):
                        with Image.open(io.BytesIO(archive.read(name))) as image:
                            image.verify()
        for writer in writers:
            writer.result()
    assert log.error is None
    summary = log.export(tmp_path / 'complete.zip')
    assert summary['event_counts']['completed'] == 80


def test_over_limit_images_are_skipped_without_scaling_or_disk_growth(tmp_path):
    log = ObservationLog(tmp_path / 'diagnostics', max_image_bytes=1)
    assert log.frame(1, np.zeros((24, 32, 3), dtype=np.uint8)) is None
    assert log.frame(2, np.zeros((3, 2049, 3), dtype=np.uint8)) is None
    assert log.error is None
    assert not list(log.directory.glob('frames/*.png'))
    _, events = _jsonl(log.directory)
    skipped = [event for event in events if event.get('event') == 'image_skipped']
    assert {event['reason'] for event in skipped} == {'byte_limit', 'dimension_limit'}


def test_export_can_exclude_images_already_saved_in_session(tmp_path):
    log = ObservationLog(tmp_path / 'diagnostics')
    image_name = log.frame(7, np.full((24, 32, 3), 51, dtype=np.uint8))
    assert image_name and (log.directory / image_name).is_file()
    log.event('completed', scan_id=7, image=image_name)
    destination = tmp_path / 'text-only.zip'
    log.export(destination, include_images=False)
    assert log.error is None
    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['images'] == {}
        assert not any(name.startswith('frames/') or name.endswith('.png') for name in archive.namelist())
        assert any(name.endswith('.jsonl') for name in archive.namelist())
    # Excluding from export does not silently delete the local diagnostic evidence.
    assert (log.directory / image_name).is_file()
