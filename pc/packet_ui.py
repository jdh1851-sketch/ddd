"""UI for bounded passive packet diagnostics. Does not send game input."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import time
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from packet_capture import PacketCapture
from packet_replay import inspect_archive
from packet_evidence import EvidenceRecorder
from packet_identity import IdentityTracker
from movement_evidence import MovementRecorder
from scan_async import LatestScan


REASONS={
    'not_started':'기록 전', 'starting':'연결 확인 중', 'capturing':'기록 중',
    'timeout':'60초 기록 완료', 'time_limit':'60초 기록 완료',
    'byte_limit':'파일 크기 제한에 도달', 'stopped':'사용자 정지',
    'connection_changed':'게임 연결 변경 감지', 'disconnected':'게임 연결 종료',
    'error':'수신 오류',
}


class PacketPanel:
    def __init__(self,parent,host):
        self.canvas=tk.Canvas(parent,highlightthickness=0,background='white')
        scroll=ttk.Scrollbar(parent,orient='vertical',command=self.canvas.yview)
        scroll.pack(side='right',fill='y');self.canvas.pack(side='left',fill='both',expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        body=ttk.Frame(self.canvas);window=self.canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda event:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda event:self.canvas.itemconfigure(window,width=event.width))
        parent=body
        self.host=host;self.capture=PacketCapture(host.home_dir);self.exporting=False
        self.evidence=None;self.replay=LatestScan();self.replay_result=None;self.last_decoded=None
        self.identity=None;self.movement=None;self.movement_error=None
        self.save_evidence=tk.BooleanVar(value=True)
        self.save_movement=tk.BooleanVar(value=True)
        self.analysis=tk.StringVar(value='내 캐릭터·현재 속성·이탈 신호·성공: 모두 미확인')
        self.report=tk.StringVar(value='수신 전용 · 게임 입력 차단')
        self.detail=tk.StringVar(value='기록 전 · 좌표·정답 해석은 아직 검증 전입니다.')
        self.saved=tk.StringVar(value='자동 전송 없음 · 저장한 ZIP을 채팅에 첨부해 주세요.')
        ttk.Label(parent,text='1번 방 투창 · 패킷 관찰·재생',font=('맑은 고딕',18,'bold')).pack(pady=10)
        ttk.Label(parent,text='모비레이 파일과 설정을 변경하지 않고 기존 Npcap으로 수신합니다.',wraplength=820).pack(pady=6)
        ttk.Label(parent,text='투창병 방에서 F8 → 평소처럼 기믹 수행 → 패킷 ZIP 저장\n'
            '최대 60초 / 패킷 12MiB · F8 시작·정지 / F9 즉시정지',
            wraplength=820,justify='left').pack(pady=8)
        buttons=ttk.Frame(parent);buttons.pack(pady=6)
        ttk.Button(buttons,text='기록 F8',command=host.toggle).pack(side='left',padx=5)
        ttk.Button(buttons,text='ZIP 재생',command=self.load_replay).pack(side='left',padx=5)
        ttk.Button(buttons,text='분석 JSON 저장',command=self.save_analysis).pack(side='left',padx=5)
        ttk.Button(buttons,text='패킷 ZIP 저장',command=self.export_log).pack(side='left',padx=5)
        ttk.Checkbutton(parent,text='기존 좌표 창도 함께 기록 · 이름표 포함 · 패킷과 화면 시각 대조용',variable=self.save_evidence).pack()
        ttk.Checkbutton(parent,text='이동 보정 자료 함께 기록 · 게임 안의 WASD·마우스 상태만',variable=self.save_movement).pack()
        ttk.Label(parent,textvariable=self.report,wraplength=820,font=('맑은 고딕',14,'bold')).pack(pady=6)
        ttk.Label(parent,textvariable=self.detail,wraplength=820,justify='left').pack(pady=4)
        ttk.Label(parent,textvariable=self.analysis,wraplength=820,justify='left').pack(pady=4)
        self.positions=ttk.Treeview(parent,columns=('entity','x','y','heading','age'),show='headings',height=4)
        for column,title,width in (('entity','개체 ID 후보',145),('x','X 후보',100),('y','Y 후보',100),
                                  ('heading','방향값 후보',115),('age','시각 / 상태',160)):
            self.positions.heading(column,text=title);self.positions.column(column,width=width,anchor='center')
        self.positions.pack(padx=16,pady=4,fill='x')
        ttk.Label(parent,textvariable=self.saved,wraplength=820).pack(pady=4)
        ttk.Label(parent,text='게임 프로세스의 실제 TCP 16000번 연결만 기록합니다.\n'
            '게임 연결이 바뀌면 정지하므로 방에 들어간 뒤 시작하세요.\n'
            'ZIP에는 해당 게임 통신의 원문과 주소·포트가 포함됩니다. 계정·개체 정보가 포함될 수 있습니다.',
            wraplength=820,justify='left',foreground='#52637c').pack(pady=6)

    def start(self):
        if self.exporting:raise ValueError('패킷 ZIP 저장이 끝난 뒤 시작하세요.')
        self.replay.reset();self.replay_result=None;self.last_decoded=None
        self.positions.delete(*self.positions.get_children())
        if self.evidence:self.evidence.close()
        if self.identity:self.identity.close()
        if self.movement:self.movement.close()
        handle=self.host.find_game()
        if not handle:raise ValueError('마비노기 모바일 게임 창을 찾지 못했습니다. 투창병 방에서 시작하세요.')
        pid=W.DWORD()
        self.host.user32.GetWindowThreadProcessId(handle,C.byref(pid))
        if not pid.value:raise ValueError('게임 프로세스를 확인하지 못했습니다.')
        self.identity=IdentityTracker()
        self.capture.start(pid=int(pid.value),seconds=60,max_bytes=12*1024*1024,
            position_observer=self.identity.add_position,reset_observer=self.identity.reset_stream,
            identity_provider=self.identity.snapshot)
        self.evidence=None
        if self.save_evidence.get():
            self.evidence=EvidenceRecorder(self.host.home_dir,on_frame=self.identity.observe_frame)
            self.evidence.start(Path(self.capture.status()['session_dir']))
        self.movement=None
        self.movement_error=None
        if self.save_movement.get():
            try:
                self.movement=MovementRecorder(self.host.home_dir)
                self.movement.start(Path(self.capture.status()['session_dir']),game_pid=int(pid.value))
            except (OSError,ValueError,RuntimeError) as exc:
                self.movement_error=str(exc)
        self.report.set('게임 연결 확인 중 · 게임 입력 차단')
        self.saved.set('이번 기록은 수신 데이터입니다. 정지 후 ZIP으로 저장해 주세요.')

    def refresh(self):
        if self.replay_result is not None:
            self.show_decoded(self.replay_result,historical=True)
            return self.capture.status()
        status=self.capture.status()
        reason=REASONS.get(status.get('reason'),status.get('reason') or '대기')
        self.report.set(f'{"기록 중" if status.get("running") else reason} · 게임 입력 차단')
        self.detail.set(f'수신 {status.get("packets",0):,}개 | 데이터 {status.get("bytes",0)/1024:.1f}KiB | '
            f'{status.get("elapsed",0):.1f}초'+
            ('\n오류: '+str(status['error']) if status.get('error') else ''))
        self.show_decoded(self.capture.decoded_snapshot(),historical=not status.get('running'))
        if self.evidence:
            evidence=self.evidence.status()
            self.detail.set(self.detail.get()+f'\n좌표 창 증거 {evidence.get("captured",0)}장 / 건너뜀 {evidence.get("skipped",0)}회 · '
                + (str(evidence.get('last_skip_reason')) if evidence.get('last_skip_reason') else
                   ('기록 중' if evidence.get('running') else '기록 정지')))
        if getattr(self,'movement',None):
            movement=self.movement.status()
            note=movement.get('error') or movement.get('stop_reason') or ('기록 중' if movement.get('running') else '기록 정지')
            self.detail.set(self.detail.get()+f'\n이동 보정 자료 {movement.get("active_rows",0)}행 · {note} · 보정 미검증')
        if getattr(self,'movement_error',None):
            self.detail.set(self.detail.get()+'\n이동 보정 자료 오류: '+self.movement_error)
        return status

    def show_decoded(self,result,historical=False):
        self.last_decoded=result
        sync={'waiting':'경계 확인 중','synced':'경계 확인됨','degraded':'일부 해석 중단'}.get(result.get('sync_state'),'미확인')
        prefix='기록 ZIP 재생 · 현재 게임 상태 아님' if self.replay_result is not None else ('정지 시점 기록' if historical else '실시간 수신')
        failed=result.get('inspector_error')
        bosses=[] if failed else result.get('boss_candidates',[])
        boss=' / '.join(str(row['entity_id']) for row in bosses) if bosses else '미확인'
        identity={} if failed else result.get('identity') or {}
        own=identity.get('self_entity_id') if identity.get('confirmed') else None
        own_text=f'{own} · 좌표 대조 {identity.get("match_count",0)}회' if own is not None else '미확인'
        spear={} if failed else result.get('spear',{})
        stages={'waiting':'패턴 대기','casting':'시전 관측','lock_candidate':'위치 고정 후보',
            'released':'투척 관측','success_observed':'적중·성공 근거 관측','unsupported_cast':'미지원 속성',
            'unverified_end':'종료 근거 부족','stale':'이전 상태 만료'}
        stage=stages.get(spear.get('stage'),'미확인')
        element=spear.get('element') or (spear.get('last_observed_element') if historical or spear.get('success_observed') else None)
        target=spear.get('target_shaman')
        target_text=(f'{target["entity_id"]} / X {target["x"]:.2f}, Y {target["y"]:.2f}'
                     if target else '미확인')
        mine=spear.get('target_is_self')
        cast_target='나 · 좌표 대조됨' if mine is True else ('다른 대상' if mine is False else '미확인')
        self.analysis.set(f'{prefix} | {sync} | 내 캐릭터: {own_text}\n'
            f'패턴: {element or "미확인"} / {stage} | 정답 주술사: {target_text}\n'
            f'투창 대상: {cast_target} | 이탈 신호: {"후보 관측·제어 미검증" if spear.get("departure_candidate") else "미확인"} | 자동이동 차단\n'
            f'투창병 후보: {boss}')
        if failed:self.analysis.set(self.analysis.get()+'\n해석 중단: '+str(failed))
        elif result.get('decode_errors'):
            self.analysis.set(self.analysis.get()+f'\n미해석 메시지 {result["decode_errors"]}개 · 확인된 형식만 후보로 표시')
        self.positions.delete(*self.positions.get_children())
        for row in ([] if failed else result.get('positions',[])[:20]):
            age=max(0,time.time()-row['timestamp'])
            if not historical and age>3:continue
            self.positions.insert('', 'end',values=(row['entity_id'],f'{row["x"]:.2f}',f'{row["y"]:.2f}',
                f'{row["heading"]:.3f}' if row.get('heading') is not None else '미확인',
                ('내 위치 · ' if row['entity_id']==own else '')+('기록 시점' if historical else f'{age:.1f}초 전')))

    def load_replay(self):
        if self.exporting:return
        self.host.stop('패킷 ZIP 재생으로 전환')
        path=filedialog.askopenfilename(title='저장한 패킷 ZIP 재생',filetypes=[('패킷 기록 ZIP','*.zip')])
        if not path:return
        self.replay.reset();self.replay_result=None;self.last_decoded=None
        self.replay_pathname=Path(path).name
        self.positions.delete(*self.positions.get_children())
        self.analysis.set('새 기록 분석 중 · 이전 후보 표시 제거됨')
        self.report.set('기록 ZIP 분석 중 · 게임 입력 차단')
        self.submit_replay(path,self.replay.generation)

    def submit_replay(self,path,generation):
        if generation!=self.replay.generation:return
        self.replay.poll(max_age=120)
        if self.replay.submit(inspect_archive,path):self.poll_replay(generation)
        else:self.host.root.after(80,lambda:self.submit_replay(path,generation))

    def poll_replay(self,generation):
        if generation!=self.replay.generation:return
        try:
            result=self.replay.poll(max_age=120)
            if result is not None:
                result['source_archive']=getattr(self,'replay_pathname',None)
                self.replay_result=result
                self.report.set('기록 ZIP 재생 완료 · 게임 입력 차단')
                self.detail.set('화면 좌표 대조와 불 패턴 관측 결과입니다. 이동 보정·다른 속성·이탈 제어는 검증 전입니다.')
                self.show_decoded(result,historical=True);return
            if self.replay.future is None:
                self.report.set('기록 ZIP 분석 시간 초과 · 결과 미표시');return
            self.host.root.after(80,lambda:self.poll_replay(generation))
        except Exception as exc:
            self.report.set('기록 ZIP 분석 실패: '+str(exc))

    def save_analysis(self):
        if not self.last_decoded:return
        path=filedialog.asksaveasfilename(title='패킷 분석 결과 저장',defaultextension='.json',
            initialfile='투창_패킷분석.json',filetypes=[('분석 결과','*.json')])
        if not path:return
        try:
            Path(path).write_text(json.dumps(self.last_decoded,ensure_ascii=False,indent=2),encoding='utf-8')
            self.saved.set('분석 JSON 저장 완료')
        except OSError as exc:messagebox.showerror('분석 저장 실패',str(exc))

    def step(self,now):
        status=self.refresh()
        if not status.get('running'):
            self.host.stop('패킷 기록 종료 · '+REASONS.get(status.get('reason'),str(status.get('reason') or '중지')))

    def stopped(self):
        self.replay.reset();self.replay_result=None
        was_running=self.capture.status().get('running')
        self.capture.stop()
        if self.evidence:self.evidence.stop()
        if getattr(self,'identity',None):self.identity.stop()
        if getattr(self,'movement',None):self.movement.stop()
        if was_running:
            self.report.set('수신 종료 처리 중 · 게임 입력 차단')
            self.host.root.after(80,self.refresh_stopped)
    def refresh_stopped(self):
        if self.refresh().get('running'):self.host.root.after(80,self.refresh_stopped)
    def close(self):
        self.capture.close();self.replay.close()
        if self.evidence:self.evidence.close()
        if self.identity:self.identity.close()
        if self.movement:self.movement.close()

    def export_log(self):
        if self.exporting:return
        self.exporting=True
        self.host.stop('패킷 ZIP 저장 · 수신 중단')
        self.export_deadline=time.monotonic()+10
        self.finish_export()

    def finish_export(self):
        status=self.refresh()
        if status.get('running') or (self.evidence and self.evidence.status().get('running')) or (self.movement and self.movement.status().get('running')):
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
            if self.evidence:self.evidence.close()
            if self.identity:self.identity.close()
            if self.movement:self.movement.close()
            self.capture.export(path,evidence=self.evidence if self.save_evidence.get() else None,
                movement=self.movement if self.save_movement.get() else None)
            self.saved.set('패킷 ZIP 저장 완료 · 이 채팅에 첨부해 주세요.')
            messagebox.showinfo('패킷 기록 저장 완료',f'{path}\n\n이 채팅에 ZIP을 첨부하면 패킷 구조를 분석할 수 있습니다.')
        except (OSError,ValueError,RuntimeError) as exc:
            self.saved.set('ZIP 저장 실패: '+str(exc))
            messagebox.showerror('패킷 기록 저장 실패',str(exc))
        finally:self.exporting=False
