"""Bounded, passive Npcap diagnostics for a verified game's existing TCP connection.

This module observes copies; it has no packet-transmission or game-input API.
Experimental diagnostics read candidate records without identifying self or
deciding mechanics.
"""
import ctypes as C
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import struct
import subprocess
from threading import Event, RLock, Thread
import time
import uuid
import zipfile
from packet_decode import PacketInspector

MAX_SECONDS = 60
MAX_BYTES = 12 * 1024 * 1024
UP = 2


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _endpoint(value):
    if value.startswith('['):
        address, port = value[1:].rsplit(']:', 1)
    else:
        address, port = value.rsplit(':', 1)
    # Preserve IPv6 scope for tuple identity; BPF uses only the IP literal.
    return str(ipaddress.ip_address(address)), int(port)


def normalize_connections(connections):
    result = set()
    for row in connections:
        try:
            local = str(ipaddress.ip_address(row['local_address']))
            remote = str(ipaddress.ip_address(row['remote_address']))
            lp, rp = int(row['local_port']), int(row['remote_port'])
            if 16000 not in (lp, rp) or not (0 < lp <= 65535 and 0 < rp <= 65535):continue
            if ipaddress.ip_address(local).is_unspecified or ipaddress.ip_address(remote).is_unspecified:continue
            if ipaddress.ip_address(local).version != ipaddress.ip_address(remote).version:continue
            result.add((local, lp, remote, rp))
        except (ValueError, KeyError, TypeError):continue
    return [dict(local_address=a, local_port=b, remote_address=c, remote_port=d)
            for a,b,c,d in sorted(result)]


def parse_netstat(text, pid):
    rows=[]
    for line in text.splitlines():
        fields=line.split()
        if len(fields)!=5 or fields[0].upper()!='TCP' or fields[-1]!=str(pid):continue
        if fields[3].upper()!='ESTABLISHED':continue
        try:
            la,lp=_endpoint(fields[1]);ra,rp=_endpoint(fields[2])
            rows.append(dict(local_address=la,local_port=lp,remote_address=ra,remote_port=rp))
        except (ValueError, IndexError):continue
    return normalize_connections(rows)


def game_connections(pid):
    if os.name!='nt':raise OSError('실제 패킷 관측은 Windows에서만 지원합니다.')
    # Omitting -p tcp includes IPv6 TCP connections as well; the parser then
    # restricts protocol, PID, established state and the server port itself.
    proc=subprocess.run(['netstat','-ano'],capture_output=True,
        timeout=2,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode:raise OSError('게임 TCP 연결을 조회하지 못했습니다.')
    return parse_netstat(proc.stdout.decode('utf-8',errors='replace'),pid)


def bpf_for_connections(connections):
    terms=[]
    for row in normalize_connections(connections):
        a,b,c,d=(row[k] for k in ('local_address','local_port','remote_address','remote_port'))
        # Validated numeric IPs/ports only. Scoped IPv6 remains exact in the
        # connection metadata; libpcap host syntax takes the unscoped literal.
        a=a.split('%')[0];c=c.split('%')[0]
        terms.append(f'((src host {a} and src port {b} and dst host {c} and dst port {d}) or '
                     f'(src host {c} and src port {d} and dst host {a} and dst port {b}))')
    if not terms:raise ValueError('포트 16000의 활성 게임 연결이 없습니다. 던전 안에서 다시 시작하세요.')
    return 'tcp and ('+' or '.join(terms)+')'


def packet_endpoints(raw, datalink):
    """Read TCP header endpoints only, never interpret application payloads."""
    try:
        if datalink==1:
            if len(raw)<14:return None
            kind=struct.unpack_from('!H',raw,12)[0];offset=14
            for _ in range(2):
                if kind not in (0x8100,0x88a8):break
                kind=struct.unpack_from('!H',raw,offset+2)[0];offset+=4
            if kind not in (0x0800,0x86dd):return None
        elif datalink in (0,108):offset=4
        elif datalink in (12,101,228,229):offset=0
        elif datalink==113:offset=16
        elif datalink==276:offset=20
        else:return None
        version=raw[offset]>>4
        if version==4:
            ihl=(raw[offset]&15)*4
            if ihl<20 or raw[offset+9]!=6:return None
            if struct.unpack_from('!H',raw,offset+6)[0]&0x1fff:return None
            total=struct.unpack_from('!H',raw,offset+2)[0]
            if total<ihl+20:return None
            src=str(ipaddress.ip_address(raw[offset+12:offset+16]))
            dst=str(ipaddress.ip_address(raw[offset+16:offset+20]))
            tcp=offset+ihl
        elif version==6:
            src=str(ipaddress.ip_address(raw[offset+8:offset+24]))
            dst=str(ipaddress.ip_address(raw[offset+24:offset+40]))
            nxt=raw[offset+6];tcp=offset+40
            for _ in range(8):
                if nxt==6:break
                if nxt in (0,43,60):
                    nxt,length=raw[tcp],(raw[tcp+1]+1)*8;tcp+=length
                elif nxt==44:
                    if struct.unpack_from('!H',raw,tcp+2)[0]&0xfff8:return None
                    nxt=raw[tcp];tcp+=8
                elif nxt==51:
                    nxt,length=raw[tcp],(raw[tcp+1]+2)*4;tcp+=length
                else:return None
            if nxt!=6:return None
        else:return None
        if len(raw)<tcp+20 or (raw[tcp+12]>>4)*4<20:return None
        sport,dport=struct.unpack_from('!HH',raw,tcp)
        return src,sport,dst,dport
    except (IndexError,ValueError,struct.error):return None


def _allowed_endpoints(connections):
    result=set()
    for r in connections:
        a,b,c,d=(r[k] for k in ('local_address','local_port','remote_address','remote_port'))
        # Link scope IDs aren't present in captured IPv6 headers.
        row=(a.split('%')[0],b,c.split('%')[0],d)
        result.add(row);result.add((row[2],row[3],row[0],row[1]))
    return result


def write_pcap_header(stream, datalink):
    stream.write(struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,datalink))


