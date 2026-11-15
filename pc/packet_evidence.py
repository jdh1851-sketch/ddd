"""Bounded, timestamped evidence from an existing coordinate window.

This records pixels and capture facts only. It neither interprets the game nor
changes Mobilay, network settings or input state. Windows capture is loaded
lazily so tests and archive replay do not require a desktop or Npcap.
"""
from collections import deque
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
import time

import numpy as np
from PIL import Image, ImageGrab


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='milliseconds')


def capture_coordinate_window():
    """Return only the unobscured client pixels of one existing Mobilay window."""
    if os.name != 'nt':
        raise ValueError('Windows 좌표 창 캡처를 사용할 수 없습니다.')
    import ctypes as C
    from ctypes import wintypes as W

    user = C.WinDLL('user32', use_last_error=True)
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = [callback_type, W.LPARAM]
    user.EnumWindows.restype = W.BOOL
    user.GetWindowTextLengthW.argtypes = [W.HWND]
    user.GetWindowTextLengthW.restype = C.c_int
    user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    user.GetWindowTextW.restype = C.c_int
    for name in ('IsWindowVisible', 'IsWindow', 'IsIconic'):
        getattr(user, name).argtypes = [W.HWND]
        getattr(user, name).restype = W.BOOL
    user.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
    user.GetClientRect.restype = W.BOOL
    user.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
    user.ClientToScreen.restype = W.BOOL
    user.WindowFromPoint.argtypes = [W.POINT]
    user.WindowFromPoint.restype = W.HWND
    user.GetAncestor.argtypes = [W.HWND, W.UINT]
    user.GetAncestor.restype = W.HWND

    found = []

    @callback_type
    def callback(handle, _):
        if not user.IsWindowVisible(handle):
            return True
        title = C.create_unicode_buffer(user.GetWindowTextLengthW(handle) + 1)
        user.GetWindowTextW(handle, title, len(title))
        if title.value.strip() == '좌표 미니맵':
            found.append(handle)
        return True

    if not user.EnumWindows(callback, 0):
        raise ValueError('좌표 창 목록을 읽지 못했습니다.')
    if not found:
        raise ValueError('좌표 미니맵 창이 없습니다.')
    if len(found) != 1:
        raise ValueError('좌표 미니맵 창이 여러 개입니다.')
    handle = found[0]

    def checked_rect():
        if not user.IsWindow(handle) or user.IsIconic(handle):
            raise ValueError('좌표 창이 닫히거나 최소화됐습니다.')
        rect = W.RECT()
        point = W.POINT()
        if not user.GetClientRect(handle, C.byref(rect)) or not user.ClientToScreen(handle, C.byref(point)):
            raise ValueError('좌표 창 영역을 확인하지 못했습니다.')
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 100 or height < 100 or width > 2048 or height > 2048:
            raise ValueError('좌표 창 크기가 캡처 범위를 벗어났습니다.')
        for fx, fy in ((.15, .15), (.5, .5), (.85, .85), (.15, .85), (.85, .15)):
            owner = user.GetAncestor(user.WindowFromPoint(W.POINT(point.x + int(width * fx), point.y + int(height * fy))), 2)
            if owner != handle:
                raise ValueError('다른 창이 좌표 창을 가리고 있습니다.')
        return point.x, point.y, width, height

    x, y, width, height = checked_rect()
    # Explicit bbox is mandatory; there is no fallback to whole-desktop capture.
    rgb = np.array(ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True).convert('RGB'))
    if checked_rect() != (x, y, width, height):
        raise ValueError('캡처 중 좌표 창 위치가 바뀌었습니다.')
    return rgb, dict(source='existing_coordinate_window', window_title='좌표 미니맵',
                     client_rect=[x, y, width, height])


