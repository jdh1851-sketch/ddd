"""Bounded observation of manual movement controls, never game input.

Only the verified game's foreground window is sampled. Cursor displacement is
evidence of pointer motion, not a camera angle or a calibrated movement vector.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
import time


class _WindowsState:
    """Load the small read-only Windows API surface only when recording starts."""
    def __init__(self, game_pid):
        if os.name != 'nt':
            raise RuntimeError('windows_required')
        import ctypes as C
        from ctypes import wintypes as W
        self.C, self.W, self.game_pid = C, W, game_pid
        self.user = C.WinDLL('user32', use_last_error=True)
        self.user.GetForegroundWindow.argtypes = []
        self.user.GetForegroundWindow.restype = W.HWND
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.GetWindowThreadProcessId.restype = W.DWORD
        self.user.GetAsyncKeyState.argtypes = [C.c_int]
        self.user.GetAsyncKeyState.restype = C.c_short
        self.user.GetCursorPos.argtypes = [C.POINTER(W.POINT)]
        self.user.GetCursorPos.restype = W.BOOL

    def _pid(self):
        handle = self.user.GetForegroundWindow()
        pid = self.W.DWORD()
        if handle:
            self.user.GetWindowThreadProcessId(handle, self.C.byref(pid))
        return pid.value

    def __call__(self):
        inactive = {'foreground_pid': 0}
        if self._pid() != self.game_pid:
            return inactive
        keys, buttons = set(), set()
        # Check focus before each read and again after the snapshot. No text,
        # window title or other key state is read, even for discarded samples.
        for label, code, result in (
                ('W', 0x57, keys), ('A', 0x41, keys),
                ('S', 0x53, keys), ('D', 0x44, keys),
                ('left', 0x01, buttons), ('right', 0x02, buttons)):
            if self._pid() != self.game_pid:
                return inactive
            if self.user.GetAsyncKeyState(code) & 0x8000:
                result.add(label)
        if self._pid() != self.game_pid:
            return inactive
        point = self.W.POINT()
        if not self.user.GetCursorPos(self.C.byref(point)):
            raise RuntimeError('cursor_unavailable')
        if self._pid() != self.game_pid:
            return inactive
        return dict(foreground_pid=self.game_pid, keys=keys, buttons=buttons,
                    cursor=(point.x, point.y))


class MovementRecorder:
    INTERVAL = .05
    MAX_SECONDS = 90.
    MAX_ROWS = 2000
    MAX_BYTES = 256 * 1024
    FLUSH_ROWS = 5

    def __init__(self, base_dir, state_provider=None):
        self.base_dir = Path(base_dir)
        self.state_provider = state_provider
        self.lock = RLock()
        self.thread = None
        self.stop_event = Event()
        self.directory = None
        self._complete = False
        self._previous_cursor = None
        self.state = self._initial_state()

    @staticmethod
    def _initial_state():
        return dict(running=False, rows=0, active_rows=0, inactive_rows=0,
                    bytes_written=0, error=None, stop_reason=None,
                    game_pid=None, session_dir=None, input_sent=False,
                    calibration_verified=False)

    def start(self, session_dir, game_pid):
        if type(game_pid) is not int or game_pid <= 0:
            raise ValueError('A verified positive game PID is required.')
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('이동 자료 기록이 아직 진행 중입니다.')
            directory = Path(session_dir) / 'movement'
            # Even an empty existing directory is not ours to reuse.
            if directory.exists() or directory.is_symlink():
                raise ValueError('이 세션에 이동 자료 폴더가 이미 있습니다.')
            directory.mkdir(parents=True, exist_ok=False)
            self.directory = directory
            self.stop_event = Event()
            self._complete = False
            self._previous_cursor = None
            self.state = self._initial_state()
            self.state.update(running=True, game_pid=game_pid,
                              session_dir=str(session_dir),
                              started_epoch=time.time(),
                              started_monotonic=time.monotonic())
            try:
                (directory / 'samples.jsonl').touch(exist_ok=False)
                self._write_manifest()
            except Exception as exc:
                self.state.update(running=False, error='storage_error:' + type(exc).__name__)
                raise
            self.thread = Thread(target=self._run, daemon=True, name='movement-evidence')
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        worker = self.thread
        if worker and worker is not current_thread():
            worker.join(timeout=1.)

    def close(self):
        self.stop()

    def status(self):
        with self.lock:
            snapshot = dict(self.state)
            snapshot['running'] = bool(self.thread and self.thread.is_alive()) or snapshot['running']
            return snapshot

    @staticmethod
    def _pressed(values, name):
        if isinstance(values, dict):
            return values.get(name) is True
        return isinstance(values, (set, frozenset, list, tuple)) and name in values

    def _row(self, source):
        epoch, monotonic = time.time(), time.monotonic()
        row = dict(epoch=epoch,
                   utc=datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='milliseconds'),
                   monotonic=monotonic, active=False)
        if not isinstance(source, dict) or type(source.get('foreground_pid')) is not int:
            self._previous_cursor = None
            return row
        if source['foreground_pid'] != self.state['game_pid']:
            self._previous_cursor = None
            return row
        keys = source.get('keys')
        buttons = source.get('buttons')
        point = source.get('cursor')
        cursor = None
        if (isinstance(point, (tuple, list)) and len(point) == 2
                and all(type(n) is int and -(2 ** 31) <= n < 2 ** 31 for n in point)):
            cursor = list(point)
        delta = None
        if cursor is not None and self._previous_cursor is not None:
            delta = [cursor[0] - self._previous_cursor[0], cursor[1] - self._previous_cursor[1]]
        self._previous_cursor = cursor
        row.update(active=True, keys={key: self._pressed(keys, key) for key in ('W', 'A', 'S', 'D')},
                   buttons={key: self._pressed(buttons, key) for key in ('left', 'right')},
                   cursor=cursor, cursor_delta=delta,
                   camera_possible_change=bool(delta and any(delta)))
        return row

    def _write_manifest(self):
        with self.lock:
            manifest = dict(schema_version=1, game_pid=self.state['game_pid'],
                            status=dict(self.state), input_sent=False,
                            calibration_verified=False,
                            scope='verified_game_foreground_manual_controls_only',
                            active_means='foreground PID exactly equals game_pid; no other PID is stored',
                            interpretation='cursor_delta is pointer motion only; camera angle and movement calibration are unverified',
                            limits=dict(interval_seconds=self.INTERVAL, seconds=self.MAX_SECONDS,
                                        rows=self.MAX_ROWS, jsonl_bytes=self.MAX_BYTES),
                            files=['samples.jsonl', 'manifest.json'])
        (self.directory / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

    def _run(self):
        reason = 'stopped'
        try:
            provider = self.state_provider or _WindowsState(self.state['game_pid'])
            with (self.directory / 'samples.jsonl').open('ab') as stream:
                while not self.stop_event.is_set():
                    if time.monotonic() - self.state['started_monotonic'] >= self.MAX_SECONDS:
                        reason = 'duration_limit'
                        break
                    if self.state['rows'] >= self.MAX_ROWS:
                        reason = 'row_limit'
                        break
                    source = provider()
                    if self.stop_event.is_set():
                        break
                    if time.monotonic() - self.state['started_monotonic'] >= self.MAX_SECONDS:
                        reason = 'duration_limit'
                        break
                    row = self._row(source)
                    data = (json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
                    if self.state['bytes_written'] + len(data) > self.MAX_BYTES:
                        reason = 'byte_limit'
                        break
                    stream.write(data)
                    with self.lock:
                        self.state['rows'] += 1
                        self.state['active_rows' if row['active'] else 'inactive_rows'] += 1
                        self.state['bytes_written'] += len(data)
                    if self.state['rows'] % self.FLUSH_ROWS == 0:
                        stream.flush()
                    self.stop_event.wait(self.INTERVAL)
                stream.flush()
        except Exception as exc:
            # Provider errors may contain unrelated app text. Retain the type,
            # never the arbitrary exception message or provider payload.
            with self.lock:
                self.state['error'] = 'recording_error:' + type(exc).__name__
            reason = 'error'
        finally:
            self._previous_cursor = None
            with self.lock:
                self.state.update(running=False, stop_reason=reason,
                                  finished_epoch=time.time(),
                                  finished_monotonic=time.monotonic())
            try:
                self._write_manifest()
            except Exception as exc:
                with self.lock:
                    self.state['error'] = 'manifest_error:' + type(exc).__name__
            with self.lock:
                self._complete = True

    def export_members(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('이동 자료 기록을 중지한 뒤 내보내세요.')
            if not self.directory or not self._complete:
                return []
            directory = self.directory
        if directory.is_symlink():
            raise ValueError('이동 자료 폴더가 바뀌었습니다.')
        members = []
        for name in ('manifest.json', 'samples.jsonl'):
            path = directory / name
            if path.is_symlink():
                raise ValueError('이동 자료 파일이 바뀌었습니다.')
            if path.is_file():
                members.append((path, 'movement/' + name))
        return members
