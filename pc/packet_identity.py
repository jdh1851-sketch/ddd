"""Read-only corroboration of an entity against synchronized coordinate pixels.

These thresholds establish diagnostic identity evidence. They are not movement,
arrival, camera or mechanic-success parameters. No game input is permitted here.
"""
from bisect import bisect_right
from collections import defaultdict, deque
import hashlib
import hmac
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
from threading import RLock, Thread, current_thread
import zipfile

MAX_POSITIONS = 4000
MAX_OBSERVATIONS = 60
MAX_SAMPLE_AGE = 3.0
MAX_CAPTURE_DURATION = 1.0
SPATIAL_TOLERANCE = .03
MIN_MATCHES = 8
MIN_CLUSTERS = 3
CLUSTER_DISTANCE = .25
MIN_SPAN = 5.0


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _coordinates(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    if not all(_finite(v) and abs(v) < 1000000 for v in value):
        return None
    return [float(v) for v in value]


class IdentityTracker:
    def __init__(self, coord_reader=None, max_positions=MAX_POSITIONS,
                 max_observations=MAX_OBSERVATIONS):
        if not 1 <= int(max_positions) <= MAX_POSITIONS or not 1 <= int(max_observations) <= MAX_OBSERVATIONS:
            raise ValueError('Invalid identity evidence bounds')
        self.lock = RLock()
        self.positions = deque(maxlen=int(max_positions))
        self.observations = deque(maxlen=int(max_observations))
        self.coord_reader = coord_reader or self._read_coordinates
        self._ocr = None
        self._thread = None
        self._busy = False
        self._closed = False
        self._ocr_generation = 0
        self._version = 0
        self._cache_version = -1
        self._cache = None
        self._seen_frames = set()
        self._stream_generations = {}
        self.skipped_busy = 0
        self.dropped_pending = 0
        self._cache = self._calculate()

    def _read_coordinates(self, rgb):
        from spear_observation import TesseractTextReader, coordinate_regions
        from spear_reading_rules import coordinate_consensus, parse_coordinates
        import cv2
        if self._ocr is None:
            self._ocr = TesseractTextReader()
        regions = []
        accepted = []
        deadline=time.monotonic()+3.
        exhausted=False
        for box in coordinate_regions(rgb):
            if time.monotonic()>=deadline:
                exhausted=True;break
            x, y, x2, y2 = box
            patch = rgb[y:y2, x:x2]
            texts=[]
            for variant in ('rgb','gray'):
                if time.monotonic()>=deadline:exhausted=True;break
                texts.append(self._ocr(patch,'eng',7,variant))
            if exhausted:break
            value = coordinate_consensus(texts)
            preprocessing = 'rgb_and_gray'
            if value is None:
                valid_baseline = {parse_coordinates(text) for text in texts if parse_coordinates(text) is not None}
                # Retry image pixels, never repair decimal points or signs in text.
                if len(valid_baseline) <= 1:
                    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
                    binary_texts=[]
                    for threshold in (100,120):
                        if time.monotonic()>=deadline:exhausted=True;break
                        binary_texts.append(self._ocr(255-cv2.threshold(gray,threshold,255,cv2.THRESH_BINARY)[1],'eng',7,'rgb'))
                    if exhausted:break
                    retry = coordinate_consensus(binary_texts)
                    all_valid = {parse_coordinates(text) for text in texts + binary_texts if parse_coordinates(text) is not None}
                    texts += binary_texts
                    if retry is not None and len(all_valid) == 1:
                        value = retry
                        preprocessing = 'binary_100_and_120_with_baseline_veto'
            if time.monotonic()>=deadline:
                exhausted=True;break
            regions.append(dict(box=list(box), texts=[s[:100] for s in texts],
                                preprocessing=preprocessing, coordinates=value))
            if value is not None:
                accepted.append(tuple(value))
        values = set(accepted)
        if exhausted:return dict(coordinates=None,readings=regions,reason='coordinate_time_budget')
        return dict(coordinates=list(next(iter(values))) if len(values) == 1 else None,
                    readings=regions, reason='coordinate_consensus' if len(values) == 1 else 'coordinate_unconfirmed')

    def add_position(self, sample):
        try:
            entity = sample['entity_id']; timestamp = sample['timestamp']
            coords = _coordinates([sample['x'], sample['y']])
            stream = str(sample['stream_id']); source = str(sample.get('source_id', 'default'))
            generation = sample.get('generation', 0)
            if (isinstance(entity, bool) or not isinstance(entity, int) or not 0 < entity < 2**64 or
                    coords is None or not _finite(timestamp) or not stream or len(stream) > 128 or
                    len(source) > 128 or isinstance(generation, bool) or not isinstance(generation, int) or generation < 0):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        row = dict(entity_id=entity, x=coords[0], y=coords[1], timestamp=float(timestamp),
                   stream_id=stream, source_id=source, generation=generation)
        if _finite(sample.get('heading')):
            row['heading'] = float(sample['heading'])
        if isinstance(sample.get('source_type'), int):
            row['source_type'] = sample['source_type']
        with self.lock:
            if self._closed:
                return False
            scope = (source, stream)
            previous = self._stream_generations.get(scope)
            if previous is not None and generation < previous:
                return False
            if previous is not None and generation != previous:
                self.positions = deque((r for r in self.positions if (r['source_id'], r['stream_id']) != scope),
                                       maxlen=self.positions.maxlen)
                self._ocr_generation += 1
                self._version += 1
                self._invalidate('stream_generation_changed')
            self._stream_generations[scope] = generation
            self.positions.append(row)
        return True

    def reset_stream(self, stream_id):
        with self.lock:
            matching = (any(r['stream_id'] == str(stream_id) for r in self.positions) or
                        (self._cache.get('self_scope') or {}).get('stream_id') == str(stream_id))
            if matching:
                self.positions = deque((r for r in self.positions if r['stream_id'] != str(stream_id)),
                                       maxlen=self.positions.maxlen)
                self._ocr_generation += 1
                self._version += 1
                self._invalidate('stream_reset')
            self._stream_generations = {key: value for key, value in self._stream_generations.items()
                                        if key[1] != str(stream_id)}

    def reset(self):
        with self.lock:
            self._ocr_generation += 1
            self.positions.clear(); self.observations.clear()
            self._seen_frames.clear(); self._stream_generations.clear()
            self._version += 1
            self._invalidate('no_screen_observations')

    def position_samples(self):
        with self.lock:
            return json.loads(json.dumps(list(self.positions)))

    def _record(self, result, captured_epoch, frame_id, capture_started_epoch=None,
                capture_finished_epoch=None, token=None):
        started = captured_epoch if capture_started_epoch is None else capture_started_epoch
        finished = captured_epoch if capture_finished_epoch is None else capture_finished_epoch
        if (not all(_finite(v) for v in (started, captured_epoch, finished)) or
                not started <= captured_epoch <= finished or finished - started > MAX_CAPTURE_DURATION):
            return False
        if isinstance(result, dict):
            coords = _coordinates(result.get('coordinates'))
            readings = result.get('readings', [])
            # The coordinate-only OCR configuration cannot return character names.
            readings = json.loads(json.dumps(readings))[:12] if isinstance(readings, list) else []
            reason = str(result.get('reason', 'coordinate_consensus' if coords else 'coordinate_unconfirmed'))[:100]
        else:
            coords = _coordinates(result)
            readings = []
            reason = 'coordinate_consensus' if coords else 'coordinate_unconfirmed'
        frame_key = str(frame_id)[:128]
        row = dict(frame_id=frame_id if isinstance(frame_id, (int, str)) else frame_key,
                   captured_epoch=float(captured_epoch), capture_started_epoch=float(started),
                   capture_finished_epoch=float(finished), coordinates=coords,
                   readings=readings, reason=reason)
        with self.lock:
            if self._closed or (token is not None and token != self._ocr_generation):
                self.dropped_pending += 1
                return False
            if frame_key in self._seen_frames:
                return False
            self._seen_frames.add(frame_key)
            # Bound the deduplication set together with the bounded observations.
            if len(self._seen_frames) > 256:
                self._seen_frames = {str(r['frame_id']) for r in self.observations}
                self._seen_frames.add(frame_key)
            self.observations.append(row)
            self._version += 1
        self._calculate()
        return True

    def observe_coordinates(self, coords, captured_epoch, frame_id,
                            capture_started_epoch=None, capture_finished_epoch=None):
        return self._record(coords, captured_epoch, frame_id, capture_started_epoch, capture_finished_epoch)

    def observe_frame(self, rgb, captured_epoch, frame_id,
                      capture_started_epoch=None, capture_finished_epoch=None):
        import numpy as np
        if (not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3 or
                rgb.dtype != np.uint8 or not 1 <= rgb.shape[0] <= 2048 or not 1 <= rgb.shape[1] <= 2048):
            return False
        with self.lock:
            if self._closed:
                return False
            if self._busy:
                self.skipped_busy += 1
                return False
            self._busy = True
            token = self._ocr_generation
        pixels = rgb.copy()
        def work():
            try:
                try:
                    result = self.coord_reader(pixels)
                except Exception as exc:
                    result = dict(coordinates=None, reason='coordinate_reader_error',
                                  readings=[dict(error_type=type(exc).__name__)])
                self._record(result, captured_epoch, frame_id, capture_started_epoch, capture_finished_epoch, token)
            finally:
                with self.lock:
                    self._busy = False
        thread = Thread(target=work, daemon=True, name='coordinate-identity-reader')
        with self.lock:
            if token != self._ocr_generation or self._closed:
                self._busy = False
                return False
            self._thread = thread
            thread.start()
        return True

    def stop(self):
        with self.lock:
            self._ocr_generation += 1

    def close(self):
        self.stop()
        with self.lock:
            self._closed = True
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=3)

    def _calculate(self):
        with self.lock:
            if self._cache_version == self._version and self._cache is not None:
                result = json.loads(json.dumps(self._cache))
                result.update(ocr_pending=self._busy, skipped_busy=self.skipped_busy, dropped_pending=self.dropped_pending)
                return result
            positions = list(self.positions); observations = list(self.observations); version = self._version
        grouped = defaultdict(list)
        for row in positions:
            grouped[(row['source_id'], row['stream_id'], row['generation'], row['entity_id'])].append(row)
        for rows in grouped.values():
            rows.sort(key=lambda r: r['timestamp'])
        times = {key: [r['timestamp'] for r in rows] for key, rows in grouped.items()}
        matches = defaultdict(list); ambiguous_ids = set(); ambiguous_scopes = set(); checked = []
        for observation in observations:
            row = dict(observation); candidates = []
            if row['coordinates'] is not None:
                x, y = row['coordinates']
                for key, rows in grouped.items():
                    index = bisect_right(times[key], row['capture_finished_epoch']) - 1
                    if index < 0:
                        continue
                    sample = rows[index]
                    age = row['captured_epoch'] - sample['timestamp']
                    if sample['timestamp'] < row['capture_started_epoch'] - MAX_SAMPLE_AGE:
                        continue
                    error = math.hypot(sample['x'] - x, sample['y'] - y)
                    if error <= SPATIAL_TOLERANCE:
                        candidates.append((key, sample, age, error))
            row['matching_entities'] = sorted({key[3] for key, _, _, _ in candidates})
            if len(candidates) == 1:
                key, sample, age, error = candidates[0]
                row.update(matched_entity_id=key[3], packet_coordinates=[sample['x'], sample['y']],
                           sample_age_seconds=age, spatial_error=error, scope=[key[0], key[1], key[2]])
                matches[key].append(row)
            elif candidates:
                ambiguous_ids.update(row['matching_entities'])
                if len(row['matching_entities']) == 1:
                    ambiguous_scopes.update(str(key[:3]) for key, _, _, _ in candidates)
            checked.append(row)
        summaries = []
        for key, rows in matches.items():
            points = [tuple(row['packet_coordinates']) for row in rows]
            centers = []
            for point in points:
                if not any(math.dist(point, center) < CLUSTER_DISTANCE for center in centers):
                    centers.append(point)
            span = max(row['captured_epoch'] for row in rows) - min(row['captured_epoch'] for row in rows)
            summaries.append(dict(entity_id=key[3], source_id=key[0], stream_id=key[1], generation=key[2],
                                  match_count=len(rows), distinct_waypoints=len({(round(x, 5), round(y, 5)) for x, y in points}),
                                  spatial_clusters=len(centers), span_seconds=span,
                                  max_spatial_error=max(row['spatial_error'] for row in rows)))
        summaries.sort(key=lambda r: (r['match_count'], r['spatial_clusters'], r['span_seconds']), reverse=True)
        qualified = [r for r in summaries if r['match_count'] >= MIN_MATCHES and
                     r['spatial_clusters'] >= MIN_CLUSTERS and r['span_seconds'] >= MIN_SPAN]
        confirmed = len(qualified) == 1
        best = qualified[0] if confirmed else (summaries[0] if summaries else {})
        if len(qualified) > 1:
            ambiguous_ids.update(r['entity_id'] for r in qualified)
            reason = 'ambiguous_identity'
        elif confirmed:
            reason = 'coordinate_window_correlated'
        elif ambiguous_ids:
            reason = 'ambiguous_connection_scope' if ambiguous_scopes else 'ambiguous_identity'
        elif not observations:
            reason = 'no_screen_observations'
        elif not any(r['coordinates'] is not None for r in observations):
            reason = 'no_coordinate_consensus'
        elif not positions:
            reason = 'no_position_evidence'
        else:
            reason = 'insufficient_coordinate_evidence'
        result = dict(self_entity_id=best.get('entity_id') if confirmed else None, confirmed=confirmed,
                      scope='coordinate_window_correlated', match_count=best.get('match_count', 0),
                      distinct_waypoints=best.get('distinct_waypoints', 0), spatial_clusters=best.get('spatial_clusters', 0),
                      span_seconds=best.get('span_seconds', 0.), max_spatial_error=best.get('max_spatial_error'),
                      self_scope={k: best[k] for k in ('source_id', 'stream_id', 'generation')} if confirmed else None,
                      ambiguous_ids=sorted(ambiguous_ids), ambiguous_scopes=sorted(ambiguous_scopes),
                      observations=checked, candidates=summaries, reason=reason, game_input_allowed=False,
                      thresholds=dict(min_matches=MIN_MATCHES, min_spatial_clusters=MIN_CLUSTERS,
                                      min_span_seconds=MIN_SPAN, spatial_tolerance=SPATIAL_TOLERANCE,
                                      cluster_distance=CLUSTER_DISTANCE, max_sample_age_seconds=MAX_SAMPLE_AGE,
                                      max_capture_duration_seconds=MAX_CAPTURE_DURATION),
                      interpretation='identity evidence only; not movement or mechanic calibration')
        with self.lock:
            if version == self._version:
                self._cache = result; self._cache_version = version
            result = json.loads(json.dumps(result))
            result.update(ocr_pending=self._busy, skipped_busy=self.skipped_busy, dropped_pending=self.dropped_pending)
        return result


    def _invalidate(self, reason):
        # Called under lock. Revocation is immediate and never schedules OCR.
        cached = dict(self._cache or {})
        cached.update(self_entity_id=None, confirmed=False, self_scope=None,
                      match_count=0, distinct_waypoints=0, spatial_clusters=0,
                      span_seconds=0., max_spatial_error=None, candidates=[],
                      ambiguous_ids=[], ambiguous_scopes=[], reason=reason,
                      observations=json.loads(json.dumps(list(self.observations))))
        self._cache = cached
        self._cache_version = self._version

    def snapshot(self):
        # Correlation is computed by the OCR worker / observe_coordinates, not
        # by the UI poll loop. New positions are history until the next frame.
        with self.lock:
            result = json.loads(json.dumps(self._cache))
            result.update(ocr_pending=self._busy, skipped_busy=self.skipped_busy,
                          dropped_pending=self.dropped_pending)
            return result

