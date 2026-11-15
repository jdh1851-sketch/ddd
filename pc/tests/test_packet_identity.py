"""Independent coordinate-window corroboration; no game inputs or private images."""
import time
from threading import Event
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packet_identity import IdentityTracker


def waypoint(index):
    return float(index % 3), 150. + float(index % 3)


def sample(entity, index, timestamp, *, stream='flow-a', generation=0, source='adapter-a'):
    x, y = waypoint(index)
    return dict(entity_id=entity, x=x, y=y, heading=0., timestamp=timestamp,
                stream_id=stream, generation=generation, source_id=source)


def add_trace(tracker, entity=987654, *, count=8, interval=1., base=None,
              stream='flow-a', generation=0, source='adapter-a', observations=True):
    base = time.time()-9 if base is None else base
    for index in range(count):
        at = base + index*interval
        tracker.add_position(sample(entity, index, at, stream=stream,
                                    generation=generation, source=source))
        if observations:
            tracker.observe_coordinates(waypoint(index), at, f'frame-{index}')
    return tracker.snapshot()


def assert_no_input(result):
    assert result['game_input_allowed'] is False
    assert result['scope'] == 'coordinate_window_correlated'


def test_positions_alone_do_not_establish_self_identity():
    tracker = IdentityTracker()
    try:
        result = add_trace(tracker, observations=False)
        assert not result['confirmed'] and result['self_entity_id'] is None
        assert_no_input(result)
    finally: tracker.close()


def test_unique_moving_entity_needs_independent_coordinate_observations():
    tracker = IdentityTracker()
    try:
        result = add_trace(tracker)
        assert result['confirmed'] and result['self_entity_id'] == 987654
        assert result['match_count'] >= 8
        assert result['distinct_waypoints'] >= 3
        assert result['span_seconds'] >= 5
        assert_no_input(result)
    finally: tracker.close()


def test_seven_frames_are_insufficient_even_with_motion_and_time():
    tracker = IdentityTracker()
    try:
        result = add_trace(tracker, count=7)
        assert not result['confirmed'] and result['self_entity_id'] is None
    finally: tracker.close()


def test_eight_repetitions_of_one_point_are_not_independent_motion_evidence():
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            point = sample(23, 0, base+index)
            tracker.add_position(point)
            tracker.observe_coordinates(waypoint(0), base+index, f'frame-{index}')
        result = tracker.snapshot()
        assert not result['confirmed']
        assert_no_input(result)
    finally: tracker.close()


def test_two_spatial_clusters_do_not_meet_three_cluster_requirement():
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            point_index = index % 2
            tracker.add_position(sample(23, point_index, base+index))
            tracker.observe_coordinates(waypoint(point_index), base+index, f'frame-{index}')
        assert not tracker.snapshot()['confirmed']
    finally: tracker.close()


def test_short_burst_is_not_five_seconds_of_corroboration():
    tracker = IdentityTracker()
    try:
        assert not add_trace(tracker, interval=.1)['confirmed']
    finally: tracker.close()


def test_two_entities_matching_every_observation_remain_ambiguous():
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            at = base+index
            tracker.add_position(sample(101, index, at))
            tracker.add_position(sample(202, index, at))
            tracker.observe_coordinates(waypoint(index), at, f'frame-{index}')
        result = tracker.snapshot()
        assert not result['confirmed'] and result['self_entity_id'] is None
        assert set(result['ambiguous_ids']) == {101, 202}
        assert_no_input(result)
    finally: tracker.close()


@pytest.mark.parametrize('shift', [-4., .2])
def test_stale_or_future_position_cannot_match_frame(shift):
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            at = base+index
            tracker.add_position(sample(33, index, at+shift))
            tracker.observe_coordinates(waypoint(index), at, f'frame-{index}',
                capture_started_epoch=at, capture_finished_epoch=at+.05)
        result = tracker.snapshot()
        assert not result['confirmed'] and result['self_entity_id'] is None
    finally: tracker.close()


