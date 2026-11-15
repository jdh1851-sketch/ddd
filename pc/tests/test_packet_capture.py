"""Offline tests for bounded read-only packet capture; no Npcap or game required."""
import ast
from collections import deque
import io
import json
from pathlib import Path, PurePosixPath
import socket
import struct
import sys
from threading import Event
import time
import zipfile

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packet_capture import (PacketCapture, NpcapAPI, parse_netstat, bpf_for_connections,
                            packet_endpoints, write_pcap_header, write_pcap_record)

PID=4521
CONNECTION=dict(local_address='192.0.2.4',local_port=50123,
                remote_address='203.0.113.9',remote_port=16000)


def ethernet_tcp(source='192.0.2.4',destination='203.0.113.9',sport=50123,dport=16000,payload=b'game sample',vlan=False):
    tcp=struct.pack('!HHIIBBHHH',sport,dport,1,0,5<<4,0x18,65535,0,0)+payload
    ipv4=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(tcp),1,0,64,6,0,
                     socket.inet_aton(source),socket.inet_aton(destination))+tcp
    ethernet=b'\x00'*12+(b'\x81\x00\x00\x01\x08\x00' if vlan else b'\x08\x00')
    return ethernet+ipv4


def wait_until(predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.005)
    pytest.fail('capture worker did not reach expected state')


class FakeAPI:
    def __init__(self,packets=(),fail_next=False,devices=None):
        self.pending=deque(packets);self.filters=[];self.closed=[];self.opened=[]
        self.fail_next=fail_next;self.entered=Event()
        self._devices=devices if devices is not None else [dict(name='fake-npcap',description='test capture device',flags=2)]

    def devices(self):return self._devices
    def open(self,name,bpf):
        self.opened.append(name);self.filters.append(bpf);return name
    def datalink(self,handle):return 1
    def next_packet(self,handle):
        self.entered.set()
        if self.fail_next:raise OSError('simulated Npcap read failure')
        if not self.pending:return None
        packet=self.pending.popleft();seconds=time.time();sec=int(seconds)
        return sec,int((seconds-sec)*1000000),packet,len(packet)
    def close(self,handle):self.closed.append(handle)
    def stats(self,handle):return {'received':len(self.opened),'dropped':0}


def capture_for(tmp_path,api,provider=None):
    return PacketCapture(tmp_path,api_factory=lambda:api,
                         connection_provider=provider or (lambda pid:[CONNECTION.copy()]))


def pcap_records(data):
    assert len(data)>=24
    magic,major,minor,zone,sigfigs,snaplen,link=struct.unpack('<IHHIIII',data[:24])
    assert magic==0xa1b2c3d4 and (major,minor)==(2,4)
    offset=24;records=[]
    while offset<len(data):
        assert offset+16<=len(data)
        sec,usec,included,original=struct.unpack('<IIII',data[offset:offset+16]);offset+=16
        assert offset+included<=len(data)
        records.append((sec,usec,data[offset:offset+included],original));offset+=included
    return link,records


def test_netstat_and_bpf_restrict_pid_and_established_16000_tuples():
    text='''Active Connections
  Proto  Local Address          Foreign Address        State           PID
  TCP    192.0.2.4:50123         203.0.113.9:16000       ESTABLISHED     4521
  TCP    [2001:db8::4]:50124     [2001:db8::9]:16000     ESTABLISHED     4521
  TCP    192.0.2.4:50125         203.0.113.9:16000       ESTABLISHED     9999
  TCP    192.0.2.4:50126         203.0.113.9:443         ESTABLISHED     4521
  TCP    0.0.0.0:16000           0.0.0.0:0              LISTENING       4521
  TCP    192.0.2.4:50127         203.0.113.9:16000       TIME_WAIT       4521
  UDP    0.0.0.0:16000           *:*                                    4521
'''
    rows=parse_netstat(text,PID)
    assert len(rows)==2 and CONNECTION in rows
    assert any(row['remote_address']=='2001:db8::9' for row in rows)
    expression=bpf_for_connections(rows)
    assert '192.0.2.4' in expression and '203.0.113.9' in expression
    assert '50123' in expression and '16000' in expression
    assert '2001:db8::4' in expression and '2001:db8::9' in expression
    assert '50126' not in expression and '443' not in expression


