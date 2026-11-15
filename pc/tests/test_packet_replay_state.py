"""Replay identity is scope-bound evidence, never movement permission."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packet_capture import PacketCapture, write_pcap_header
from packet_replay import attach_identity, inspect_archive
import packet_ui
from packet_ui import PacketPanel


def identity(confirmed=True, stream='stream-1', generation=0, entity=101):
    return dict(confirmed=confirmed, self_entity_id=entity if confirmed else None,
                self_scope=dict(source_id='adapter-00.pcap', stream_id=stream,
                                generation=generation) if confirmed else None,
                match_count=12, game_input_allowed=False)


def observation(target=101, stream='stream-1', generation=0):
    return dict(sync_state='synced', positions=[], boss_candidates=[],
                self_identity_confirmed=False, departure_signal_confirmed=False,
                success_confirmed=False, game_input_allowed=False,
                spear=dict(stream_id=stream, generation=generation, cast_target_id=target,
                           stage='lock_candidate', element='불', departure_candidate=True,
                           departure_control_verified=False, success_observed=False,
                           game_input_allowed=False))


@pytest.mark.parametrize('target,expected', [(101, True), (202, False)])
def test_same_scope_identity_resolves_cast_target_without_promoting_control(target, expected):
    result = attach_identity(observation(target), identity())
    assert result['self_identity_confirmed'] is True
    assert result['spear']['self_identity_confirmed'] is True
    assert result['spear']['self_entity_id'] == 101
    assert result['spear']['target_is_self'] is expected
    assert result['spear']['departure_candidate'] is True
    assert result['spear']['departure_control_verified'] is False
    assert result['departure_signal_confirmed'] is False
    assert result['success_confirmed'] is False and result['spear']['success_observed'] is False
    assert result['game_input_allowed'] is False and result['spear']['game_input_allowed'] is False


@pytest.mark.parametrize('stream,generation', [('stream-2', 0), ('stream-1', 1)])
def test_cast_from_other_stream_or_generation_does_not_inherit_own_binding(stream, generation):
    result = attach_identity(observation(stream=stream, generation=generation), identity())
    # Historical identity is retained as evidence; authority over this cast is not.
    assert result['self_identity_confirmed'] is True
    assert result['spear']['self_identity_confirmed'] is False
    assert result['spear']['self_entity_id'] is None
    assert result['spear']['target_is_self'] is None
    assert result['spear']['departure_control_verified'] is False
    assert result['game_input_allowed'] is False


@pytest.mark.parametrize('bad_identity', [
    dict(confirmed=False, self_entity_id=None, self_scope=None),
    dict(confirmed=True, self_entity_id=101, self_scope=None),
    dict(confirmed=True, self_entity_id=101, self_scope={}),
    dict(confirmed=True, self_entity_id=None,
         self_scope=dict(stream_id='stream-1', generation=0)),
    dict(confirmed=True, self_entity_id=101,
         self_scope=dict(source_id='adapter-00.pcap', stream_id='stream-1', generation=True)),
    dict(confirmed=True, self_entity_id=101,
         self_scope=dict(source_id='adapter-00.pcap', stream_id='', generation=0)),
])
def test_missing_or_unconfirmed_identity_never_binds_a_cast(bad_identity):
    result = attach_identity(observation(), bad_identity)
    assert result['spear']['self_identity_confirmed'] is False
    assert result['spear']['target_is_self'] is None
    assert result['game_input_allowed'] is False


def test_missing_scopes_do_not_match_each_other_via_none_equality():
    result = attach_identity(dict(spear={}), dict(confirmed=True, self_entity_id=101))
    assert result['spear']['self_identity_confirmed'] is False
    assert result['spear']['self_entity_id'] is None
    assert result['spear']['target_is_self'] is None


class Value:
    def __init__(self): self.value = ''
    def set(self, value): self.value = value
    def get(self): return self.value


class Tree:
    def __init__(self): self.rows = []
    def get_children(self): return tuple(range(len(self.rows)))
    def delete(self, *items): self.rows.clear()
    def insert(self, parent, index, values): self.rows.append(values)


def panel():
    view = PacketPanel.__new__(PacketPanel)
    view.analysis = Value(); view.report = Value(); view.detail = Value()
    view.positions = Tree(); view.replay_result = {'historical': True}
    view.last_decoded = None
    return view


def test_historical_own_label_supports_missing_heading_and_failure_clears_authority():
    view = panel()
    result = attach_identity(observation(), identity())
    result['positions'] = [dict(entity_id=101, x=1.25, y=160.5,
                                heading=None, timestamp=1.)]
    view.show_decoded(result, historical=True)
    assert '기록 ZIP 재생 · 현재 게임 상태 아님' in view.analysis.get()
    assert '내 캐릭터: 101' in view.analysis.get()
    assert '후보 관측·제어 미검증' in view.analysis.get()
    assert '자동이동 차단' in view.analysis.get()
    assert view.positions.rows[0][3] == '미확인'
    assert view.positions.rows[0][-1] == '내 위치 · 기록 시점'
    failed = deepcopy(result)
    failed['inspector_error'] = 'synthetic decoder failure'
    view.show_decoded(failed, historical=True)
    assert not view.positions.rows
    assert '내 캐릭터: 미확인' in view.analysis.get()
    assert '투창 대상: 미확인' in view.analysis.get()
    assert '이탈 신호: 미확인' in view.analysis.get()
    assert '자동이동 차단' in view.analysis.get()


def test_offline_archive_never_trusts_saved_summary_identity_or_starts_live_capture(tmp_path):
    archive = tmp_path / 'synthetic.zip'
    pcap = io.BytesIO(); write_pcap_header(pcap, 1)
    # A prior JSON result is not replay authority; only raw supported PCAP and
    # independently verified synchronized images may establish identity.
    forged = attach_identity(observation(), identity())
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('adapter-00.pcap', pcap.getvalue())
        output.writestr('decoded-summary.json', json.dumps(forged))
        output.writestr('movement/samples.jsonl', json.dumps(dict(active=True, keys={'W': True})))
    result = inspect_archive(archive)
    assert result['replayed_packets'] == 0 and result['positions'] == []
    assert result['self_identity_confirmed'] is False
    assert result['identity']['confirmed'] is False
    assert result['spear']['target_is_self'] is None
    assert result['spear']['departure_control_verified'] is False
    assert result['game_input_allowed'] is False


def test_panel_stop_cancels_replay_and_stops_identity_and_both_recorders():
    view = panel(); calls = []
    view.replay = SimpleNamespace(reset=lambda: calls.append('replay'))
    view.capture = SimpleNamespace(status=lambda: dict(running=False), stop=lambda: calls.append('capture'))
    view.evidence = SimpleNamespace(stop=lambda: calls.append('evidence'))
    view.identity = SimpleNamespace(stop=lambda: calls.append('identity'))
    view.movement = SimpleNamespace(stop=lambda: calls.append('movement'))
    view.stopped()
    assert calls == ['replay', 'capture', 'evidence', 'identity', 'movement']
    assert view.replay_result is None


def test_new_recording_replaces_identity_callbacks_and_discards_replay_state(monkeypatch):
    view = panel(); closed = []; started = []
    view.evidence = SimpleNamespace(close=lambda: closed.append('old evidence'))
    view.identity = SimpleNamespace(close=lambda: closed.append('old identity'))
    view.movement = SimpleNamespace(close=lambda: closed.append('old movement'))
    view.replay = SimpleNamespace(reset=lambda: closed.append('old replay'))
    view.exporting = False
    view.saved = Value()
    view.save_evidence = SimpleNamespace(get=lambda: False)
    view.save_movement = SimpleNamespace(get=lambda: False)
    view.capture = SimpleNamespace(start=lambda **kwargs: started.append(kwargs))
    view.positions.rows.append(('old own identity',))
    fresh = SimpleNamespace(add_position=lambda row: None, reset_stream=lambda stream: None,
                            snapshot=lambda: dict(confirmed=False, self_entity_id=None))
    monkeypatch.setattr(packet_ui, 'IdentityTracker', lambda: fresh)

    def game_pid(handle, output):
        output._obj.value = 42

    view.host = SimpleNamespace(find_game=lambda: 99,
                                user32=SimpleNamespace(GetWindowThreadProcessId=game_pid))
    view.start()
    assert closed == ['old replay', 'old evidence', 'old identity', 'old movement']
    assert view.identity is fresh and view.evidence is None and view.movement is None
    assert view.replay_result is None and view.last_decoded is None and not view.positions.rows
    assert len(started) == 1 and started[0]['pid'] == 42
    assert started[0]['position_observer'] is fresh.add_position
    assert started[0]['reset_observer'] is fresh.reset_stream
    assert started[0]['identity_provider'] is fresh.snapshot


@pytest.mark.parametrize('kind', ['evidence', 'movement'])
def test_export_rejects_auxiliary_files_from_another_capture_session(tmp_path, kind):
    capture = PacketCapture(tmp_path)
    directory = tmp_path / 'current'; directory.mkdir()
    other = tmp_path / 'previous' / kind; other.mkdir(parents=True)
    source = other / 'manifest.json'; source.write_text('{}')
    capture.state.update(session_dir=str(directory), running=False)
    provider = SimpleNamespace(export_members=lambda: [(source, kind + '/manifest.json')])
    destination = tmp_path / 'result.zip'
    with pytest.raises(ValueError, match='현재 수신 기록 밖'):
        capture.export(destination, **{kind: provider})
    assert not destination.exists()
    assert not list(tmp_path.glob('*.tmp'))


def test_export_combines_only_current_session_auxiliary_members(tmp_path):
    capture = PacketCapture(tmp_path)
    directory = tmp_path / 'current'; directory.mkdir()
    capture.state.update(session_dir=str(directory), running=False)
    providers = {}
    for kind in ('evidence', 'movement'):
        folder = directory / kind; folder.mkdir()
        source = folder / 'manifest.json'; source.write_text(json.dumps(dict(synthetic=True)))
        (folder / 'private-settings.json').write_text('not in export list')
        providers[kind] = SimpleNamespace(export_members=lambda source=source, kind=kind:
                                         [(source, kind + '/manifest.json')])
    destination = tmp_path / 'result.zip'
    capture.export(destination, **providers)
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {'metadata.json', 'decoded-summary.json',
                                         'evidence/manifest.json', 'movement/manifest.json'}