def test_generation_boundaries_do_not_combine_separate_evidence_runs():
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            at = base+index; generation = 0 if index < 4 else 1
            tracker.add_position(sample(101, index, at, generation=generation))
            tracker.observe_coordinates(waypoint(index), at, f'frame-{index}')
        assert not tracker.snapshot()['confirmed']
    finally: tracker.close()


def test_confirmed_identity_is_not_reused_after_connection_generation_changes():
    tracker = IdentityTracker()
    try:
        assert add_trace(tracker)['confirmed']
        tracker.add_position(sample(987654, 0, time.time(), generation=1))
        result = tracker.snapshot()
        assert not result['confirmed'] and result['self_entity_id'] is None
    finally: tracker.close()


def test_two_adapters_or_streams_do_not_silently_combine_identity_evidence():
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            at = base+index
            tracker.add_position(sample(101, index, at,
                stream='flow-a' if index < 4 else 'flow-b',
                source='adapter-a' if index < 4 else 'adapter-b'))
            tracker.observe_coordinates(waypoint(index), at, f'frame-{index}')
        assert not tracker.snapshot()['confirmed']
    finally: tracker.close()


def wait_until(predicate, timeout=2.):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.005)
    pytest.fail('identity OCR worker did not finish')


def test_busy_ocr_does_not_block_or_queue_more_frames():
    entered = Event(); release = Event(); calls = []
    def reader(rgb):
        calls.append(1); entered.set(); release.wait(2); return (0., 150.)
    tracker = IdentityTracker(coord_reader=reader); now = time.time()
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    try:
        tracker.add_position(sample(101, 0, now))
        started = time.monotonic()
        tracker.observe_frame(image, now, 'first')
        assert entered.wait(1)
        for index in range(20): tracker.observe_frame(image, now, f'busy-{index}')
        assert time.monotonic()-started < .2
        assert len(calls) == 1
    finally: release.set(); tracker.close()


def test_stop_discards_late_ocr_completion_and_returns_quickly():
    entered = Event(); release = Event()
    def reader(rgb):
        entered.set(); release.wait(2); return (0., 150.)
    tracker = IdentityTracker(coord_reader=reader); now = time.time()
    try:
        tracker.add_position(sample(101, 0, now))
        tracker.observe_frame(np.zeros((80, 120, 3), dtype=np.uint8), now, 'late')
        assert entered.wait(1)
        started = time.monotonic(); tracker.stop()
        assert time.monotonic()-started < .1
        release.set(); time.sleep(.04)
        result = tracker.snapshot()
        assert not result['confirmed']
        assert result['match_count'] == 0
        assert_no_input(result)
    finally: release.set(); tracker.close()


def test_none_coordinate_reading_does_not_vote_for_any_entity():
    tracker = IdentityTracker()
    try:
        at = time.time()
        tracker.add_position(sample(101, 0, at))
        tracker.observe_coordinates(None, at, 'unreadable')
        result = tracker.snapshot()
        assert not result['confirmed'] and result['match_count'] == 0
    finally: tracker.close()


def test_reusing_one_frame_id_cannot_become_eight_independent_frames():
    tracker = IdentityTracker(); base = time.time()-9
    try:
        for index in range(8):
            at = base+index
            tracker.add_position(sample(101, index, at))
            tracker.observe_coordinates(waypoint(index), at, 'same-frame')
        assert not tracker.snapshot()['confirmed']
    finally: tracker.close()


def test_position_and_observation_history_are_bounded():
    tracker = IdentityTracker(max_positions=6, max_observations=6); base = time.time()-20
    try:
        for index in range(15):
            at = base+index
            tracker.add_position(sample(101, index, at))
            tracker.observe_coordinates(waypoint(index), at, f'frame-{index}')
        assert len(tracker.position_samples()) <= 6
        assert len(tracker.snapshot()['observations']) <= 6
        assert not tracker.snapshot()['confirmed']
    finally: tracker.close()