def test_packet_endpoints_and_pcap_records_preserve_only_real_packet_bytes():
    raw=ethernet_tcp()
    assert packet_endpoints(raw,1)==('192.0.2.4',50123,'203.0.113.9',16000)
    assert packet_endpoints(ethernet_tcp(vlan=True),1)==('192.0.2.4',50123,'203.0.113.9',16000)
    assert packet_endpoints(raw[:30],1) is None
    stream=io.BytesIO();write_pcap_header(stream,1)
    write_pcap_record(stream,123,456,raw,len(raw))
    link,records=pcap_records(stream.getvalue())
    assert link==1 and records==[(123,456,raw,len(raw))]


def test_capture_filters_both_directions_and_exports_only_fixed_diagnostics(tmp_path):
    outbound=ethernet_tcp()
    inbound=ethernet_tcp('203.0.113.9','192.0.2.4',16000,50123,b'reply')
    unrelated=ethernet_tcp('192.0.2.4','203.0.113.9',50123,443,b'not game traffic')
    api=FakeAPI([outbound,unrelated,inbound]);capture=capture_for(tmp_path,api)
    capture.start(PID,seconds=.4,max_bytes=4096)
    wait_until(lambda:not capture.status()['running'])
    state=capture.status()
    assert state['error'] in (None,'') and state['packets']==2
    assert state['bytes']==len(outbound)+len(inbound)
    assert len(api.closed)==len(api.opened)==1
    directory=Path(state['session_dir'])
    (directory/'private-settings.json').write_text('not diagnostic')
    destination=tmp_path/'capture.zip'
    assert capture.export(destination)==destination
    with zipfile.ZipFile(destination) as archive:
        assert 'private-settings.json' not in archive.namelist()
        assert all(not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts for name in archive.namelist())
        payloads=[]
        for name in archive.namelist():
            if name.endswith('.pcap'):
                _,records=pcap_records(archive.read(name));payloads.extend(row[2] for row in records)
        assert payloads==[outbound,inbound]


def test_capture_total_file_size_is_bounded_including_pcap_headers(tmp_path):
    packet=ethernet_tcp(payload=b'x'*32)
    budget=24+16+len(packet)
    api=FakeAPI([packet]*20);capture=capture_for(tmp_path,api)
    capture.start(PID,seconds=.4,max_bytes=budget)
    wait_until(lambda:not capture.status()['running'])
    state=capture.status()
    assert state['file_bytes']<=budget and state['packets']==1
    assert sum(p.stat().st_size for p in Path(state['session_dir']).glob('*.pcap'))<=budget
    assert api.closed==api.opened


def test_stop_is_nonblocking_and_running_export_is_rejected(tmp_path):
    api=FakeAPI();capture=capture_for(tmp_path,api)
    capture.start(PID,seconds=3,max_bytes=4096)
    assert api.entered.wait(1)
    with pytest.raises((ValueError,RuntimeError)):capture.export(tmp_path/'running.zip')
    started=time.monotonic();capture.stop();capture.stop()
    assert time.monotonic()-started<.15
    wait_until(lambda:not capture.status()['running'])
    assert api.closed==api.opened
    assert not (tmp_path/'running.zip').exists()


def test_connection_change_stops_without_capturing_new_connection(tmp_path):
    calls=[]
    def provider(pid):
        calls.append(pid)
        if len(calls)==1:return [CONNECTION.copy()]
        changed=CONNECTION.copy();changed['remote_address']='203.0.113.10';return [changed]
    api=FakeAPI();capture=capture_for(tmp_path,api,provider)
    capture.start(PID,seconds=5,max_bytes=4096)
    wait_until(lambda:not capture.status()['running'],timeout=3.5)
    assert capture.status()['reason'] in ('connection_changed','disconnected')
    assert calls and set(calls)=={PID}
    assert api.closed==api.opened


def test_capture_runtime_error_and_missing_devices_close_handles(tmp_path):
    api=FakeAPI(fail_next=True);capture=capture_for(tmp_path/'error',api)
    capture.start(PID,seconds=.4,max_bytes=4096)
    wait_until(lambda:not capture.status()['running'])
    assert 'simulated Npcap read failure' in str(capture.status()['error'])
    assert api.closed==api.opened
    empty=FakeAPI(devices=[]);capture=capture_for(tmp_path/'empty',empty)
    capture.start(PID,seconds=.4,max_bytes=4096)
    wait_until(lambda:not capture.status()['running'])
    assert capture.status()['error'] or capture.status()['reason']
    assert not empty.opened


def test_packet_capture_never_injects_packets_or_game_input():
    source=(Path(__file__).parents[1]/'packet_capture.py').read_text(encoding='utf-8')
    tree=ast.parse(source)
    forbidden={'pcap_sendpacket','pcap_inject','sendto','SendInput','keybd_event','mouse_event','SetCursorPos','WriteProcessMemory'}
    for node in ast.walk(tree):
        if isinstance(node,ast.Attribute):assert node.attr not in forbidden
        if isinstance(node,ast.Name):assert node.id not in forbidden


