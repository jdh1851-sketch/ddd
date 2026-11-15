"""Synthetic capture/replay tests. No user packet, name or address is bundled."""
import io
import json
from pathlib import Path
import socket
import struct
import sys
import zipfile

import brotli
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packet_decode import PacketInspector, analyze_archive

START = struct.pack('<IIB', 20132, 0, 0)
END = struct.pack('<IIB', 20316, 0, 0)
SKILLS = (1791041054, 324458883, 948487208, 1324307937, 597598050, 1369085966)
MARKER = bytes.fromhex('18271c28c3b6114d')


def frame(kind, payload=b'', encoding=0):
    return struct.pack('<IIB', kind, len(payload), encoding) + payload


def position(entity=101, x=2.5, y=160.25, heading=.4):
    return frame(170826, struct.pack('<Qfff', entity, x, y, heading))


def batch(*messages):
    return START + b''.join(messages) + END


def tcp_packet(payload, sequence=1000, *, server=True, flags=0x18,
               source='198.51.100.9', destination='192.0.2.10', padding=b''):
    sport, dport = (16000, 50123) if server else (50123, 16000)
    if not server: source, destination = destination, source
    tcp = struct.pack('!HHIIBBHHH', sport, dport, sequence % (2**32), 0,
                      5 << 4, flags, 65535, 0, 0) + payload
    ipv4 = struct.pack('!BBHHHBBH4s4s', 0x45, 0,
                       20 + len(tcp), 1, 0, 64, 6, 0,
                       socket.inet_aton(source), socket.inet_aton(destination)) + tcp
    return b'\0' * 12 + b'\x08\x00' + ipv4 + padding


def feed(inspector, payload, sequence=1000, timestamp=1., **options):
    inspector.feed(tcp_packet(payload, sequence, **options), 1, timestamp)
    return inspector.snapshot()


def assert_read_only(snapshot):
    for key in ('self_identity_confirmed', 'departure_signal_confirmed',
                'success_confirmed', 'game_input_allowed'):
        assert snapshot[key] is False
    assert snapshot['element'] is None


def pcap(records):
    out = io.BytesIO()
    out.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
    for timestamp, raw in records:
        seconds = int(timestamp)
        out.write(struct.pack('<IIII', seconds, int((timestamp-seconds)*1e6), len(raw), len(raw)))
        out.write(raw)
    return out.getvalue()


def archive(path, data, extras=None):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('metadata.json', json.dumps({'schema_version': 1, 'app_version': 'synthetic-only'}))
        z.writestr('adapter-00.pcap', data)
        for name, contents in (extras or {}).items(): z.writestr(name, contents)
    return path


def test_position_is_a_candidate_not_identity_or_mechanic_success():
    reader = PacketInspector()
    snapshot = feed(reader, batch(position()))
    assert snapshot['server_frames'] == 3
    assert len(snapshot['positions']) == 1
    point = snapshot['positions'][0]
    assert point['entity_id'] == 101
    assert point['x'] == pytest.approx(2.5) and point['y'] == pytest.approx(160.25)
    assert point['heading'] == pytest.approx(.4)
    assert_read_only(snapshot)


def test_split_and_reordered_tcp_waits_for_complete_batch_and_deduplicates():
    reader = PacketInspector(); payload = batch(position())
    assert not feed(reader, payload[:12])['positions']
    assert not feed(reader, payload[30:], sequence=1030, timestamp=1.1)['positions']
    result = feed(reader, payload[12:30], sequence=1012, timestamp=1.2)
    assert len(result['positions']) == 1 and result['server_frames'] == 3
    retransmission = feed(reader, payload, timestamp=1.3)
    assert retransmission['server_frames'] == result['server_frames']
    assert len(retransmission['positions']) == 1


def test_tcp_sequence_wrap_keeps_one_complete_record():
    reader = PacketInspector(); payload = batch(position())
    feed(reader, payload[:20], sequence=0xfffffff0)
    result = feed(reader, payload[20:], sequence=4, timestamp=1.1)
    assert result['server_frames'] == 3
    assert result['positions'][0]['entity_id'] == 101


def test_conflicting_overlap_revokes_old_position_instead_of_promoting_tail():
    reader = PacketInspector(); previous = batch(position()); pending = batch(position(x=9))
    assert feed(reader, previous)['positions']
    sequence = 1000 + len(previous)
    feed(reader, pending[:30], sequence=sequence, timestamp=1.1)
    bad = bytearray(pending[12:25]); bad[3] ^= 0xff
    feed(reader, bytes(bad), sequence=sequence+12, timestamp=1.2)
    result = feed(reader, pending[30:], sequence=sequence+30, timestamp=1.3)
    assert not result['positions']
    assert result['sync_state'] != 'synced'
    assert_read_only(result)