def evidence_archive(path, *, corrupt=None, count=8):
    import hashlib
    import io
    import json
    import zipfile
    from PIL import Image
    base = time.time()-9
    images = {}; contents = {}; samples = []
    for index in range(count):
        at = base+index
        rgb = np.zeros((120, 200, 3), dtype=np.uint8); rgb[0, 0, 0] = index
        stream = io.BytesIO(); Image.fromarray(rgb).save(stream, format='PNG')
        data = stream.getvalue(); name = f'frames/frame-{index+1:02d}.png'
        record = dict(frame_id=index+1, width=200, height=120,
            sha256=hashlib.sha256(data).hexdigest(),
            capture_started_epoch=at-.01, capture_finished_epoch=at+.01,
            captured_epoch=at, source=dict(source='existing_coordinate_window',
                window_title='좌표 미니맵', client_rect=[100, 200, 200, 120]))
        if index == 0:
            if corrupt == 'hash': record['sha256'] = '0' * 64
            elif corrupt == 'source': record['source']['source'] = 'whole_desktop'
            elif corrupt == 'title': record['source']['window_title'] = 'arbitrary image'
            elif corrupt == 'dimensions': record['width'] = 201
            elif corrupt == 'path': name = '../outside.png'
        images[name] = record; contents['evidence/'+name] = data
        samples.append(sample(987654, index, at))
    manifest = dict(schema_version=1, session_id='synthetic-session', status={}, images=images)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('metadata.json', '{}')
        z.writestr('evidence/manifest.json', json.dumps(manifest))
        for name, data in contents.items(): z.writestr(name, data)
    return samples


def pixel_coordinate_reader(rgb):
    return waypoint(int(rgb[0, 0, 0]))


def test_verified_native_window_evidence_can_correlate_without_extracting_files(tmp_path):
    from packet_identity import correlate_evidence
    path = tmp_path/'evidence.zip'; samples = evidence_archive(path)
    result = correlate_evidence(path, samples, coord_reader=pixel_coordinate_reader)
    assert result['confirmed'] and result['self_entity_id'] == 987654
    assert not result['evidence_errors']
    assert_no_input(result)
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize('corrupt', ['hash', 'source', 'title', 'dimensions', 'path'])
def test_unverified_archive_evidence_cannot_confirm_identity(tmp_path, corrupt):
    from packet_identity import correlate_evidence
    path = tmp_path/'evidence.zip'; samples = evidence_archive(path, corrupt=corrupt)
    try:
        result = correlate_evidence(path, samples, coord_reader=pixel_coordinate_reader)
    except ValueError:
        result = None
    if result is not None:
        assert not result['confirmed'] and result['self_entity_id'] is None
        assert result['evidence_errors']
        assert_no_input(result)
    assert set(tmp_path.iterdir()) == {path}
    assert not (tmp_path.parent/'outside.png').exists()


def test_stopping_preserves_completed_corroboration_for_export():
    tracker = IdentityTracker()
    try:
        result = add_trace(tracker)
        assert result['confirmed']
        tracker.stop()
        saved = tracker.snapshot()
        assert saved['confirmed'] and saved['self_entity_id'] == result['self_entity_id']
        assert_no_input(saved)
    finally: tracker.close()


def test_default_coordinate_reader_matches_native_pixels_without_claiming_identity():
    from PIL import Image
    image = np.array(Image.open(Path(__file__).parent/'fixtures/native_fire.png').convert('RGB'))
    tracker = IdentityTracker(); at = time.time()
    try:
        point = dict(entity_id=7654321, x=-1.88, y=162.35, heading=0., timestamp=at,
                     stream_id='native-fixture', generation=0, source_id='synthetic')
        tracker.add_position(point)
        tracker.observe_frame(image, at, 'native-coordinate-only')
        wait_until(lambda: tracker.snapshot()['match_count'] == 1, timeout=5.)
        result = tracker.snapshot()
        assert not result['confirmed'] and result['self_entity_id'] is None
        assert_no_input(result)
    finally: tracker.close()