def correlate_evidence(path, samples, coord_reader=None):
    """Correlate verified native evidence images without extracting ZIP paths."""
    import numpy as np
    from PIL import Image
    tracker = IdentityTracker(coord_reader=coord_reader)
    errors = []; verified = 0; manifest_valid = False; available = False
    deadline=time.monotonic()+30.
    try:
        for sample in samples:
            tracker.add_position(sample)
        path = Path(path)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Archive exceeds identity-analysis size limit')
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist(); names = [info.filename for info in infos]
            if len(infos) > 256 or len(names) != len(set(names)):
                raise ValueError('Invalid archive file list')
            if sum(info.file_size for info in infos) > 32 * 1024 * 1024:
                raise ValueError('Archive exceeds identity-analysis size limit')
            for info in infos:
                member = PurePosixPath(info.filename)
                if member.is_absolute() or '..' in member.parts or '\\' in info.filename:
                    raise ValueError('Unsafe archive path')
            if 'evidence/manifest.json' not in names:
                result = tracker.snapshot()
                result.update(archive_validation=dict(available=False, manifest_valid=False, verified_images=0),
                              evidence_errors=['missing_evidence_manifest'])
                return result
            available = True
            info = archive.getinfo('evidence/manifest.json')
            if info.file_size > 128 * 1024:
                raise ValueError('Evidence manifest exceeds limit')
            manifest = json.loads(archive.read(info))
            if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
                raise ValueError('Unsupported evidence manifest schema')
            images = manifest.get('images')
            if not isinstance(images, dict) or len(images) > 30:
                raise ValueError('Invalid evidence image list')
            if any(not isinstance(row, dict) or isinstance(row.get('frame_id'), bool) or
                   not isinstance(row.get('frame_id'), int) or row['frame_id'] < 0 for row in images.values()):
                raise ValueError('Invalid evidence frame ID')
            ids = [str(row.get('frame_id')) for row in images.values()]
            if len(ids) != len(images) or len(ids) != len(set(ids)):
                raise ValueError('Duplicate or invalid evidence frame ID')
            manifest_valid = True; total_image_bytes = 0
            for name, row in sorted(images.items(), key=lambda pair: pair[1].get('captured_epoch', 0)):
                if time.monotonic()>=deadline:
                    errors.append(dict(frame_id=row.get('frame_id'),reason='identity_analysis_time_budget'));break
                frame_id = row.get('frame_id')
                try:
                    if not isinstance(name, str) or re.fullmatch(r'frames/frame-\d{2}\.png', name) is None:
                        raise ValueError('unsafe_frame_name')
                    source = row.get('source', {})
                    if source.get('source') != 'existing_coordinate_window' or source.get('window_title') != '좌표 미니맵':
                        raise ValueError('unverified_coordinate_window_source')
                    image_info = archive.getinfo('evidence/' + name)
                    total_image_bytes += image_info.file_size
                    if image_info.file_size > 2 * 1024 * 1024 or total_image_bytes > 8 * 1024 * 1024:
                        raise ValueError('evidence_image_budget')
                    raw = archive.read(image_info)
                    digest = row.get('sha256')
                    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
                        raise ValueError('image_sha256_mismatch')
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.format != 'PNG' or image.size != (row.get('width'), row.get('height')) or not all(1 <= v <= 2048 for v in image.size):
                            raise ValueError('image_native_dimensions_mismatch')
                        rgb = np.array(image.convert('RGB'))
                    result = tracker.coord_reader(rgb)
                    if time.monotonic()>=deadline:
                        errors.append(dict(frame_id=frame_id,reason='identity_analysis_time_budget'));break
                    if not tracker._record(result, row.get('captured_epoch'), frame_id,
                                           row.get('capture_started_epoch'), row.get('capture_finished_epoch')):
                        raise ValueError('invalid_capture_timing_or_duplicate_frame')
                    verified += 1
                except (KeyError, ValueError, TypeError, OSError, RuntimeError) as exc:
                    errors.append(dict(frame_id=frame_id, reason=str(exc)[:160]))
        result = tracker.snapshot()
        if errors:
            result.update(confirmed=False,self_entity_id=None,self_scope=None,
                          reason='incomplete_or_invalid_evidence')
        result.update(archive_validation=dict(available=available, manifest_valid=manifest_valid,
                                             verified_images=verified, all_images_verified=not errors),
                      evidence_errors=errors)
        return result
    finally:
        tracker.close()
