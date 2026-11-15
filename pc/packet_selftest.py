"""Packaged synthetic capture, bounded decoder and evidence ZIP verification."""
from collections import deque
import io
import ipaddress
import json
from pathlib import Path
import struct
import time
import zipfile
import brotli
from PIL import Image
from packet_capture import PacketCapture
from packet_decode import analyze_archive
from packet_evidence import EvidenceRecorder


def run_inspection_selftest(base_dir, fixture):
    def frame(typ, body=b'', flag=0):return struct.pack('<IIB',typ,len(body),flag)+body
    boss=bytearray(1636)
    struct.pack_into('<Q',boss,0,888)
    struct.pack_into('<6I',boss,638,1791041054,324458883,948487208,1324307937,597598050,1369085966)
    boss[1100:1108]=bytes.fromhex('18271c28c3b6114d')
    struct.pack_into('<fff',boss,1108,0,163,3.1415927)
    payload=(frame(20132)+frame(170826,struct.pack('<Qfff',777,12.5,150,1))+
             frame(20051,brotli.compress(bytes(boss)),1)+frame(20932,b'invalid-brotli',1)+frame(20316))
    connection=dict(local_address='192.0.2.10',local_port=54321,remote_address='198.51.100.20',remote_port=16000)
    def packet(body,seq):
        tcp=struct.pack('!HHIIBBHHH',16000,54321,seq,0,0x50,0x18,65535,0,0)+body
        ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(tcp),0,0,64,6,0,
            ipaddress.ip_address(connection['remote_address']).packed,
            ipaddress.ip_address(connection['local_address']).packed)
        return b'\0'*12+b'\x08\x00'+ip+tcp
    class FakeAPI:
        def __init__(self):self.pending=deque([packet(payload[:17],100),packet(payload[17:],117)])
        def devices(self):return [dict(name='synthetic',description='selftest',flags=2)]
        def open(self,name,bpf):return 1
        def datalink(self,handle):return 1
        def next_packet(self,handle):
            if not self.pending:return None
            data=self.pending.popleft();epoch=time.time();seconds=int(epoch)
            return seconds,int((epoch-seconds)*1000000),data,len(data)
        def close(self,handle):pass
    capture=PacketCapture(base_dir,api_factory=FakeAPI,connection_provider=lambda pid:[connection])
    evidence=EvidenceRecorder(base_dir,source_provider=lambda:(fixture,{'source_kind':'synthetic-selftest'}))
    try:
        capture.start(1,seconds=1)
        evidence.start(Path(capture.status()['session_dir']))
        capture.thread.join(4);evidence.close()
        assert not capture.status()['running'] and not capture.status()['error']
        assert evidence.status()['captured']>=1
        path=Path(base_dir)/'selftest-inspection.zip';capture.export(path,evidence=evidence)
        result=analyze_archive(path)
        assert result['server_frames']==5 and result['counters']['position_records']==1
        assert result['positions'][0]['entity_id']==777 and result['positions'][0]['x']==12.5
        assert result['boss_candidates'][0]['entity_id']==888 and result['decode_errors']==1
        assert not any(result[key] for key in ('self_identity_confirmed','departure_signal_confirmed','success_confirmed','game_input_allowed'))
        with zipfile.ZipFile(path) as archive:
            manifest=json.loads(archive.read('evidence/manifest.json'))
            for name,record in manifest['images'].items():
                image=Image.open(io.BytesIO(archive.read('evidence/'+name)))
                assert image.size==(fixture.shape[1],fixture.shape[0])
                assert record['capture_started_epoch']<=record['captured_epoch']<=record['capture_finished_epoch']
        return path,result
    finally:
        capture.close();evidence.close()
