"""Experimental read-only inspection of captured game TCP messages.

The profile below was observed in one user capture. It supplies candidates,
never character ownership, a mechanic answer, a departure decision or input.
"""
from collections import Counter
import io
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
import struct
from threading import RLock
import time
import zipfile
from spear_packet_state import SpearPacketState

MAX_BUFFER = 2 * 1024 * 1024
MAX_DECODED = 256 * 1024
MAX_STREAMS = 32
MAX_TOTAL_DECODED = 32 * 1024 * 1024
STALE_SECONDS = 3.0
START = struct.pack('<IIB', 20132, 0, 0)
END = struct.pack('<IIB', 20316, 0, 0)
SPEAR_SKILLS = (1791041054, 324458883, 948487208, 1324307937, 597598050, 1369085966)
SPEAR_SIGNATURE = struct.pack('<6I', *SPEAR_SKILLS)
POSITION_MARKER = bytes.fromhex('18271c28c3b6114d')


def parse_tcp_segment(raw, datalink):
    """Validate the captured IP/TCP lengths and return only the TCP segment."""
    try:
        raw = bytes(raw)
        kind = None
        if datalink == 1:
            if len(raw) < 14: return None
            kind = struct.unpack_from('!H', raw, 12)[0]; offset = 14
            for _ in range(2):
                if kind not in (0x8100, 0x88a8): break
                kind = struct.unpack_from('!H', raw, offset + 2)[0]; offset += 4
            if kind not in (0x0800, 0x86dd): return None
        elif datalink in (0, 108):
            if len(raw) < 5: return None
            families = (int.from_bytes(raw[:4], 'little'), int.from_bytes(raw[:4], 'big'))
            if not any(f in (2, 10, 23, 24, 28, 30) for f in families): return None
            kind = 0x0800 if 2 in families else 0x86dd
            offset = 4
        elif datalink in (12, 101, 228, 229): offset = 0
        elif datalink == 113:
            kind = struct.unpack_from('!H', raw, 14)[0]; offset = 16
        elif datalink == 276:
            kind = struct.unpack_from('!H', raw, 0)[0]; offset = 20
        else: return None
        version = raw[offset] >> 4
        if kind is not None and kind != {4: 0x0800, 6: 0x86dd}.get(version): return None
        if datalink == 228 and version != 4: return None
        if datalink == 229 and version != 6: return None
        if version == 4:
            ihl = (raw[offset] & 15) * 4
            total = struct.unpack_from('!H', raw, offset + 2)[0]
            if ihl < 20 or total < ihl + 20 or offset + total > len(raw): return None
            if raw[offset + 9] != 6: return None
            # IP fragments require a separate IP reassembler; never guess TCP.
            if struct.unpack_from('!H', raw, offset + 6)[0] & 0x3fff: return None
            src = str(ipaddress.ip_address(raw[offset + 12:offset + 16]))
            dst = str(ipaddress.ip_address(raw[offset + 16:offset + 20]))
            tcp, end = offset + ihl, offset + total
        elif version == 6:
            if offset + 40 > len(raw): return None
            length = struct.unpack_from('!H', raw, offset + 4)[0]
            if not length or offset + 40 + length > len(raw): return None
            end = offset + 40 + length
            src = str(ipaddress.ip_address(raw[offset + 8:offset + 24]))
            dst = str(ipaddress.ip_address(raw[offset + 24:offset + 40]))
            nxt, tcp = raw[offset + 6], offset + 40
            for _ in range(8):
                if nxt == 6: break
                if tcp + 2 > end: return None
                if nxt in (0, 43, 60):
                    nxt, size = raw[tcp], (raw[tcp + 1] + 1) * 8
                elif nxt == 44:
                    # Reject all fragmented IPv6 packets, including first fragments.
                    if tcp + 8 > end or struct.unpack_from('!H', raw, tcp + 2)[0] & 0xfff9: return None
                    nxt, size = raw[tcp], 8
                elif nxt == 51: nxt, size = raw[tcp], (raw[tcp + 1] + 2) * 4
                else: return None
                tcp += size
                if tcp > end: return None
            if nxt != 6: return None
        else: return None
        if tcp + 20 > end: return None
        header = (raw[tcp + 12] >> 4) * 4
        if header < 20 or tcp + header > end: return None
        sport, dport, seq = struct.unpack_from('!HHI', raw, tcp)
        if 16000 not in (sport, dport): return None
        return dict(src=src, dst=dst, sport=sport, dport=dport, seq=seq,
                    flags=raw[tcp + 13], payload=raw[tcp + header:end])
    except (IndexError, ValueError, struct.error, TypeError): return None


