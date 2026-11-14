"""Read-only observation of the existing EntityMap window. No game input."""
import ctypes as C
from ctypes import wintypes as W
import time
import tkinter as tk
from tkinter import ttk, filedialog
import cv2
import numpy as np
from PIL import Image, ImageGrab, ImageTk
from scan_async import LatestScan
from spear_observation import NativeMapReader


class MinimapPanel:
    def __init__(self, parent, host):
        self.host=host;self.worker=LatestScan();self.reader=NativeMapReader()
        self.source=None;self.next_scan=0;self.size=None;self.last_frame=0
        self.image_generation=0;self.pending_image=None;self.displayed_at=None
        self.report=tk.StringVar(value='관찰 전용 · 게임 입력 차단')
        self.detail=tk.StringVar(value='기존 모비레이 좌표 창을 읽습니다. 원본 창과 캡처 이미지를 사용할 수 있습니다.')
        ttk.Label(parent,text='1번 방 투창 · 원본 창 관찰',font=('맑은 고딕',18,'bold')).pack(pady=12)
        ttk.Label(parent,text='정답 읽기 시험 · 이동/공격/성공 판정 미연결').pack()
        buttons=ttk.Frame(parent);buttons.pack(pady=8)
        ttk.Button(buttons,text='원본 PNG 열기',command=self.load).pack(side='left',padx=5)
        ttk.Button(buttons,text='기존 좌표 창 관찰 F8',command=self.host.toggle).pack(side='left',padx=5)
        ttk.Label(parent,textvariable=self.report,wraplength=850).pack(pady=8)
        ttk.Label(parent,textvariable=self.detail,wraplength=850,justify='left').pack()
        self.preview=ttk.Label(parent,text='읽은 영역을 기존 화면 위에 표시합니다.');self.preview.pack(pady=10)

    def clear_display(self,message='이전 인식 표시 제거됨'):
        self.displayed_at=None
        self.preview.configure(image='',text=message)
        self.report.set('현재 좌표·목표: 미확인 · 게임 입력 차단')
        self.detail.set('새 프레임에서 다시 확인해야 합니다. 기믹 성공은 판정하지 않습니다.')

    def invalidate(self):
        self.image_generation+=1;self.pending_image=None;self.worker.reset()
        self.clear_display()
        # Reader caches are worker-owned; replace only after a previous task drains.
        self.reset_reader=True

    def stopped(self):self.invalidate()
    def close(self):self.worker.close()

    def find_window(self):
        u=self.host.user32;found=[]
        @C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        def callback(handle,_):
            title=C.create_unicode_buffer(u.GetWindowTextLengthW(handle)+1)
            u.GetWindowTextW(handle,title,len(title))
            if title.value.strip()=='좌표 미니맵' and u.IsWindowVisible(handle):found.append(handle)
            return True
        u.EnumWindows(callback,0)
        if len(found)!=1:raise ValueError('모비레이의 ‘좌표 미니맵’ 창 하나를 열어주세요.')
        return found[0]

    def start(self):
        self.invalidate();self.source=self.find_window();self.size=None
        self.last_frame=time.monotonic();self.next_scan=0

    def analyze(self,rgb,captured_at,reset_reader=False):
        if reset_reader:self.reader=NativeMapReader()
        return rgb,self.reader.read(rgb),captured_at

    def submit(self,rgb,captured_at):
        reset=getattr(self,'reset_reader',False)
        if self.worker.submit(self.analyze,rgb,captured_at,reset):
            self.reset_reader=False;return True
        return False

    def load(self):
        self.host.stop('이미지 관찰로 전환')
        path=filedialog.askopenfilename(filetypes=[('원본 캡처','*.png *.jpg *.jpeg *.bmp')])
        if not path:return
        try:
            with Image.open(path) as im:self.pending_image=np.array(im.convert('RGB'))
            self.source=None;self.poll_image(self.image_generation)
        except Exception as exc:self.report.set('이미지 오류: '+str(exc))

    def poll_image(self,generation):
        if generation!=self.image_generation:return
        try:
            result=self.worker.poll(max_age=10)
            if result is not None:self.render(*result,live=False);return
            if self.pending_image is not None:
                if self.submit(self.pending_image,time.monotonic()):self.pending_image=None
            elif self.worker.future is None:
                self.clear_display('분석 시간 초과 · 원본 크기를 확인하세요.');return
            self.host.root.after(80,lambda:self.poll_image(generation))
        except Exception as exc:
            self.invalidate();self.report.set('분석 오류: '+str(exc))

    def step(self,now):
        u=self.host.user32
        if not self.source or not u.IsWindow(self.source) or u.IsIconic(self.source):
            self.host.stop('좌표 창 닫힘/최소화 · 관찰 중단');return
        x,y,w,h=self.host.window_rect(self.source)
        if w<100 or h<100:self.host.stop('좌표 창 크기를 확인하세요.');return
        if self.size!=(x,y,w,h):
            self.invalidate();self.size=(x,y,w,h);self.last_frame=now
        for fx,fy in ((.15,.15),(.5,.5),(.85,.85),(.15,.85),(.85,.15)):
            if u.GetAncestor(u.WindowFromPoint(W.POINT(x+int(w*fx),y+int(h*fy))),2)!=self.source:
                self.host.stop('다른 창이 좌표 창을 가림 · 관찰 중단');return
        result=self.worker.poll(max_age=8)
        if result is not None:
            self.last_frame=now;self.render(*result,live=True)
        # Do not leave old targets on screen while a slow OCR task continues.
        if self.displayed_at is not None and now-self.displayed_at>3:
            self.clear_display('새 프레임 읽는 중 · 이전 목표 만료')
        if now-self.last_frame>10:self.host.stop('새 인식 결과 없음 · 관찰 중단');return
        if now>=self.next_scan and self.worker.future is None:
            rgb=np.array(ImageGrab.grab(bbox=(x,y,x+w,y+h),all_screens=True))
            self.submit(rgb,time.monotonic());self.next_scan=now+.5

    def render(self,rgb,result,captured_at,live):
        now=time.monotonic();age=now-captured_at
        if live and age>3:
            self.clear_display('오래된 인식 결과 폐기 · 원본 창을 확인하세요.');return
        frame=rgb.copy()
        for region in result['coordinate_regions']:
            x,y,x2,y2=region['box'];cv2.rectangle(frame,(x,y),(x2,y2),(255,170,30),2)
        for key in ('boss','answer'):
            if result[key]:
                x,y,x2,y2=result[key]['box'];cv2.rectangle(frame,(x,y),(x2,y2),(20,220,240),2)
        if result['star']:
            x,y=result['star']['pixel'];cv2.circle(frame,(round(x),round(y)),9,(255,170,30),2)
        element=result['element'];coords=result['coordinates']
        evidence='두 문자 분할 결과 일치' if element else '속성 일치 미확인'
        self.report.set(f'패턴: {element or "미확인"} | 인식 근거: {evidence} | 게임 입력 차단')
        coord_text=f'X {coords[0]:.2f}, Y {coords[1]:.2f}' if coords is not None else '미확인'
        star=result['star']['pixel'] if result['star'] else '미확인'
        timing=f'캡처 후 {age:.1f}초 / 읽기 {result["processing_seconds"]:.1f}초' if live else '정지 이미지 결과 · 실시간 검증 아님'
        self.detail.set(f'내 좌표: {coord_text}\n정답 별 후보(화면 픽셀): {star}\n{timing}'+
                        (' · 시간 제한으로 일부만 읽음' if result['partial'] else '')+
                        '\n내 투창 대상 여부·정답 게임 좌표·도착·성공 판정: 미검증')
        im=Image.fromarray(frame);im.thumbnail((850,430));self.photo=ImageTk.PhotoImage(im)
        self.preview.configure(image=self.photo,text='');self.displayed_at=captured_at if live else None
