"""Exercise observer/worker diagnostics without a Tk root, OCR or game window."""
import json
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
import time

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minimap_ui import MinimapPanel
from observation_log import ObservationLog
from scan_async import LatestScan


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Preview:
    def __init__(self):
        self.options = {'image': 'previous target'}

    def configure(self, **options):
        self.options.update(options)


def panel_without_tk(directory, reader):
    panel = MinimapPanel.__new__(MinimapPanel)
    panel.worker = LatestScan()
    panel.reader = reader
    panel.log = ObservationLog(directory)
    panel.scan_number = 0
    panel.active_scan = None
    panel.source_kind = 'live'
    panel.reset_logged_scan = None
    panel.image_generation = 0
    panel.pending_image = None
    panel.displayed_at = None
    panel.save_images = Value(False)
    panel.log_status = Value()
    panel.report = Value()
    panel.detail = Value()
    panel.preview = Preview()
    return panel


def events(panel):
    rows = []
    # Read rotation files oldest first, retaining event order.
    for name in ('events.2.jsonl', 'events.1.jsonl', 'events.jsonl'):
        path = panel.log.directory / name
        if path.exists():
            rows.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines())
    return rows


def test_completion_survives_reset_but_stale_worker_result_is_not_promoted(tmp_path):
    entered = Event()
    release = Event()

    def read(rgb):
        entered.set()
        assert release.wait(3)
        return {'coordinates': [-1.88, 162.35], 'element': '불', 'partial': False}

    panel = panel_without_tk(tmp_path, SimpleNamespace(read=read))
    try:
        assert panel.submit(np.zeros((70, 90, 3), dtype=np.uint8), time.monotonic())
        assert entered.wait(2)
        job = panel.worker.future[0]
        panel.invalidate()
        panel.invalidate()  # Repeated stop/tab change must not double count a reset.
        release.set()
        job.result(timeout=3)
        rows = events(panel)
        completed = [row for row in rows if row['event'] == 'completed']
        assert len(completed) == 1 and completed[0]['scan_id'] == 1
        assert completed[0]['generation'] == 0
        assert completed[0]['result']['element'] == '불'
        assert len([row for row in rows if row['event'] == 'generation_reset']) == 1
        assert panel.poll(max_age=8) is None
        assert panel.active_scan is None
        rows = events(panel)
        assert rows[-1]['event'] == 'discarded'
        assert rows[-1]['scan_id'] == 1 and rows[-1]['reason'] == 'generation_reset'
        assert not any(row['event'] == 'displayed' for row in rows)
        assert panel.displayed_at is None
    finally:
        release.set()
        panel.close()


def test_reader_error_keeps_scan_id_and_evidence_and_cleans_active_token(tmp_path):
    def broken_reader(rgb):
        raise ValueError('simulated OCR failure')

    panel = panel_without_tk(tmp_path, SimpleNamespace(read=broken_reader))
    panel.save_images.set(True)
    try:
        assert panel.submit(np.zeros((70, 90, 3), dtype=np.uint8), time.monotonic())
        job = panel.worker.future[0]
        with pytest.raises(ValueError, match='simulated OCR failure'):
            job.result(timeout=3)
        with pytest.raises(ValueError, match='simulated OCR failure'):
            panel.poll(max_age=8)
        assert panel.worker.future is None and panel.active_scan is None
        failures = [row for row in events(panel) if row['event'] == 'error']
        assert len(failures) == 1
        error = failures[0]
        assert error['scan_id'] == 1 and error['generation'] == 0
        assert error['error_type'] == 'ValueError'
        assert error['message'] == 'simulated OCR failure'
        assert error['image'] and (panel.log.directory / error['image']).is_file()
        assert not any(row['event'] == 'completed' for row in events(panel))
    finally:
        panel.close()


def test_old_capture_is_logged_and_previous_target_cleared_without_display(tmp_path):
    panel = panel_without_tk(tmp_path, SimpleNamespace())
    panel.displayed_at = time.monotonic() - 4
    token = {'scan_id': 9, 'generation': 2, 'source': 'live'}
    try:
        # An empty result cannot be rendered: the stale guard must return before
        # reading it, drawing annotations or constructing a Tk PhotoImage.
        panel.render(np.zeros((70, 90, 3), dtype=np.uint8), {},
                     time.monotonic() - 4, token, live=True)
        rows = events(panel)
        stale = [row for row in rows if row['event'] == 'stale']
        assert len(stale) == 1 and stale[0]['scan_id'] == 9
        assert stale[0]['capture_age_seconds'] >= 4
        assert not any(row['event'] == 'displayed' for row in rows)
        assert panel.displayed_at is None
        assert panel.preview.options['image'] == ''
        assert '미확인' in panel.report.get()
    finally:
        panel.close()