def iter_pcap(data):
    """Read classic PCAP with a strict byte budget; PCAPNG is not guessed."""
    if len(data) < 24: raise ValueError('PCAP header is incomplete')
    formats = {b'\xd4\xc3\xb2\xa1': ('<', 1e6), b'\xa1\xb2\xc3\xd4': ('>', 1e6),
               b'\x4d\x3c\xb2\xa1': ('<', 1e9), b'\xa1\xb2\x3c\x4d': ('>', 1e9)}
    if data[:4] not in formats: raise ValueError('Unsupported PCAP format')
    order, scale = formats[data[:4]]
    _, major, minor, _, _, snaplen, link = struct.unpack_from(order + 'IHHIIII', data)
    if (major, minor) != (2, 4) or not 0 < snaplen <= 16 * 1024 * 1024:
        raise ValueError('Invalid PCAP header')
    offset = 24
    while offset < len(data):
        if offset + 16 > len(data): raise ValueError('Incomplete PCAP record header')
        sec, fraction, caplen, original = struct.unpack_from(order + 'IIII', data, offset); offset += 16
        if caplen > snaplen or caplen > original or fraction >= scale or offset + caplen > len(data):
            raise ValueError('Invalid PCAP record length or timestamp')
        yield sec + fraction / scale, data[offset:offset + caplen], link
        offset += caplen


def _brotli(payload, limit=MAX_DECODED):
    import brotli
    decoder = brotli.Decompressor()
    output = bytearray()
    piece = payload
    for _ in range(32):
        block = decoder.process(piece, output_buffer_limit=limit - len(output) + 1)
        output.extend(block)
        if len(output) > limit: raise ValueError('Brotli output exceeds limit')
        if decoder.is_finished(): return bytes(output)
        if not block: raise ValueError('Brotli stream is incomplete')
        piece = b''
    raise ValueError('Brotli iteration limit')