@pytest.mark.parametrize('filename,expected', [
    ('native_coordinate_zero_157.png', (0., 157.07)),
    ('native_coordinate_zero_161.png', (0., 161.62)),
])
def test_default_ocr_binary_retry_reads_original_zero_coordinate_pixels(filename, expected):
    from PIL import Image
    image = np.array(Image.open(Path(__file__).parent/'fixtures'/filename).convert('RGB'))
    tracker = IdentityTracker()
    try:
        result = tracker._read_coordinates(image)
        assert tuple(result['coordinates']) == expected
    finally: tracker.close()


def test_valid_baseline_coordinate_conflict_vetoes_binary_retry(monkeypatch):
    import spear_observation
    monkeypatch.setattr(spear_observation, 'coordinate_regions', lambda rgb: [(0, 0, 120, 80)])
    answers = iter(['X0,Y157.07', 'unreadable', 'X0,Y161.62', 'X0,Y161.62'])
    tracker = IdentityTracker()
    tracker._ocr = lambda *args: next(answers)
    try:
        result = tracker._read_coordinates(np.zeros((80, 120, 3), dtype=np.uint8))
        assert result['coordinates'] is None
    finally: tracker.close()


def test_binary_retry_can_confirm_exact_readings_after_invalid_baseline(monkeypatch):
    import spear_observation
    monkeypatch.setattr(spear_observation, 'coordinate_regions', lambda rgb: [(0, 0, 120, 80)])
    answers = iter(['X.0,Y161.62', 'unreadable', 'X0,Y161.62', 'X0,Y161.62'])
    shapes = []
    def fake_ocr(patch, *args):
        shapes.append(patch.ndim)
        return next(answers)
    tracker = IdentityTracker(); tracker._ocr = fake_ocr
    try:
        result = tracker._read_coordinates(np.zeros((80, 120, 3), dtype=np.uint8))
        assert tuple(result['coordinates']) == (0., 161.62)
        assert shapes == [3, 3, 2, 2]
        assert result['readings'][0]['preprocessing'] == 'binary_100_and_120_with_baseline_veto'
    finally: tracker.close()


def test_coordinate_search_budget_cannot_promote_an_earlier_region(monkeypatch):
    import packet_identity
    import spear_observation
    clock = [0.]
    monkeypatch.setattr(packet_identity.time, 'monotonic', lambda: clock[0])
    def regions(rgb):
        yield (0, 0, 60, 40)
        # The next region becomes available only after the total search deadline.
        clock[0] = 3.01
        yield (60, 0, 120, 40)
    monkeypatch.setattr(spear_observation, 'coordinate_regions', regions)
    calls = []
    def reader(*args):
        calls.append(1); clock[0] += 1.
        return 'X0,Y157.07'
    tracker = IdentityTracker(); tracker._ocr = reader
    try:
        result = tracker._read_coordinates(np.zeros((80, 120, 3), dtype=np.uint8))
        assert len(calls) == 2
        assert tuple(result['readings'][0]['coordinates']) == (0., 157.07)
        assert result['coordinates'] is None
        assert result['reason'] == 'coordinate_time_budget'
    finally: tracker.close()


@pytest.mark.parametrize('frame_count', [8, 9])
def test_archive_time_budget_revokes_partial_identity_and_stops_reading(tmp_path, monkeypatch, frame_count):
    import packet_identity
    path = tmp_path/'time-budget.zip'; samples = evidence_archive(path, count=frame_count)
    clock = [0.]; calls = []
    monkeypatch.setattr(packet_identity.time, 'monotonic', lambda: clock[0])
    def slow_reader(rgb):
        calls.append(1); clock[0] += 4.
        return pixel_coordinate_reader(rgb)
    result = packet_identity.correlate_evidence(path, samples, coord_reader=slow_reader)
    assert len(calls) == 8  # The eighth read crosses the deadline and cannot vote.
    assert result['archive_validation']['verified_images'] == 7
    assert any(error['reason'] == 'identity_analysis_time_budget' for error in result['evidence_errors'])
    assert not result['confirmed'] and result['self_entity_id'] is None
    assert_no_input(result)
