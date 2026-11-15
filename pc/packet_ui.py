"""UI for bounded passive packet diagnostics. Does not send game input."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from packet_capture import PacketCapture


REASONS={
    'not_started':'기록 전', 'starting':'연결 확인 중', 'capturing':'기록 중',
    'timeout':'60초 기록 완료', 'time_limit':'60초 기록 완료',
    'byte_limit':'파일 크기 제한에 도달', 'stopped':'사용자 정지',
    'connection_changed':'게임 연결 변경 감지', 'disconnected':'게임 연결 종료',
    'error':'수신 오류',
}


class PacketPanel:
    def __init__(self,parent,host):
        self.host=host;self.capture=PacketCapture(host.home_dir);self.exporting=False
        self.report=tk.StringVar(value='수신 전용 · 게임 입력 차단')
        self.detail=tk.StringVar(value='기록 전 · 좌표·정답 해석은 아직 검증 전입니다.')
        self.saved=tk.StringVar(value='자동 전송 없음 · 저장한 ZIP을 채팅에 첨부해 주세요.')
        ttk.Label(parent,text='1번 방 투창 · 패킷 기록',font=('맑은 고딕',18,'bold')).pack(pady=16)
        ttk.Label(parent,text='모비레이 파일과 설정을 변경하지 않고 기존 Npcap으로 수신합니다.',wraplength=820).pack(pady=6)
        ttk.Label(parent,text='1. 게임의 투창병 방에 들어갑니다.\n'
            '2. F8로 기록을 시작한 뒤 기믹을 평소처럼 수행합니다.\n'
            '3. 정지 후 ‘패킷 ZIP 저장’을 눌러 이 채팅에 첨부합니다.',
            wraplength=820,justify='left').pack(pady=16)
        ttk.Label(parent,text='최대 60초 / 12MiB · F8 시작·정지 / F9 즉시정지',wraplength=820).pack(pady=8)
        buttons=ttk.Frame(parent);buttons.pack(pady=12)
        ttk.Button(buttons,text='패킷 기록 F8',command=host.toggle).pack(side='left',padx=8)
        ttk.Button(buttons,text='패킷 ZIP 저장',command=self.export_log).pack(side='left',padx=8)
        ttk.Label(parent,textvariable=self.report,wraplength=820,font=('맑은 고딕',14,'bold')).pack(pady=12)
        ttk.Label(parent,textvariable=self.detail,wraplength=820,justify='left').pack(pady=8)
        ttk.Label(parent,textvariable=self.saved,wraplength=820).pack(pady=8)
        ttk.Label(parent,text='게임 프로세스의 실제 TCP 16000번 연결만 기록합니다.\n'
            '게임 연결이 바뀌면 정지하므로 방에 들어간 뒤 시작하세요.\n'
            'ZIP에는 해당 게임 통신의 원문과 주소·포트가 포함됩니다. 계정·개체 정보가 포함될 수 있습니다.',
            wraplength=820,justify='left',foreground='#52637c').pack(pady=12)

    def start(self):
        if self.exporting:raise ValueError('패킷 ZIP 저장이 끝난 뒤 시작하세요.')
        handle=self.host.find_game()
        if not handle:raise ValueError('마비노기 모바일 게임 창을 찾지 못했습니다. 투창병 방에서 시작하세요.')
        pid=W.DWORD()
        self.host.user32.GetWindowThreadProcessId(handle,C.byref(pid))
        if not pid.value:raise ValueError('게임 프로세스를 확인하지 못했습니다.')
        self.capture.start(pid=int(pid.value),seconds=60,max_bytes=12*1024*1024)
        self.report.set('게임 연결 확인 중 · 게임 입력 차단')
        self.saved.set('이번 기록은 수신 데이터입니다. 정지 후 ZIP으로 저장해 주세요.')

    def refresh(self):
        status=self.capture.status()
        reason=REASONS.get(status.get('reason'),status.get('reason') or '대기')
        self.report.set(f'{"기록 중" if status.get("running") else reason} · 게임 입력 차단')
        self.detail.set(f'수신 {status.get("packets",0):,}개 | 데이터 {status.get("bytes",0)/1024:.1f}KiB | '
            f'{status.get("elapsed",0):.1f}초\n좌표·대상·정답 해석: 아직 검증 전'+
            ('\n오류: '+str(status['error']) if status.get('error') else ''))
        return status

    def step(self,now):
        status=self.refresh()
        if not status.get('running'):
            self.host.stop('패킷 기록 종료 · '+REASONS.get(status.get('reason'),str(status.get('reason') or '중지')))

    def stopped(self):
        was_running=self.capture.status().get('running')
        self.capture.stop()
        if was_running:
            self.report.set('수신 종료 처리 중 · 게임 입력 차단')
            self.host.root.after(80,self.refresh_stopped)
    def refresh_stopped(self):
        if self.refresh().get('running'):self.host.root.after(80,self.refresh_stopped)
    def close(self):self.capture.close()

    def export_log(self):
        if self.exporting:return
        self.exporting=True
        self.host.stop('패킷 ZIP 저장 · 수신 중단')
        self.export_deadline=time.monotonic()+10
        self.finish_export()

    def finish_export(self):
        status=self.refresh()
        if status.get('running'):
            if time.monotonic()<self.export_deadline:
                self.saved.set('수신 종료를 기다리는 중입니다.')
                self.host.root.after(80,self.finish_export);return
            self.saved.set('수신 종료 처리 중입니다. 잠시 후 ZIP 저장을 다시 눌러주세요.')
            self.exporting=False;return
        if not status.get('session_dir'):
            self.saved.set('F8로 한 번 기록한 뒤 ZIP을 저장하세요.')
            self.exporting=False;return
        name=Path(status['session_dir']).name
        path=filedialog.asksaveasfilename(title='패킷 분석용 ZIP 저장',defaultextension='.zip',
            initialfile=f'투창_패킷기록_{name}.zip',filetypes=[('패킷 기록','*.zip')])
        if not path:
            self.exporting=False;return
        try:
            self.capture.export(path)
            self.saved.set('패킷 ZIP 저장 완료 · 이 채팅에 첨부해 주세요.')
            messagebox.showinfo('패킷 기록 저장 완료',f'{path}\n\n이 채팅에 ZIP을 첨부하면 패킷 구조를 분석할 수 있습니다.')
        except (OSError,ValueError,RuntimeError) as exc:
            self.saved.set('ZIP 저장 실패: '+str(exc))
            messagebox.showerror('패킷 기록 저장 실패',str(exc))
        finally:self.exporting=False