class _Stream:
    def __init__(self, owner, key, server):
        self.owner, self.key, self.server = owner, key, server
        self.name = f'stream-{len(owner._streams) + 1}'
        self.source_id = key[0]
        self.origin = None; self.chunks = []; self.cursor = None
        self.gap_since = None; self.pending_since = None
        self.synced = False; self.ever_synced = False; self.validated = False; self.last_frame = None
        self.last_seen = 0.; self.generation = 0; self.frames = 0

    def reset(self, reason):
        self.owner._revoke(self.name)
        self.origin = None; self.chunks = []; self.cursor = None
        self.gap_since = None; self.pending_since = None; self.synced = False; self.validated = False
        self.generation += 1
        if reason:
            self.owner._counts[reason] += 1
            self.owner._warn(reason)

    def expire(self, now):
        if ((self.gap_since is not None and now - self.gap_since > STALE_SECONDS) or
                (self.pending_since is not None and now - self.pending_since > STALE_SECONDS)):
            self.reset('gap_timeout')
        if self.server and self.last_frame is not None and now - self.last_frame > STALE_SECONDS:
            self.synced = False
            self.owner._revoke(self.name)

    def accept(self, segment, timestamp):
        self.last_seen = timestamp
        if segment['flags'] & 2: self.reset('connection_reset')
        payload = segment['payload']
        if payload:
            seq = (segment['seq'] + bool(segment['flags'] & 2)) & 0xffffffff
            if self.origin is None: self.origin = seq
            start = (seq - self.origin + 2**31) % 2**32 - 2**31
            end = start + len(payload)
            for old_start, old_data in self.chunks:
                lo, hi = max(start, old_start), min(end, old_start + len(old_data))
                if lo < hi and payload[lo - start:hi - start] != old_data[lo - old_start:hi - old_start]:
                    self.reset('conflicting_overlap'); return
            combined = sorted(self.chunks + [(start, payload)], key=lambda row: row[0])
            merged = []
            for position, body in combined:
                if merged and position <= merged[-1][0] + len(merged[-1][1]):
                    previous, existing = merged[-1]
                    overlap = previous + len(existing) - position
                    if overlap < len(body): merged[-1] = (previous, existing + body[overlap:])
                else: merged.append((position, body))
            if sum(len(body) for _, body in merged) > MAX_BUFFER or len(merged) > 4096:
                self.reset('buffer_limit'); return
            self.chunks = merged
            # Before any boundary has been validated in this generation, an
            # earlier out-of-order segment may contain the missing START.
            if not self.validated: self.cursor = self.chunks[0][0]
            self._drain(timestamp)
            # Retain recent consumed bytes to detect conflicting retransmissions.
            if self.cursor is not None:
                keep = self.cursor - 65536
                self.chunks = [(max(position, keep), body[max(0, keep - position):])
                               for position, body in self.chunks if position + len(body) > keep]
        if segment['flags'] & 5: self.reset('connection_closed')

    def _drain(self, timestamp):
        if not self.chunks: return
        deadline = time.monotonic() + .15
        if self.cursor is None: self.cursor = self.chunks[0][0]
        while True:
            if time.monotonic() > deadline:
                self.reset('processing_limit'); return
            chosen = next(((start, data) for start, data in self.chunks
                           if start <= self.cursor < start + len(data)), None)
            if chosen is None:
                if any(start > self.cursor for start, _ in self.chunks):
                    if self.gap_since is None: self.gap_since = timestamp
                return
            self.gap_since = None
            start, data = chosen
            block = data[self.cursor - start:]
            if self.server:
                # Once a batch established an exact boundary, unwrapped
                # envelopes can follow it. They are not a resynchronization
                # heuristic; several observed control/opaque types use this.
                if self.synced and not block.startswith(START):
                    if self.pending_since is None: self.pending_since = timestamp
                    if len(block) < 9: return
                    typ, length, encoding = struct.unpack_from('<IIB', block)
                    if length > MAX_BUFFER - 9:
                        self.reset('invalid_frame'); return
                    if len(block) < 9 + length: return
                    body = block[9:9 + length]
                    self.cursor += 9 + length; self.pending_since = None
                    self.frames += 1; self.last_frame = timestamp
                    self.owner._frame(self, typ, encoding, body, timestamp)
                    continue
                found = block.find(START)
                if found < 0:
                    self.cursor += max(0, len(block) - len(START) + 1)
                    if self.pending_since is None: self.pending_since = timestamp
                    return
                if found:
                    self.cursor += found; block = block[found:]
                if self.pending_since is None: self.pending_since = timestamp
                offset, batch = 0, []
                while True:
                    if time.monotonic() > deadline:
                        self.reset('processing_limit'); return
                    if len(block) - offset < 9: return
                    typ, length, encoding = struct.unpack_from('<IIB', block, offset)
                    if length > MAX_BUFFER - 9 or len(batch) >= 4096:
                        self.owner._counts['invalid_batches'] += 1
                        self.owner._revoke(self.name); self.synced = False
                        self.cursor += 1; self.pending_since = None; break
                    if offset and block[offset:offset + 9] == START:
                        self.owner._counts['invalid_batches'] += 1
                        self.owner._revoke(self.name); self.synced = False
                        self.cursor += offset; self.pending_since = None; break
                    if len(block) - offset < 9 + length: return
                    payload = block[offset + 9:offset + 9 + length]
                    batch.append((typ, encoding, payload)); offset += 9 + length
                    if typ == 20316 and not length and not encoding:
                        self.cursor += offset; self.pending_since = None
                        self.synced = self.ever_synced = self.validated = True; self.last_frame = timestamp
                        self.frames += len(batch)
                        for typ, flag, body in batch: self.owner._frame(self, typ, flag, body, timestamp)
                        break
                # A valid completed batch or an invalid prefix advanced cursor.
            else:
                if not self.synced:
                    marker = struct.pack('<II', 190006, 0)
                    found = block.find(marker)
                    if found < 0:
                        self.cursor += max(0, len(block) - 7)
                        if self.pending_since is None: self.pending_since = timestamp
                        return
                    self.cursor += found; block = block[found:]
                if len(block) < 13:
                    if self.pending_since is None: self.pending_since = timestamp
                    return
                typ, length = struct.unpack_from('<II', block)
                if length > MAX_BUFFER - 13:
                    self.synced = False; self.cursor += 1; self.pending_since = None; continue
                if len(block) < 13 + length:
                    if self.pending_since is None: self.pending_since = timestamp
                    return
                self.synced = self.ever_synced = self.validated = True; self.last_frame = timestamp
                self.frames += 1; self.owner._counts['client_frames'] += 1
                self.cursor += 13 + length; self.pending_since = None