class EvidenceRecorder:
    def __init__(self, base_dir, source_provider=None, *, interval=1., image_slots=30,
                 max_image_bytes=2 * 1024 * 1024, max_total_image_bytes=8 * 1024 * 1024,
                 max_events=180, max_event_bytes=65536, on_frame=None):
        self.base_dir = Path(base_dir)
        self.source_provider = source_provider or capture_coordinate_window
        self.on_frame=on_frame
        self.interval = max(.01, float(interval))
        self.image_slots = max(1, min(int(image_slots), 30))
        self.max_image_bytes = max(1, min(int(max_image_bytes), 2 * 1024 * 1024))
        self.max_total_image_bytes = max(1, min(int(max_total_image_bytes), 8 * 1024 * 1024))
        self.max_events = max(1, min(int(max_events), 180))
        self.max_event_bytes = max(256, min(int(max_event_bytes), 65536))
        self.lock = RLock()
        self.thread = None
        self.stop_event = Event()
        self.directory = None
        self.images = {}
        self.events = deque()
        self.state = self._initial_state()

    @staticmethod
    def _initial_state():
        return dict(running=False, captured=0, skipped=0, attempts=0, error=None,
                    session_dir=None, window_state='not_started', last_capture_epoch=None,
                    last_skip_reason=None, game_input_allowed=False)

    def start(self, session_dir):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('좌표 창 증거 기록이 아직 진행 중입니다.')
            self.directory = Path(session_dir) / 'evidence'
            if self.directory.exists() and any(self.directory.iterdir()):
                raise ValueError('이 세션에 좌표 창 증거가 이미 있습니다.')
            self.images = {}
            self.events.clear()
            self.stop_event = Event()
            self.state = self._initial_state()
            self.state.update(running=True, session_dir=str(session_dir),
                              window_state='starting', started_epoch=time.time())
            self.thread = Thread(target=self._run, daemon=True, name='coordinate-evidence')
            self.thread.start()

    def stop(self):
        self.stop_event.set()

    def close(self):
        self.stop()
        if self.thread and self.thread is not current_thread():
            self.thread.join(timeout=2)

    def status(self):
        with self.lock:
            result = dict(self.state)
            result['running'] = bool(self.thread and self.thread.is_alive()) or result['running']
            result['image_files'] = len(self.images)
            result['image_bytes'] = sum(image['size_bytes'] for image in self.images.values())
            result['events_retained'] = len(self.events)
            return result

    def _fail(self, exc):
        with self.lock:
            self.state['error'] = str(exc)[:500]

    def _event(self, kind, **fields):
        epoch = time.time()
        record = dict(fields, event=kind, epoch=epoch, utc=_utc(epoch))
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        with self.lock:
            self.events.append(encoded)
            while len(self.events) > self.max_events or sum(map(len, self.events)) > self.max_event_bytes:
                self.events.popleft()
            data = b''.join(self.events)
        target = self.directory / 'events.jsonl'
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(target)

    @staticmethod
    def _source_metadata(metadata):
        if not isinstance(metadata, dict):
            return {}
        value = {}
        for key in ('source', 'window_title'):
            if isinstance(metadata.get(key), str):
                value[key] = metadata[key][:100]
        rect = metadata.get('client_rect')
        if isinstance(rect, (list, tuple)) and len(rect) == 4 and all(isinstance(n, int) for n in rect):
            value['client_rect'] = list(rect)
        return value

    def _skip(self, frame_id, reason, started, finished):
        with self.lock:
            self.state['skipped'] += 1
            self.state.update(window_state='unavailable', last_skip_reason=reason[:300])
        self._event('skipped', frame_id=frame_id, reason=reason[:300],
                    capture_started_epoch=started, capture_finished_epoch=finished)

    def _capture_once(self):
        started = time.time()
        with self.lock:
            self.state['attempts'] += 1
            frame_id = self.state['attempts']
        try:
            rgb, source = self.source_provider()
        except Exception as exc:
            self._skip(frame_id, str(exc) or type(exc).__name__, started, time.time())
            return
        finished = time.time()
        if self.stop_event.is_set():
            self._skip(frame_id, 'stopped_during_capture', started, finished)
            return
        if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
            self._skip(frame_id, 'invalid_rgb_source', started, finished)
            return
        height, width = rgb.shape[:2]
        if not (1 <= height <= 2048 and 1 <= width <= 2048):
            self._skip(frame_id, 'dimension_limit', started, finished)
            return
        stream = io.BytesIO()
        Image.fromarray(rgb).save(stream, format='PNG')
        data = stream.getvalue()
        if len(data) > self.max_image_bytes:
            self._skip(frame_id, 'image_byte_limit', started, finished)
            return
        name = f'frames/frame-{frame_id % self.image_slots:02d}.png'
        with self.lock:
            previous = self.images.get(name, {}).get('size_bytes', 0)
            total = sum(image['size_bytes'] for image in self.images.values())
        if total - previous + len(data) > self.max_total_image_bytes:
            self._skip(frame_id, 'total_image_byte_limit', started, finished)
            return
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
        evidence = dict(frame_id=frame_id, width=width, height=height, size_bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(), capture_started_epoch=started,
                        capture_finished_epoch=finished, captured_epoch=(started + finished) / 2,
                        timestamp_basis='midpoint_of_grab_call_bounds',
                        source=self._source_metadata(source))
        with self.lock:
            self.images[name] = evidence
            self.state['captured'] += 1
            self.state.update(window_state='available', last_skip_reason=None,
                              last_capture_epoch=evidence['captured_epoch'])
        self._event('captured', file=name, **evidence)
        self._write_manifest()
        if self.on_frame:
            try:
                self.on_frame(rgb,evidence['captured_epoch'],frame_id,
                    capture_started_epoch=started,capture_finished_epoch=finished)
            except Exception as exc:
                self._event('analysis_callback_error',message=str(exc)[:300])

    def _write_manifest(self):
        with self.lock:
            manifest = dict(schema_version=1, session_id=Path(self.state['session_dir']).name,
                            status=dict(self.state), images=dict(self.images),
                            scope='existing_coordinate_window_only',
                            image_notice='Coordinate-window labels may contain player names.',
                            interpretation='pixels and capture timing only; no game state inferred',
                            limits=dict(image_slots=self.image_slots, bytes_per_image=self.max_image_bytes,
                                        total_image_bytes=self.max_total_image_bytes, max_dimension=2048,
                                        event_records=self.max_events, event_bytes=self.max_event_bytes))
        target = self.directory / 'manifest.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)

    def _run(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._event('session_started', session_id=Path(self.state['session_dir']).name)
            self._write_manifest()
            while not self.stop_event.is_set():
                before = time.monotonic()
                self._capture_once()
                self.stop_event.wait(max(0, self.interval - (time.monotonic() - before)))
        except Exception as exc:
            self._fail(exc)
        finally:
            with self.lock:
                self.state.update(running=False, finished_epoch=time.time())
            try:
                self._event('session_stopped', captured=self.state['captured'], skipped=self.state['skipped'])
                self._write_manifest()
            except Exception as exc:
                self._fail(exc)

    def export_members(self):
        if self.thread and self.thread.is_alive():
            raise RuntimeError('좌표 창 증거 기록을 종료한 뒤 내보내세요.')
        if self.directory is None:
            return []
        try:
            self._write_manifest()
        except OSError as exc:
            self._fail(exc)
        names = ['manifest.json', 'events.jsonl']
        with self.lock:
            names.extend(self.images)
        # No glob: neither neighbouring packet files nor arbitrary user files are exported.
        return [(self.directory / name, 'evidence/' + name)
                for name in names if (self.directory / name).is_file()]
