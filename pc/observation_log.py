"""Bounded local diagnostics for the read-only coordinate-window observer.

No capture, game-input, network, settings or whole-directory export APIs here.
"""
from collections import Counter
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from threading import RLock
import uuid
import zipfile
from PIL import Image

APP_VERSION = '0.10.5-packet-inspect'


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class ObservationLog:
    def __init__(self, base_dir, max_events_bytes=1048576,
                 max_image_bytes=2097152, image_slots=12):
        self.session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        self.directory = Path(base_dir) / 'observation_logs' / self.session_id
        self.max_events_bytes = max_events_bytes
        self.max_image_bytes = max_image_bytes
        self.image_slots = image_slots
        self.lock = RLock()
        self.error = None
        self.event_counts = Counter()
        self.result_counts = Counter()
        self.images = {}
        self.started_at = utc_now()
        self.event('session_start', app_version=APP_VERSION, game_input_allowed=False)

    def event(self, kind, **fields):
        with self.lock:
            self.event_counts[kind] += 1
            if kind == 'completed':
                result=fields.get('result',{})
                self.result_counts['coordinates_confirmed' if result.get('coordinates') is not None else 'coordinates_unknown']+=1
                self.result_counts['pattern_confirmed' if result.get('element') else 'pattern_unknown']+=1
                if result.get('partial'):self.result_counts['time_budget_reached']+=1
            record = dict(fields, event=kind, session_id=self.session_id, utc=utc_now())
            try:
                data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
                if len(data) > self.max_events_bytes:
                    raise ValueError('진단 이벤트 크기 제한 초과')
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / 'events.jsonl'
                if path.exists() and path.stat().st_size + len(data) > self.max_events_bytes:
                    old = self.directory / 'events.2.jsonl'
                    old.unlink(missing_ok=True)
                    previous = self.directory / 'events.1.jsonl'
                    if previous.exists(): previous.replace(old)
                    path.replace(previous)
                with path.open('ab') as stream:
                    stream.write(data)
            except (OSError, ValueError, TypeError) as exc:
                self.error = str(exc)

    def frame(self, scan_id, rgb):
        # Preserve original pixels; do not turn a scaled diagnostic into OCR evidence.
        if rgb.shape[0] > 2048 or rgb.shape[1] > 2048:
            self.event('image_skipped', scan_id=scan_id, reason='dimension_limit')
            return None
        try:
            stream = io.BytesIO()
            Image.fromarray(rgb).save(stream, format='PNG')
            data = stream.getvalue()
            if len(data) > self.max_image_bytes:
                self.event('image_skipped', scan_id=scan_id, reason='byte_limit')
                return None
            with self.lock:
                name = f'frames/frame-{int(scan_id) % self.image_slots:02d}.png'
                path = self.directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_bytes(data)
                temporary.replace(path)
                self.images[name] = dict(scan_id=int(scan_id), width=rgb.shape[1], height=rgb.shape[0])
                return name
        except (OSError, ValueError, TypeError) as exc:
            with self.lock: self.error = str(exc)
            return None

    def export(self, destination, include_images=True):
        destination = Path(destination)
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
        with self.lock:
            summary = dict(session_id=self.session_id, started_at=self.started_at,
                           exported_at=utc_now(), event_counts=dict(self.event_counts),
                           result_counts=dict(self.result_counts),
                           logging_error=self.error, game_input_allowed=False,
                           success_confirmed=False)
            images=dict(self.images) if include_images else {}
            manifest = dict(schema_version=1, app_version=APP_VERSION,
                            session_id=self.session_id, images=images,
                            image_scope='coordinate window or manually selected image only',
                            image_notice='Coordinate-window labels may include player names.',
                            limits=dict(event_files=3, bytes_per_event_file=self.max_events_bytes,
                                        image_files=self.image_slots, bytes_per_image=self.max_image_bytes),
                            cutoff='Snapshot at export; pending OCR may complete after this ZIP.')
            try:
                with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for name, value in (('manifest.json', manifest), ('summary.json', summary)):
                        archive.writestr(name, json.dumps(value, ensure_ascii=False, indent=2))
                    # Fixed allowlist: never glob the EXE directory, settings or arbitrary files.
                    for name in ('events.2.jsonl', 'events.1.jsonl', 'events.jsonl', *images):
                        path = self.directory / name
                        if path.is_file(): archive.write(path, name)
                    archive.writestr('README.txt',
                        'Read-only spear observation diagnostics. No game input was sent.\n'
                        'Events distinguish completed, displayed, stale and generation_reset.\n'
                        'Frames are rolling slots: match each retained image to manifest scan_id.\n'
                        'No images means image saving was disabled or its limit was exceeded.\n'
                        'This ZIP is not proof of mechanic success or correct target ownership.\n')
                os.replace(temporary, destination)
                return summary
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                self.error = str(exc)
                temporary.unlink(missing_ok=True)
                raise