class PacketInspector:
    def __init__(self, on_position=None, on_reset=None):
        self._lock = RLock(); self._streams = {}; self._counts = Counter()
        self._positions = {}; self._bosses = {}; self._warnings = []
        self._time = None; self._fed_at = None
        self._on_position=on_position;self._on_reset=on_reset
        self.spear=SpearPacketState()

    def _warn(self, reason):
        if reason not in self._warnings:
            self._warnings.append(reason)
            self._warnings = self._warnings[-16:]

    def _revoke(self, name):
        self._positions = {key: value for key, value in self._positions.items() if key[0] != name}
        self._bosses = {key: value for key, value in self._bosses.items() if key[0] != name}
        self.spear.reset(stream_id=name)
        if self._on_reset:self._on_reset(name)

    def feed(self, raw, datalink, timestamp, source_id='default'):
        timestamp = float(timestamp)
        if not math.isfinite(timestamp): raise ValueError('Invalid capture timestamp')
        with self._lock:
            if self._time is not None and timestamp < self._time - STALE_SECONDS:
                self._counts['stale_packets'] += 1; return
            self._time = timestamp if self._time is None else max(self._time, timestamp)
            self._fed_at = time.monotonic()
            for stream in self._streams.values(): stream.expire(self._time)
            self._counts['packets'] += 1
            segment = parse_tcp_segment(raw, datalink)
            if segment is None:
                self._counts['ignored_packets'] += 1; return
            self._counts['tcp_packets'] += 1
            key = (str(source_id), segment['src'], segment['sport'], segment['dst'], segment['dport'])
            if key not in self._streams:
                if len(self._streams) >= MAX_STREAMS:
                    oldest = min(self._streams, key=lambda k: self._streams[k].last_seen)
                    self._revoke(self._streams[oldest].name); del self._streams[oldest]
                    self._counts['evicted_streams'] += 1; self._warn('stream_limit')
                stream = _Stream(self, key, segment['sport'] == 16000)
                # Names remain unique after eviction.
                stream.name = f'stream-{self._counts["created_streams"] + 1}'
                self._counts['created_streams'] += 1; self._streams[key] = stream
            self._streams[key].accept(segment, self._time)

    def _put_candidate(self, store, key, value, limit):
        if key not in store:
            keys = [entry for entry in store if entry[0] == key[0]]
            if len(keys) >= limit:
                oldest = min(keys, key=lambda entry: store[entry]['timestamp'])
                del store[oldest]
                self._warn('candidate_limit')
        store[key] = value

    def _frame(self, stream, typ, encoding, body, timestamp):
        self._counts['server_frames'] += 1
        if typ in (20132, 20316) and not body and not encoding:
            self.spear.feed(typ,body,timestamp,stream_id=stream.name,generation=stream.generation)
            return
        if encoding not in (0, 1):
            self._counts['opaque'] += 1; self._warn('unknown_encoding'); return
        remaining = MAX_TOTAL_DECODED - self._counts['decoded_bytes']
        if remaining <= 0:
            self._counts['opaque'] += 1; self._warn('decoded_budget_exhausted'); return
        if encoding == 1:
            try:
                body = _brotli(body, min(MAX_DECODED, remaining))
                self._counts['brotli_valid'] += 1
            except Exception:
                self._counts['opaque'] += 1; self._counts['decode_errors'] += 1
                self._warn('opaque_compressed_payload'); return
        if len(body) > remaining:
            self._counts['opaque'] += 1; self._warn('decoded_budget_exhausted'); return
        self._counts['decoded_bytes'] += len(body)
        self.spear.feed(typ,body,timestamp,stream_id=stream.name,generation=stream.generation)
        if (typ in (170826,170830) and len(body)==20) or (typ==170815 and len(body)==16):
            entity, x, y=struct.unpack_from('<Qff',body)
            heading=struct.unpack_from('<f',body,16)[0] if len(body)==20 else None
            if 0 < entity < 2**64 and all(math.isfinite(v) and abs(v) < 1e6 for v in (x,y)) and (heading is None or (math.isfinite(heading) and abs(heading)<1e6)):
                sample=dict(entity_id=entity, x=x, y=y, heading=heading,
                    timestamp=timestamp, source_type=typ, stream_id=stream.name, source_id=stream.source_id,
                    identity_confirmed=False, profile='observed-position-v2', generation=stream.generation)
                self._put_candidate(self._positions, (stream.name, entity), sample, 256)
                self._counts['position_records'] += 1
                self.spear.update_position(sample)
                if self._on_position:self._on_position(sample)
            else:
                self._counts['decode_errors'] += 1; self._warn('invalid_position')
        elif typ == 20051 and len(body) == 1636:
            if body[638:662] == SPEAR_SIGNATURE and body[1100:1108] == POSITION_MARKER:
                entity = struct.unpack_from('<Q', body)[0]
                x, y, heading = struct.unpack_from('<fff', body, 1108)
                if entity and all(math.isfinite(v) and abs(v) < 1e6 for v in (x, y, heading)):
                    self._put_candidate(self._bosses, (stream.name, entity), dict(entity_id=entity, x=x, y=y, heading=heading,
                        timestamp=timestamp, source_type=typ, stream_id=stream.name, source_id=stream.source_id,
                        role_confirmed=False, evidence='six_skill_ids_and_position_marker', generation=stream.generation), 64)
                    self._counts['boss_records'] += 1
                else: self._counts['decode_errors'] += 1
            else: self._counts['opaque'] += 1
        else: self._counts['opaque'] += 1

    def snapshot(self, now=None):
        with self._lock:
            if now is None:
                now = self._time if self._time is not None else 0.
                if self._fed_at is not None: now += max(0., time.monotonic() - self._fed_at)
                if now > 100000000: now = max(now, time.time())
            for stream in self._streams.values(): stream.expire(now)
            positions = [v for v in self._positions.values() if now - v['timestamp'] <= STALE_SECONDS]
            # Spawn evidence remains a profile candidate, but not a live target.
            bosses = [dict(v, stale=now - v['timestamp'] > STALE_SECONDS) for v in self._bosses.values()]
            servers = [s for s in self._streams.values() if s.server]
            state = 'synced' if any(s.synced for s in servers) else ('degraded' if any(s.ever_synced for s in servers) else 'waiting')
            result = dict(frames=self._counts['server_frames'], server_frames=self._counts['server_frames'],
                client_frames=self._counts['client_frames'], streams=[dict(stream_id=s.name, source_id=s.source_id,
                    direction='server' if s.server else 'client', synced=s.synced, frames=s.frames,
                    generation=s.generation) for s in self._streams.values()], stream_count=len(self._streams),
                sync_state=state, positions=positions, boss_candidates=bosses,
                opaque=self._counts['opaque'], decode_errors=self._counts['decode_errors'],
                counters=dict(self._counts), warnings=list(self._warnings), last_capture_timestamp=self._time,
                self_identity_confirmed=False, element=None, departure_signal_confirmed=False,
                success_confirmed=False, game_input_allowed=False,
                experimental_profile=True, profile='observed-20261115-position-v2',
                spear=self.spear.snapshot(now=now))
            return json.loads(json.dumps(result, allow_nan=False))