def test_missing_bytes_and_stale_time_cannot_reuse_previous_coordinates():
    reader = PacketInspector(); previous = batch(position()); pending = batch(position(x=9))
    assert feed(reader, previous)['positions']
    # The missing first ten bytes never arrive. Do not concatenate across that gap.
    feed(reader, pending[10:], sequence=1000+len(previous)+10, timestamp=1.1)
    result = feed(reader, pending[10:], sequence=1000+len(previous)+10, timestamp=5.2)
    assert not result['positions']
    assert_read_only(result)


def test_reassembly_does_not_mix_devices_or_connections():
    reader = PacketInspector(); payload = batch(position())
    reader.feed(tcp_packet(payload[:20]), 1, 1., source_id='device-a')
    reader.feed(tcp_packet(payload[20:], sequence=1020), 1, 1.1, source_id='device-b')
    assert not reader.snapshot()['positions']
    # Another remote endpoint cannot fill the first connection's missing bytes either.
    reader.feed(tcp_packet(payload[20:], sequence=1020, source='203.0.113.7'), 1, 1.2, source_id='device-a')
    assert not reader.snapshot()['positions']


def test_brotli_and_opaque_payloads_preserve_batch_boundaries():
    reader = PacketInspector()
    raw = struct.pack('<Qfff', 102, 3., 161., .2)
    payload = batch(frame(170826, brotli.compress(raw), 1),
                    frame(20932, b'not-a-brotli-stream', 1),
                    frame(222222, b'uninterpreted', 7))
    result = feed(reader, payload)
    assert result['positions'][0]['entity_id'] == 102
    assert result['decode_errors'] >= 1
    assert result['opaque'] >= 1
    assert_read_only(result)


def test_oversized_brotli_output_is_not_a_position_or_unbounded_result():
    reader = PacketInspector()
    payload = batch(frame(170826, brotli.compress(b'x' * (256*1024+1)), 1))
    result = feed(reader, payload)
    assert not result['positions']
    assert result['decode_errors'] >= 1
    assert_read_only(result)


def test_incomplete_invalid_length_cannot_expose_position_before_batch_validation():
    reader = PacketInspector()
    payload = START + position() + struct.pack('<IIB', 1234, 0x7fffffff, 0) + END
    result = feed(reader, payload)
    assert not result['positions']
    assert result['sync_state'] != 'synced'
    assert_read_only(result)


def boss_payload():
    raw = bytearray(1636)
    struct.pack_into('<Q', raw, 0, 301)
    struct.pack_into('<I', raw, 626, 9)
    struct.pack_into('<9I', raw, 630, 123, 456, *SKILLS, 789)
    raw[1100:1108] = MARKER
    struct.pack_into('<fff', raw, 1108, 0., 163., 3.1415927)
    return bytes(raw)


def test_exact_boss_profile_stays_candidate_and_malformed_profiles_fail():
    result = feed(PacketInspector(), batch(frame(20051, boss_payload())))
    assert len(result['boss_candidates']) == 1
    boss = result['boss_candidates'][0]
    assert boss['entity_id'] == 301 and boss['x'] == 0 and boss['y'] == 163
    assert_read_only(result)
    for bad in ('skill', 'marker', 'length'):
        altered = bytearray(boss_payload())
        if bad == 'skill': altered[638] ^= 1
        elif bad == 'marker': altered[1100] ^= 1
        else: altered.pop()
        rejected = feed(PacketInspector(), batch(frame(20051, bytes(altered))))
        assert not rejected['boss_candidates'], bad
        assert_read_only(rejected)


def test_ethernet_padding_does_not_become_application_bytes():
    reader = PacketInspector(); payload = batch(position())
    result = feed(reader, payload, padding=batch(position(entity=999)))
    assert result['server_frames'] == 3
    assert [r['entity_id'] for r in result['positions']] == [101]


def test_client_has_separate_header_and_never_creates_position_candidates():
    reader = PacketInspector()
    client = (struct.pack('<II5s', 190006, 0, bytes.fromhex('d41d8cd900')) +
              struct.pack('<II5s', 20624, 20, b'\x01\x02\x03\x04\0') + struct.pack('<Qfff', 101, 2., 3., 0.))
    result = feed(reader, client, server=False)
    assert result['client_frames'] == 2
    assert not result['positions']
    assert_read_only(result)


def test_zip_replay_matches_direct_feed_without_writing_members(tmp_path):
    raw = tcp_packet(batch(position()))
    path = archive(tmp_path/'capture.zip', pcap([(1., raw)]))
    result = analyze_archive(path)
    assert result['server_frames'] == 3
    assert result['positions'][0]['entity_id'] == 101
    assert set(tmp_path.iterdir()) == {path}
    assert_read_only(result)


def test_zip_traversal_member_cannot_be_used_as_capture_or_extracted(tmp_path):
    dangerous = pcap([(1., tcp_packet(batch(position(entity=999))))])
    path = archive(tmp_path/'capture.zip', pcap([]), {'../adapter-99.pcap': dangerous})
    try:
        result = analyze_archive(path)
    except ValueError:
        result = None
    if result is not None: assert not result['positions']
    assert set(tmp_path.iterdir()) == {path}
    assert not (tmp_path.parent/'adapter-99.pcap').exists()