def write_pcap_record(stream, sec, usec, payload, original_len):
    stream.write(struct.pack('<IIII',int(sec),int(usec),len(payload),int(original_len)))
    stream.write(payload)


class _PcapIf(C.Structure):pass
_PcapIf._fields_=[('next',C.POINTER(_PcapIf)),('name',C.c_char_p),('description',C.c_char_p),
                 ('addresses',C.c_void_p),('flags',C.c_uint32)]
class _Timeval(C.Structure):
    _fields_=[('sec',C.c_int32),('usec',C.c_int32)]
class _Header(C.Structure):
    _fields_=[('time',_Timeval),('caplen',C.c_uint32),('length',C.c_uint32)]
class _Bpf(C.Structure):
    _fields_=[('length',C.c_uint),('instructions',C.c_void_p)]
class _Stats(C.Structure):
    # Npcap's Windows pcap_stat additionally has ps_capt; reserve it to avoid
    # a native buffer overwrite even though diagnostics only need three fields.
    _fields_=[('received',C.c_uint32),('dropped',C.c_uint32),('interface_dropped',C.c_uint32),('captured',C.c_uint32)]


class NpcapAPI:
    def __init__(self):
        if os.name!='nt':raise OSError('Npcap 수신은 Windows에서만 지원합니다.')
        system=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'
        self.dll_directory=os.add_dll_directory(str(system/'Npcap')) if (system/'Npcap').is_dir() else None
        candidates=[system/'Npcap'/'wpcap.dll',system/'wpcap.dll']
        path=next((p for p in candidates if p.is_file()),None)
        if path is None:raise OSError('설치된 Npcap(wpcap.dll)을 찾지 못했습니다. 모비레이 설치는 변경하지 않았습니다.')
        self.lib=C.CDLL(str(path));L=self.lib
        L.pcap_findalldevs.argtypes=[C.POINTER(C.POINTER(_PcapIf)),C.c_char_p];L.pcap_findalldevs.restype=C.c_int
        L.pcap_freealldevs.argtypes=[C.POINTER(_PcapIf)]
        L.pcap_open_live.argtypes=[C.c_char_p,C.c_int,C.c_int,C.c_int,C.c_char_p];L.pcap_open_live.restype=C.c_void_p
        L.pcap_compile.argtypes=[C.c_void_p,C.POINTER(_Bpf),C.c_char_p,C.c_int,C.c_uint32];L.pcap_compile.restype=C.c_int
        L.pcap_setfilter.argtypes=[C.c_void_p,C.POINTER(_Bpf)];L.pcap_setfilter.restype=C.c_int
        L.pcap_freecode.argtypes=[C.POINTER(_Bpf)]
        L.pcap_setnonblock.argtypes=[C.c_void_p,C.c_int,C.c_char_p];L.pcap_setnonblock.restype=C.c_int
        L.pcap_next_ex.argtypes=[C.c_void_p,C.POINTER(C.POINTER(_Header)),C.POINTER(C.POINTER(C.c_ubyte))];L.pcap_next_ex.restype=C.c_int
        L.pcap_datalink.argtypes=[C.c_void_p];L.pcap_datalink.restype=C.c_int
        L.pcap_geterr.argtypes=[C.c_void_p];L.pcap_geterr.restype=C.c_char_p
        L.pcap_close.argtypes=[C.c_void_p]
        L.pcap_stats.argtypes=[C.c_void_p,C.POINTER(_Stats)];L.pcap_stats.restype=C.c_int

    def devices(self):
        head=C.POINTER(_PcapIf)();error=C.create_string_buffer(256)
        if self.lib.pcap_findalldevs(C.byref(head),error):raise OSError(error.value.decode(errors='replace'))
        rows=[]
        try:
            current=head
            while current and len(rows)<64:
                d=current.contents
                rows.append(dict(name=d.name.decode(errors='replace'),description=(d.description or b'').decode(errors='replace'),flags=int(d.flags)))
                current=d.next
        finally:
            if head:self.lib.pcap_freealldevs(head)
        return rows

    def _error(self,handle):
        return (self.lib.pcap_geterr(handle) or b'Npcap error').decode(errors='replace')

    def open(self,name,bpf):
        error=C.create_string_buffer(256)
        handle=self.lib.pcap_open_live(name.encode(),65535,0,50,error)
        if not handle:raise OSError(error.value.decode(errors='replace'))
        program=_Bpf();compiled=False
        try:
            if self.lib.pcap_compile(handle,C.byref(program),bpf.encode('ascii'),1,0xffffffff):raise OSError(self._error(handle))
            compiled=True
            if self.lib.pcap_setfilter(handle,C.byref(program)):raise OSError(self._error(handle))
            if self.lib.pcap_setnonblock(handle,1,error):raise OSError(error.value.decode(errors='replace'))
            return handle
        except Exception:
            self.lib.pcap_close(handle);raise
        finally:
            if compiled:self.lib.pcap_freecode(C.byref(program))

    def next_packet(self,handle):
        header=C.POINTER(_Header)();data=C.POINTER(C.c_ubyte)()
        code=self.lib.pcap_next_ex(handle,C.byref(header),C.byref(data))
        if code==0:return None
        if code!=1:raise OSError(self._error(handle))
        h=header.contents
        if h.caplen>65535 or h.caplen>h.length:raise OSError('Npcap returned invalid capture length')
        return h.time.sec,h.time.usec,C.string_at(data,h.caplen),h.length

    def datalink(self,handle):return self.lib.pcap_datalink(handle)
    def close(self,handle):self.lib.pcap_close(handle)
    def stats(self,handle):
        stats=_Stats()
        if self.lib.pcap_stats(handle,C.byref(stats)):return None
        return dict(received=int(stats.received),dropped=int(stats.dropped),interface_dropped=int(stats.interface_dropped))