def analyze_archive(path, position_observer=None, reset_observer=None):
    """Replay only allowlisted PCAP members without extracting any ZIP paths."""
    packets = []; total = 0
    deadline = time.monotonic() + 30
    if Path(path).stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Archive file exceeds analysis size limit')
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 256: raise ValueError('Archive contains too many files')
        names = [i.filename for i in infos]
        if len(names) != len(set(names)): raise ValueError('Duplicate ZIP member name')
        for info in infos:
            name = PurePosixPath(info.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in info.filename:
                raise ValueError('Unsafe ZIP member path')
            total += info.file_size
            if total > 32 * 1024 * 1024: raise ValueError('Archive exceeds analysis size limit')
            if re.fullmatch(r'adapter-\d{2}\.pcap', info.filename):
                for timestamp, raw, link in iter_pcap(archive.read(info)):
                    if time.monotonic() > deadline: raise ValueError('Archive replay time limit')
                    packets.append((timestamp, info.filename, raw, link))
                    if len(packets) > 200000: raise ValueError('Archive contains too many packets')
        if not any(re.fullmatch(r'adapter-\d{2}\.pcap', name) for name in names):
            raise ValueError('Archive contains no supported PCAP files')
    inspector = PacketInspector(on_position=position_observer,on_reset=reset_observer)
    for timestamp, source, raw, link in sorted(packets, key=lambda row: row[0]):
        if time.monotonic() > deadline: raise ValueError('Archive replay time limit')
        inspector.feed(raw, link, timestamp, source)
    result = inspector.snapshot(now=max((row[0] for row in packets), default=0.))
    result['replayed_packets'] = len(packets)
    return result


def run_selftest(base_dir):
    """Replay synthetic split/reordered TCP plus bundled bounded Brotli only."""
    import brotli
    base_dir = Path(base_dir)
    def envelope(typ, body, flag=0):
        return struct.pack('<IIB', typ, len(body), flag) + body
    spawn = bytearray(1636)
    struct.pack_into('<Q', spawn, 0, 202)
    spawn[638:662] = SPEAR_SIGNATURE
    spawn[1100:1108] = POSITION_MARKER
    struct.pack_into('<fff', spawn, 1108, 0., 163., math.pi)
    wire = (START + envelope(170826, brotli.compress(struct.pack('<Qfff', 101, -1.25, 2.5, .75)), 1) +
            envelope(20051, bytes(spawn)) + END)
    def packet(sequence, payload):
        header = bytearray(40)
        header[0] = 0x45; struct.pack_into('!H', header, 2, 40 + len(payload)); header[9] = 6
        header[12:16] = ipaddress.ip_address('198.51.100.20').packed
        header[16:20] = ipaddress.ip_address('192.0.2.10').packed
        struct.pack_into('!HHI', header, 20, 16000, 54321, sequence)
        header[32] = 0x50; header[33] = 0x18
        return b'\0' * 12 + b'\x08\x00' + header + payload
    chunks = [(0, wire[:31]), (100, wire[100:]), (31, wire[31:100])]
    pcap = io.BytesIO()
    pcap.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
    for index, (offset, payload) in enumerate(chunks):
        raw = packet(300 + offset, payload)
        pcap.write(struct.pack('<IIII', 100, index * 100000, len(raw), len(raw))); pcap.write(raw)
    path = base_dir / 'selftest-packet-decode.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('metadata.json', '{"synthetic_only":true}')
        archive.writestr('adapter-00.pcap', pcap.getvalue())
    result = analyze_archive(path)
    assert result['server_frames'] == 4 and result['counters']['brotli_valid'] == 1
    assert result['positions'][0]['entity_id'] == 101 and result['positions'][0]['x'] == -1.25
    assert result['boss_candidates'][0]['entity_id'] == 202
    assert not result['game_input_allowed'] and not result['self_identity_confirmed']
    result['selftest_archive'] = path.name
    return result
