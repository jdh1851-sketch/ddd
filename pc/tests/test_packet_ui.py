"""Headless checks for diagnostic display and stop/cancel behavior."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import json
import socket
import struct
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import packet_ui
from packet_ui import PacketPanel
from scan_async import LatestScan


class Value:
    def __init__(self, value=''): self.value = value
    def set(self, value): self.value = value
    def get(self): return self.value


class Tree:
    def __init__(self): self.rows = {}
    def get_children(self): return tuple(self.rows)
    def delete(self, *items):
        for item in items: self.rows.pop(item, None)
    def insert(self, parent, index, values):
        key = f'row-{len(self.rows)}'
        self.rows[key] = values
        return key


class Root:
    def __init__(self): self.callbacks = []
    def after(self, delay, callback): self.callbacks.append((delay, callback))


def result(*rows):
    return dict(sync_state='synced', server_frames=12, positions=list(rows),
                boss_candidates=[dict(entity_id=900, role_confirmed=False)],
                self_identity_confirmed=False, element=None,
                departure_signal_confirmed=False, success_confirmed=False,
                game_input_allowed=False)


def position(entity_id, timestamp):
    return dict(entity_id=entity_id, timestamp=timestamp, x=1.25, y=140.5, heading=.5)


def panel():
    view = PacketPanel.__new__(PacketPanel)
    view.analysis = Value(); view.report = Value(); view.detail = Value(); view.saved = Value()
    view.positions = Tree(); view.replay_result = None; view.last_decoded = None
    view.host = SimpleNamespace(root=Root())
    view.evidence = None
    return view


def test_live_display_removes_expired_positions_without_promoting_identity(monkeypatch):
    monkeypatch.setattr(packet_ui.time, 'time', lambda: 1000.)
    view = panel()
    view.positions.rows['previous-frame'] = ('old target',)
    view.show_decoded(result(position(101, 997.1), position(202, 996.9)))
    values = list(view.positions.rows.values())
    assert len(values) == 1 and values[0][0] == 101
    assert values[0][-1] == '2.9초 전'
    assert '실시간 수신' in view.analysis.get()
    assert '내 캐릭터: 미확인' in view.analysis.get()
    assert '이탈 신호: 미확인' in view.analysis.get()
    assert '자동이동 차단' in view.analysis.get()
    assert view.last_decoded['game_input_allowed'] is False


def test_archive_positions_are_explicitly_historical_and_keep_unknown_mechanics(monkeypatch):
    monkeypatch.setattr(packet_ui.time, 'time', lambda: 1000.)
    view = panel(); replay = result(position(101, 1.))
    view.replay_result = replay
    view.show_decoded(replay, historical=True)
    assert len(view.positions.rows) == 1
    assert list(view.positions.rows.values())[0][-1] == '기록 시점'
    assert '기록 ZIP 재생 · 현재 게임 상태 아님' in view.analysis.get()
    assert '투창병 후보: 900' in view.analysis.get()
    assert '내 캐릭터: 미확인' in view.analysis.get()
    assert '이탈 신호: 미확인' in view.analysis.get()
    assert '자동이동 차단' in view.analysis.get()
    assert not replay['self_identity_confirmed'] and not replay['departure_signal_confirmed']
    assert not replay['success_confirmed'] and replay['element'] is None


def test_stop_cancels_pending_replay_and_requests_both_recorders_stop():
    view = panel(); release = Event(); entered = Event(); stopped = []
    view.replay = LatestScan()
    view.capture = SimpleNamespace(status=lambda: {'running': True}, stop=lambda: stopped.append('packet'))
    view.evidence = SimpleNamespace(stop=lambda: stopped.append('evidence'))
    def delayed_replay():
        entered.set(); release.wait(2); return result(position(123, time.time()))
    try:
        assert view.replay.submit(delayed_replay)
        assert entered.wait(1)
        generation = view.replay.generation
        view.replay_result = {'old': 'historical data'}
        started = time.monotonic(); view.stopped()
        assert time.monotonic() - started < .2
        assert stopped == ['packet', 'evidence']
        assert view.replay_result is None and view.replay.generation != generation
        assert view.host.root.callbacks  # UI refresh is scheduled, not blocking.
        release.set()
        deadline = time.monotonic() + 1
        while not view.replay.future[0].done() and time.monotonic() < deadline: time.sleep(.005)
        before = view.report.get()
        view.poll_replay(generation)
        assert view.report.get() == before and view.replay_result is None
        assert view.replay.poll() is None  # Late worker data is also rejected.
    finally:
        release.set(); view.replay.close()


def test_finished_replay_is_shown_without_starting_a_capture():
    view = panel(); decoded = result(position(321, 10.))
    view.replay = SimpleNamespace(generation=2, future=None, poll=lambda **kwargs: decoded)
    # There is intentionally no capture device on this panel; replay is offline.
    view.poll_replay(2)
    assert view.replay_result == decoded and view.last_decoded == decoded
    assert '재생 완료' in view.report.get() and '게임 입력 차단' in view.report.get()
    assert '이동 보정·다른 속성·이탈 제어는 검증 전' in view.detail.get()
    assert list(view.positions.rows.values())[0][0] == 321


def test_inspector_failure_is_visible_and_removes_previous_candidates(monkeypatch):
    monkeypatch.setattr(packet_ui.time, 'time', lambda: 1000.)
    view = panel()
    decoded = result(position(101, 999.))
    view.show_decoded(decoded)
    assert view.positions.rows and '투창병 후보: 900' in view.analysis.get()
    decoded['inspector_error'] = 'RuntimeError: synthetic decoder stopped'
    view.show_decoded(decoded)
    assert not view.positions.rows
    assert '해석 중단: RuntimeError: synthetic decoder stopped' in view.analysis.get()
    assert '투창병 후보: 미확인' in view.analysis.get()
    assert '내 캐릭터: 미확인' in view.analysis.get()
    assert '이탈 신호: 미확인' in view.analysis.get()
    assert '자동이동 차단' in view.analysis.get()


def test_completed_capture_preserves_record_end_analysis_during_late_export(tmp_path, monkeypatch):
    from packet_capture import PacketCapture
    import packet_decode

    position_body = struct.pack('<Qfff', 777, 1.25, 140.5, .5)
    payload = (packet_decode.START + struct.pack('<IIB', 170826, len(position_body), 0)
               + position_body + packet_decode.END)
    tcp = struct.pack('!HHIIBBHHH', 16000, 50123, 1000, 0, 5 << 4, 0x18, 65535, 0, 0) + payload
    ipv4 = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0,
                       socket.inet_aton('198.51.100.9'), socket.inet_aton('192.0.2.10')) + tcp
    raw = b'\0' * 12 + b'\x08\x00' + ipv4
    connection = dict(local_address='192.0.2.10', local_port=50123,
                      remote_address='198.51.100.9', remote_port=16000)

    class CaptureAPI:
        def __init__(self): self.sent = False; self.closed = False
        def devices(self): return [dict(name='synthetic', description='test only', flags=2)]
        def open(self, name, bpf): return 1
        def datalink(self, handle): return 1
        def next_packet(self, handle):
            if self.sent: return None
            self.sent = True
            return int(time.time()), 100, raw, len(raw)
        def close(self, handle): self.closed = True

    api = CaptureAPI()
    capture = PacketCapture(tmp_path, api_factory=lambda: api,
                            connection_provider=lambda pid: [connection])
    try:
        capture.start(123, seconds=.15)
        capture.thread.join(3)
        assert not capture.status()['running'] and api.closed
        frozen = capture.decoded_snapshot()
        assert frozen['server_frames'] == 3 and frozen['positions'][0]['entity_id'] == 777
        # Advancing the clock genuinely expires the live inspector. A finished
        # record must still retain its own final timestamp for display/export.
        later = time.monotonic() + 60
        monkeypatch.setattr(packet_decode.time, 'monotonic', lambda: later)
        assert not capture.inspector.snapshot()['positions']
        assert capture.decoded_snapshot() == frozen
        frozen['positions'][0]['x'] = 999.  # Callers cannot mutate the retained record.
        assert capture.decoded_snapshot()['positions'][0]['x'] == 1.25
        destination = tmp_path / 'late-export.zip'
        capture.export(destination)
        with zipfile.ZipFile(destination) as archive:
            saved = json.loads(archive.read('decoded-summary.json'))
        assert saved['sync_state'] == 'synced'
        assert saved['positions'][0]['entity_id'] == 777 and saved['positions'][0]['x'] == 1.25
        assert saved['game_input_allowed'] is False and saved['self_identity_confirmed'] is False
    finally:
        capture.close()