class PacketCapture:
    def __init__(self,base_dir,api_factory=NpcapAPI,connection_provider=game_connections):
        self.base_dir=Path(base_dir);self.api_factory=api_factory;self.connection_provider=connection_provider
        self.lock=RLock();self.stop_event=Event();self.thread=None;self.started=None
        self.state=dict(running=False,packets=0,bytes=0,file_bytes=0,elapsed=0.,reason='not_started',error=None,
                        session_dir=None,devices=[],pid=None,filter=None)
        self.files=[]
        self.inspector=PacketInspector();self.inspector_error=None
        self.inspector_frozen=None;self.last_packet_epoch=None

    def start(self,pid,seconds=60,max_bytes=MAX_BYTES):
        if isinstance(pid,bool) or not isinstance(pid,int) or pid<=0:raise ValueError('검증된 게임 PID가 필요합니다.')
        if not 0<float(seconds)<=MAX_SECONDS:raise ValueError('관측 시간은 0초 초과, 60초 이하입니다.')
        if not 24<=int(max_bytes)<=MAX_BYTES:raise ValueError('관측 파일 제한은 24바이트 이상, 12MiB 이하입니다.')
        with self.lock:
            if self.state['running'] or (self.thread and self.thread.is_alive()):raise RuntimeError('이미 패킷 관측 중입니다.')
            self.stop_event=Event();self.files=[];self.started=time.monotonic()
            self.inspector=PacketInspector();self.inspector_error=None
            self.inspector_frozen=None;self.last_packet_epoch=None
            session=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8]
            directory=self.base_dir/'packet_logs'/session
            self.state=dict(running=True,packets=0,bytes=0,file_bytes=0,elapsed=0.,reason='starting',error=None,
                session_dir=str(directory),devices=[],pid=pid,filter=None,connections=[],started_at=utc_now(),
                max_seconds=float(seconds),max_bytes=int(max_bytes),skipped_packets=0,truncated_packets=0,
                schema_version=2,app_version='0.10.5-packet-inspect',
                game_input_allowed=False,game_messages_decoded=False,coordinates_decoded=False)
            self.thread=Thread(target=self._run,daemon=True,name='passive-game-capture')
            self.thread.start()

    def stop(self):self.stop_event.set()
    def close(self):
        self.stop()
        if self.thread:self.thread.join(timeout=2)

    def status(self):
        with self.lock:
            value=json.loads(json.dumps(self.state))
            if value['running'] and self.started is not None:value['elapsed']=round(time.monotonic()-self.started,3)
            return value

    def _update(self,**fields):
        with self.lock:self.state.update(fields)

    def decoded_snapshot(self):
        with self.lock:
            frozen=self.inspector_frozen
        result=json.loads(json.dumps(frozen)) if frozen is not None else self.inspector.snapshot()
        result['inspector_error']=self.inspector_error
        return result

    def _decoded_file(self):
        directory=Path(self.state['session_dir'])
        path=directory/'decoded-summary.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.decoded_snapshot(),ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)

    def _metadata(self):
        directory=Path(self.state['session_dir']);directory.mkdir(parents=True,exist_ok=True)
        temporary=directory/'metadata.tmp'
        temporary.write_text(json.dumps(self.status(),ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(directory/'metadata.json')

    def _run(self):
        opened=[];api=None;reason='error';error=None
        try:
            self._metadata()
            connections=normalize_connections(self.connection_provider(self.state['pid']))
            bpf=bpf_for_connections(connections);allowed=_allowed_endpoints(connections)
            self._update(connections=connections,filter=bpf)
            api=self.api_factory();devices=api.devices();selected=[d for d in devices if int(d.get('flags',0))&UP][:16]
            self._update(devices=[dict(d,state='not_up' if not int(d.get('flags',0))&UP else 'not_opened') for d in devices[:64]])
            if not selected:raise OSError('사용 가능한 활성 Npcap 장치가 없습니다.')
            directory=Path(self.state['session_dir'])
            for index,device in enumerate(selected):
                if self.stop_event.is_set():reason='stopped';break
                if self.state['file_bytes']+24>self.state['max_bytes']:reason='byte_limit';break
                handle=None;stream=None
                row=next(r for r in self.state['devices'] if r['name']==device['name'])
                try:
                    handle=api.open(device['name'],bpf);link=int(api.datalink(handle))
                    if link not in (0,1,12,101,108,113,228,229,276):raise OSError(f'지원하지 않는 링크 형식: {link}')
                    name=f'adapter-{index:02d}.pcap';stream=(directory/name).open('wb')
                    write_pcap_header(stream,link);stream.flush()
                    self.state['file_bytes']+=24;self.files.append(name)
                    with self.lock:row.update(state='capturing',datalink=link,file=name,packets=0)
                    opened.append((handle,stream,row,link));handle=None;stream=None
                except Exception as exc:
                    with self.lock:row.update(state='failed',error=str(exc)[:500])
                finally:
                    if stream:stream.close()
                    if handle is not None:api.close(handle)
            self._metadata()
            if not opened:
                if self.stop_event.is_set():reason='stopped';return
                raise OSError('Npcap 장치를 열지 못했습니다. 장치별 오류와 캡처 권한을 확인하세요.')
            self._update(reason='capturing');last_check=time.monotonic()
            while True:
                now=time.monotonic()
                if self.stop_event.is_set():reason='stopped';break
                if now-self.started>=self.state['max_seconds']:reason='time_limit';break
                if now-last_check>=2:
                    current=normalize_connections(self.connection_provider(self.state['pid']))
                    if current!=connections:
                        reason='disconnected' if not current else 'connection_changed';break
                    last_check=now
                got=False
                for handle,stream,row,link in opened:
                    for _ in range(32):
                        if self.stop_event.is_set() or time.monotonic()-self.started>=self.state['max_seconds']:break
                        packet=api.next_packet(handle)
                        if packet is None:break
                        got=True;sec,usec,data,original=packet
                        if packet_endpoints(data,link) not in allowed:
                            with self.lock:self.state['skipped_packets']+=1
                            continue
                        if len(data)>65535 or original<len(data) or not 0<=usec<1000000:raise ValueError('잘못된 캡처 레코드')
                        if self.state['file_bytes']+16+len(data)>self.state['max_bytes']:
                            reason='byte_limit';return
                        write_pcap_record(stream,sec,usec,data,original)
                        self.last_packet_epoch=sec+usec/1000000
                        with self.lock:
                            self.state['packets']+=1;self.state['bytes']+=len(data);self.state['file_bytes']+=16+len(data)
                            if original>len(data):self.state['truncated_packets']+=1
                            row['packets']+=1
                        if self.inspector_error is None:
                            try:self.inspector.feed(data,link,sec+usec/1000000,source_id=row['file'])
                            except Exception as exc:
                                self.inspector_error=f'{type(exc).__name__}: {exc}'[:500]
                if not got:self.stop_event.wait(.01)
        except Exception as exc:error=f'{type(exc).__name__}: {exc}'[:1000];reason='error'
        finally:
            for handle,stream,row,_ in opened:
                try:
                    if hasattr(api,'stats'):
                        stats=api.stats(handle)
                        with self.lock:row['capture_stats']=stats
                except Exception as exc:
                    with self.lock:row['stats_error']=str(exc)[:300]
                try:stream.close()
                except Exception as exc:error=error or str(exc)
                try:api.close(handle)
                except Exception as exc:error=error or str(exc)
                with self.lock:row['state']='closed'
            with self.lock:
                self.inspector_frozen=self.inspector.snapshot(now=self.last_packet_epoch or 0.)
                self._update(running=False,reason=reason,error=error,elapsed=round(time.monotonic()-self.started,3),finished_at=utc_now())
                try:self._metadata();self._decoded_file()
                except Exception as exc:self._update(error=error or ('로그 저장 실패: '+str(exc)))

    def export(self,destination,evidence=None):
        with self.lock:
            if self.state['running']:raise RuntimeError('관측을 정지한 뒤 ZIP을 저장하세요.')
            if not self.state['session_dir']:raise ValueError('먼저 패킷 관측을 시작하세요.')
            directory=Path(self.state['session_dir']);destination=Path(destination)
            # Refresh running=False in final metadata after worker cleanup.
            self._metadata()
            self._decoded_file()
            temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
                    z.write(directory/'metadata.json','metadata.json')
                    z.write(directory/'decoded-summary.json','decoded-summary.json')
                    for name in self.files:
                        if (directory/name).is_file():z.write(directory/name,name)
                    if evidence is not None:
                        for source,name in evidence.export_members():
                            relative=Path(name)
                            if relative.is_absolute() or '..' in relative.parts or not name.startswith('evidence/'):
                                raise ValueError('잘못된 관찰 증거 파일 경로')
                            if not Path(source).resolve().is_relative_to(directory.resolve()):
                                raise ValueError('현재 수신 기록 밖의 파일은 내보내지 않습니다.')
                            z.write(source,name)
                os.replace(temporary,destination)
                return destination
            except Exception:
                temporary.unlink(missing_ok=True);raise


def run_selftest(base_dir):
    """Exercise the worker and PCAP/ZIP format with synthetic traffic only."""
    connection=dict(local_address='192.0.2.10',local_port=54321,remote_address='198.51.100.20',remote_port=16000)
    ip=bytearray(20);ip[0]=0x45;struct.pack_into('!H',ip,2,43);ip[9]=6
    ip[12:16]=ipaddress.ip_address(connection['remote_address']).packed
    ip[16:20]=ipaddress.ip_address(connection['local_address']).packed
    tcp=bytearray(20);struct.pack_into('!HH',tcp,0,16000,54321);tcp[12]=0x50
    packet=b'\0'*12+b'\x08\x00'+ip+tcp+b'abc'
    class FakeAPI:
        def __init__(self):self.done=False;self.closed=False
        def devices(self):return [dict(name='synthetic',description='selftest only',flags=UP)]
        def open(self,name,bpf):return 1
        def datalink(self,handle):return 1
        def next_packet(self,handle):
            if self.done:return None
            self.done=True;return int(time.time()),123,packet,len(packet)
        def close(self,handle):self.closed=True
    api=FakeAPI();capture=PacketCapture(base_dir,api_factory=lambda:api,connection_provider=lambda pid:[connection])
    capture.start(1234,seconds=1)
    capture.thread.join(3)
    assert not capture.status()['running'] and capture.status()['packets']==1 and api.closed
    path=Path(base_dir)/'selftest-packets.zip';capture.export(path)
    with zipfile.ZipFile(path) as z:
        metadata=json.loads(z.read('metadata.json'))
        assert metadata['game_messages_decoded'] is False and metadata['coordinates_decoded'] is False
        data=z.read('adapter-00.pcap')
        assert struct.unpack_from('<I',data)[0]==0xa1b2c3d4
        assert struct.unpack_from('<IIII',data,24)[2:]==(len(packet),len(packet))
        assert data[40:]==packet
    return path