def test_ipv6_loopback_and_fragment_headers_cannot_bypass_tuple_scope():
    payload=b'ipv6 game data'
    tcp=struct.pack('!HHIIBBHHH',50124,16000,1,0,5<<4,0x18,65535,0,0)+payload
    ipv6=struct.pack('!IHBB16s16s',6<<28,len(tcp),6,64,
                     socket.inet_pton(socket.AF_INET6,'2001:db8::4'),
                     socket.inet_pton(socket.AF_INET6,'2001:db8::9'))+tcp
    assert packet_endpoints(b'\x00'*12+b'\x86\xdd'+ipv6,1)==('2001:db8::4',50124,'2001:db8::9',16000)
    ipv4=ethernet_tcp()[14:]
    assert packet_endpoints(struct.pack('<I',2)+ipv4,0)==('192.0.2.4',50123,'203.0.113.9',16000)
    assert packet_endpoints(struct.pack('>I',2)+ipv4,108)==('192.0.2.4',50123,'203.0.113.9',16000)
    assert packet_endpoints(ipv4,101)==('192.0.2.4',50123,'203.0.113.9',16000)
    fragment=bytearray(ethernet_tcp());fragment[20:22]=struct.pack('!H',1)
    assert packet_endpoints(bytes(fragment),1) is None
    assert packet_endpoints(ethernet_tcp(),9999) is None


@pytest.mark.parametrize('kwargs',[dict(pid=0),dict(seconds=0),dict(seconds=61),
                                   dict(max_bytes=23),dict(max_bytes=12*1024*1024+1)])
def test_invalid_bounds_never_open_a_capture_device(tmp_path,kwargs):
    api=FakeAPI();capture=capture_for(tmp_path,api)
    options=dict(pid=PID,seconds=.4,max_bytes=4096);options.update(kwargs)
    with pytest.raises((ValueError,TypeError)):capture.start(**options)
    assert not api.opened


def test_atomic_export_failure_preserves_completed_capture(tmp_path):
    api=FakeAPI([ethernet_tcp()]);capture=capture_for(tmp_path,api)
    capture.start(PID,seconds=.4,max_bytes=4096)
    wait_until(lambda:not capture.status()['running'])
    directory=Path(capture.status()['session_dir'])
    pcaps={path.name:path.read_bytes() for path in directory.glob('*.pcap')}
    target=tmp_path/'existing-directory.zip';target.mkdir()
    sentinel=target/'keep.txt';sentinel.write_text('keep this directory')
    with pytest.raises((OSError,ValueError)):capture.export(target)
    assert sentinel.read_text()=='keep this directory'
    assert {path.name:path.read_bytes() for path in directory.glob('*.pcap')}==pcaps
    assert not list(tmp_path.glob('existing-directory.zip.*.tmp'))


@pytest.mark.parametrize('failure',[None,'compile','filter','nonblock'])
def test_native_open_is_nonpromiscuous_filtered_and_closes_on_setup_error(failure):
    class NativeSpy:
        def __init__(self):self.calls=[]
        def pcap_open_live(self,name,snaplen,promisc,timeout,error):
            self.calls.append(('open',name,snaplen,promisc));return 42
        def pcap_compile(self,handle,program,bpf,optimize,netmask):
            self.calls.append(('compile',bpf));return int(failure=='compile')
        def pcap_setfilter(self,handle,program):
            self.calls.append(('filter',handle));return int(failure=='filter')
        def pcap_setnonblock(self,handle,enabled,error):
            self.calls.append(('nonblock',enabled));return int(failure=='nonblock')
        def pcap_geterr(self,handle):return b'simulated setup failure'
        def pcap_close(self,handle):self.calls.append(('close',handle))
        def pcap_freecode(self,program):self.calls.append(('freecode',))
    spy=NativeSpy();api=NpcapAPI.__new__(NpcapAPI);api.lib=spy
    bpf=bpf_for_connections([CONNECTION])
    if failure:
        with pytest.raises(OSError):api.open('fake-adapter',bpf)
        assert ('close',42) in spy.calls
    else:
        assert api.open('fake-adapter',bpf)==42
        assert ('filter',42) in spy.calls and ('nonblock',1) in spy.calls
        assert not any(call[0]=='close' for call in spy.calls)
    assert spy.calls[0]==('open',b'fake-adapter',65535,0)
    assert ('compile',bpf.encode('ascii')) in spy.calls
    assert (('freecode',) in spy.calls)==(failure!='compile')