def test_zip_rejects_truncated_pcap_records(tmp_path):
    raw = pcap([(1., tcp_packet(batch(position())))])
    path = archive(tmp_path/'capture.zip', raw[:-1])
    with pytest.raises(ValueError): analyze_archive(path)


@pytest.mark.parametrize('x,y,heading', [(float('nan'), 160., 0.), (1., float('inf'), 0.), (1., 160., float('-inf'))])
def test_nonfinite_position_fields_are_never_displayed(x, y, heading):
    result = feed(PacketInspector(), batch(position(x=x, y=y, heading=heading)))
    assert not result['positions']
    assert_read_only(result)


def test_wrong_offsets_do_not_create_a_boss_candidate():
    payload = bytearray(boss_payload())
    payload[638:662] = b'\0' * 24
    payload[642:666] = struct.pack('<6I', *SKILLS)
    result = feed(PacketInspector(), batch(frame(20051, bytes(payload))))
    assert not result['boss_candidates']


def test_position_expires_even_when_only_new_control_frames_arrive():
    reader = PacketInspector(); payload = batch(position())
    assert feed(reader, payload)['positions']
    result = feed(reader, batch(), sequence=1000+len(payload), timestamp=4.2)
    assert not result['positions']
    assert_read_only(result)


def test_zip_declared_inflated_size_is_bounded_before_replay(tmp_path):
    path = tmp_path/'oversized.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('metadata.json', '{}')
        with z.open('adapter-00.pcap', 'w') as out:
            for _ in range(33): out.write(b'\0' * 1024 * 1024)
    with pytest.raises(ValueError, match='(?i)size|limit|large|크기|제한|초과|32'):
        analyze_archive(path)


def test_marker_like_bytes_inside_an_opaque_body_are_not_nested_messages():
    reader = PacketInspector()
    nested = batch(position(entity=999))
    result = feed(reader, batch(position(entity=101), frame(222222, nested)))
    assert [row['entity_id'] for row in result['positions']] == [101]
    assert result['server_frames'] == 4


def test_client_body_cannot_be_reinterpreted_as_server_position_frames():
    reader = PacketInspector(); body = batch(position(entity=999))
    client = (struct.pack('<II5s', 190006, 0, bytes.fromhex('d41d8cd900')) +
              struct.pack('<II5s', 20624, len(body), b'\0' * 5) + body)
    result = feed(reader, client, server=False)
    assert result['client_frames'] == 2
    assert not result['positions']
    assert_read_only(result)


def test_tcp_reset_revokes_position_without_waiting_for_another_payload():
    reader = PacketInspector(); payload = batch(position())
    assert feed(reader, payload)['positions']
    result = feed(reader, b'', sequence=1000+len(payload), timestamp=1.1, flags=0x14)
    assert not result['positions']
    assert result['sync_state'] != 'synced'
    assert_read_only(result)


def test_stream_flood_remains_bounded_and_cannot_mix_actor_state():
    reader = PacketInspector()
    for index in range(40):
        raw = tcp_packet(batch(position(entity=index+1)), source=f'203.0.113.{index+1}')
        reader.feed(raw, 1, 1.+index*.001)
    snapshot = reader.snapshot()
    assert snapshot['stream_count'] <= 32
    assert len(snapshot['positions']) <= 32
    assert_read_only(snapshot)


def test_standalone_envelopes_after_a_verified_batch_remain_diagnostic_evidence():
    reader = PacketInspector()
    payload = (batch(position()) + frame(20932, b'not-a-brotli-stream', 1) +
               frame(210011) + batch(position(x=3.)))
    result = feed(reader, payload)
    assert result['server_frames'] == 8
    assert result['decode_errors'] == 1
    assert result['positions'][0]['x'] == 3.
    assert_read_only(result)


def test_idle_client_stream_does_not_discard_complete_frames_at_trusted_boundary():
    reader = PacketInspector()
    first = struct.pack('<II5s', 190006, 0, bytes.fromhex('d41d8cd900'))
    result = feed(reader, first, server=False)
    assert result['client_frames'] == 1
    second = struct.pack('<II5s', 20126, 3, b'\0' * 5) + b'abc'
    result = feed(reader, second, sequence=1000+len(first), timestamp=5., server=False)
    assert result['client_frames'] == 2
    assert not result['positions']
    assert_read_only(result)


def test_initial_tail_before_prefix_can_synchronize_once_missing_prefix_arrives():
    reader = PacketInspector(); payload = batch(position())
    assert not feed(reader, payload[18:], sequence=1018)['positions']
    result = feed(reader, payload[:18], sequence=1000, timestamp=1.1)
    assert result['server_frames'] == 3
    assert result['positions'][0]['entity_id'] == 101
    assert_read_only(result)
