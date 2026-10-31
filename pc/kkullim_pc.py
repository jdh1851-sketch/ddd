import ctypes as C
from ctypes import wintypes as W
import sys, time, json, pathlib, logging
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import ImageGrab, Image, ImageTk

BASE = pathlib.Path(getattr(sys, '_MEIPASS', pathlib.Path(__file__).parent))
HOME = pathlib.Path(sys.executable).parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).parent
u = C.windll.user32
try: C.windll.shcore.SetProcessDpiAwareness(2)
except Exception: u.SetProcessDPIAware()
u.GetForegroundWindow.restype = W.HWND
u.GetAncestor.argtypes = [W.HWND, W.UINT]
u.GetAncestor.restype = W.HWND
u.WindowFromPoint.argtypes = [W.POINT]
u.WindowFromPoint.restype = W.HWND
logging.basicConfig(filename=str(HOME / 'abyss.log'), level=logging.INFO, encoding='utf-8', format='%(asctime)s %(message)s')

NAMES = {'enter':'입장하기', 'compass':'나침반', 'skip':'장면 넘기기', 'touch':'화면 터치', 'retry':'다시 하기', 'food':'음식 B', 'break':'브레이크', 'partyready':'준비 완료', 'partycancel':'준비 취소', 'challenge':'도전하기', 'challenge_title':'도전 확인창', 'hud':'게임 조작 화면'}
NAMES.update({'other_dungeon':'다른 던전 가기','dungeon_dock':'허상의 정박지','dungeon_cave':'광기의 동굴','dungeon_water':'흩어진 물길'})
DUNGEONS={'허상의 정박지':'dungeon_dock','광기의 동굴':'dungeon_cave','흩어진 물길':'dungeon_water'}
ALLOWED = {'ready':['challenge','partyready','enter','compass'], 'battle':['skip','touch'], 'result':['retry'], 'queue':['challenge','partyready','compass','enter']}
STAGE_LABELS={'search':'현재 화면 확인 중','ready':'입장 화면','queue':'매칭·입장 준비','battle':'게임 진행·연출','result':'클리어·결과 화면'}
STAGE_LABELS['dungeon_list']='어비스 던전 목록'

def dungeon_list_visible(valid):
    return sum(k in valid for k in DUNGEONS.values())>=2

def screen_stage(valid, previous='search'):
    """Recover from observable screens, independently of earlier input history."""
    if 'partycancel' in valid:return 'queue'
    if 'challenge_title' in valid or 'partyready' in valid:return 'queue'
    if dungeon_list_visible(valid):return 'dungeon_list'
    if 'retry' in valid or 'other_dungeon' in valid or 'touch' in valid:return 'result'
    if 'skip' in valid or 'break' in valid:return 'battle'
    if 'enter' in valid:return 'ready'
    if 'compass' in valid:return 'queue'
    if 'hud' in valid:return 'battle'
    return previous

def screen_action(valid,party_mode='1인',target='허상의 정박지',party_role='파티장'):
    if 'partycancel' in valid:return None
    if 'challenge_title' in valid:return 'challenge' if 'challenge' in valid else None
    if 'partyready' in valid:return 'partyready'
    if dungeon_list_visible(valid):
        key=DUNGEONS.get(target)
        return key if party_mode=='2~3인' and key in valid else None
    if 'retry' in valid or 'other_dungeon' in valid:
        if party_mode=='4인' and party_role=='파티원':return None
        key='other_dungeon' if party_mode=='2~3인' else 'retry'
        return key if key in valid else None
    action=next((k for k in ('touch','skip','enter','compass') if k in valid),None)
    if party_mode=='4인' and party_role=='파티원' and action=='enter':return None
    return action

def game_window():
    found=[]
    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, p):
        n=u.GetWindowTextLengthW(h); b=C.create_unicode_buffer(n+1); u.GetWindowTextW(h,b,n+1)
        if b.value.strip() in ('마비노기 모바일','Mabinogi Mobile','MabinogiMobile') and u.IsWindowVisible(h): found.append(h)
        return True
    u.EnumWindows(cb,0)
    return found[0] if found else None

def rect(h):
    r=W.RECT(); u.GetClientRect(h,C.byref(r)); p=W.POINT(0,0); u.ClientToScreen(h,C.byref(p))
    return p.x,p.y,r.right,r.bottom

class Detector:
    def __init__(self):
        self.cfg=json.loads((BASE/'templates/config.json').read_text(encoding='utf-8'))
        self.templates={k:cv2.imdecode(np.fromfile(str(BASE/'templates'/f'{k}.png'),dtype=np.uint8),0) for k in self.cfg}
        self.variants={k:[v] for k,v in self.templates.items()}
        for k in ('touch','compass','break','other_dungeon'):
            self.variants[k]=[cv2.imdecode(np.fromfile(str(p),dtype=np.uint8),0) for p in sorted((BASE/'templates').glob(k+'*.png'))]
        v=cv2.threshold(self.templates['compass'],210,255,cv2.THRESH_BINARY)[1]
        hh,ww=v.shape
        self.variants['compass']=[cv2.warpAffine(v,cv2.getRotationMatrix2D((ww/2,hh/2),a,1),(ww,hh)) for a in range(0,360,15)]
        self.variants['break']=[cv2.threshold(v,210,255,cv2.THRESH_BINARY)[1] for v in self.variants['break']]
        self.cache_size=None;self.scaled={}
    def prepare(self,width,height):
        if self.cache_size==(width,height):return
        self.cache_size=(width,height);self.scaled={}
        aspect=(1660/1024)/(width/height)
        for k,variants in self.variants.items():
            scales=(.75,.85,.94,1.,1.06,1.15,1.3) if k=='break' else (.9,.95,1.,1.05,1.1)
            factors=[(s,s) for s in scales]
            if abs(aspect-1)>.025:factors += [(s*aspect,s) for s in (.9,1.,1.1)]
            prepared=[]
            for v in variants:
                for fx,fy in factors:
                    t=cv2.resize(v,None,fx=fx,fy=fy)
                    if width<1000 or height<600:
                        hh,ww=t.shape
                        tiny=cv2.resize(t,(max(3,round(ww*width/1660)),max(3,round(hh*height/1024))),interpolation=cv2.INTER_AREA)
                        t=cv2.resize(tiny,(ww,hh),interpolation=cv2.INTER_LINEAR)
                    prepared.append(t)
            self.scaled[k]=prepared
    def scan(self, rgb):
        source=np.array(rgb);self.prepare(source.shape[1],source.shape[0])
        frame=cv2.resize(source,(1660,1024))
        gray=cv2.cvtColor(frame,cv2.COLOR_RGB2GRAY)
        hits={}
        for k,cfg in self.cfg.items():
            x,y,w,h=cfg['roi']; patch=gray[y:y+h,x:x+w]
            if k in ('compass','break'):patch=cv2.threshold(patch,210,255,cv2.THRESH_BINARY)[1]
            score=-1; pos=None
            for t in self.scaled[k]:
                if t.shape[0]>h or t.shape[1]>w: continue
                _,v,_,loc=cv2.minMaxLoc(cv2.matchTemplate(patch,t,cv2.TM_CCOEFF_NORMED))
                if v>score: score=v;pos=(x+loc[0]+t.shape[1]/2,y+loc[1]+t.shape[0]/2)
            hits[k]=(score,pos)
        return frame,hits

class App:
    def __init__(self):
        self.root=tk.Tk();self.root.title('끌림 매크로 v0.12');self.root.iconbitmap(str(BASE/'app_icon.ico'))
        self.root.geometry(f'900x{min(1100,self.root.winfo_screenheight()-100)}');self.root.minsize(820,650)
        self.root.configure(bg='#f2f5fa');self.root.option_add('*Font',('맑은 고딕',12))
        style=ttk.Style();style.theme_use('clam')
        style.configure('.',font=('맑은 고딕',12),background='#ffffff',foreground='#24334a')
        style.configure('TFrame',background='#ffffff')
        style.configure('TLabel',background='#ffffff',foreground='#24334a')
        style.configure('TRadiobutton',font=('맑은 고딕',13),background='#ffffff',padding=6)
        style.configure('TCheckbutton',font=('맑은 고딕',13),background='#ffffff',padding=8)
        style.configure('TEntry',padding=6)
        style.configure('TButton',font=('맑은 고딕',12,'bold'),padding=(12,12))
        style.configure('Start.TButton',background='#2866d8',foreground='white')
        style.map('Start.TButton',background=[('active','#2056b9')])
        style.configure('Stop.TButton',background='#be384b',foreground='white')
        style.map('Stop.TButton',background=[('active','#a52d40')])
        self.det=Detector();self.running=False;self.state='search';self.count=0;self.pending=None;self.last_progress=time.monotonic();self.delay=0
        self.last_size=None;self.resize_until=0
        self.party_mode=tk.StringVar(value='1인');self.target_dungeon=tk.StringVar(value='허상의 정박지');self.party_role=tk.StringVar(value='파티장')
        self.result_counted=False;self.result_seen=0
        self.mode=tk.StringVar(value='observe');self.limit=tk.StringVar(value='1');self.threshold=tk.StringVar(value='0.84');self.timeout=tk.StringVar(value='15')
        self.status=tk.StringVar(value='중지됨 · 게임을 준비한 뒤 시작해 주세요.')
        # Keep stop/start accessible even when content must scroll on a small display.
        footer=tk.Frame(self.root,bg='#f2f5fa');footer.pack(side='bottom',fill='x',padx=22,pady=(6,14))
        actions=tk.Frame(footer,bg='#f2f5fa');actions.pack(fill='x')
        ttk.Button(actions,text='시작 / 일시정지  F8',style='Start.TButton',command=self.toggle).pack(side='left',fill='x',expand=True,padx=(0,8))
        ttk.Button(actions,text='즉시정지  F9',style='Stop.TButton',command=lambda:self.stop('사용자 정지')).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='회차 초기화',command=self.reset).pack(side='left')
        tk.Label(footer,textvariable=self.status,bg='#f2f5fa',fg='#52637c',anchor='w',justify='left',wraplength=810,font=('맑은 고딕',11)).pack(fill='x',pady=(9,0))
        canvas=tk.Canvas(self.root,bg='#f2f5fa',highlightthickness=0)
        scroll=ttk.Scrollbar(self.root,orient='vertical',command=canvas.yview);scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body=tk.Frame(canvas,bg='#f2f5fa');window=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        self.root.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-int(e.delta/120),'units'))
        tk.Label(body,text='끌림 매크로',font=('맑은 고딕',24,'bold'),bg='#f2f5fa',fg='#24334a').pack(anchor='w',padx=24,pady=(16,2))
        tk.Label(body,text='우연한 만남 · 파티 입장 및 재도전',font=('맑은 고딕',12),bg='#f2f5fa',fg='#64748b').pack(anchor='w',padx=26,pady=(0,14))
        banner=tk.Frame(body,bg='#172b4d',padx=18,pady=13);banner.pack(fill='x',padx=22)
        tk.Label(banner,text='F8  시작 / 일시정지       F9  즉시정지',font=('맑은 고딕',17,'bold'),bg='#172b4d',fg='white').pack(anchor='w')
        tk.Label(banner,text='게임 창이 맨 앞일 때만 작동합니다.',font=('맑은 고딕',13,'bold'),bg='#172b4d',fg='#dbeafe').pack(anchor='w',pady=(5,0))
        tk.Label(body,text='입장하기 · 준비 완료 · 도전하기 자동 처리  /  준비 취소는 대기',font=('맑은 고딕',12,'bold'),bg='#f2f5fa',fg='#36557d').pack(anchor='w',padx=26,pady=12)
        def card(title):
            f=tk.Frame(body,bg='white',highlightbackground='#dce3ee',highlightthickness=1,padx=16,pady=12);f.pack(fill='x',padx=22,pady=(0,12))
            tk.Label(f,text=title,font=('맑은 고딕',15,'bold'),bg='white',fg='#24334a').pack(anchor='w',pady=(0,8))
            return f
        modes=card('1   실행 모드 선택')
        row=ttk.Frame(modes);row.pack(fill='x')
        ttk.Radiobutton(row,text='인식만 확인 (클릭 없음)',variable=self.mode,value='observe').pack(side='left',padx=(0,20))
        ttk.Radiobutton(row,text='실제 실행',variable=self.mode,value='click').pack(side='left')
        tk.Label(modes,text='미리보기 · 인식된 대상에 사각형 표시',font=('맑은 고딕',11),bg='white',fg='#64748b').pack(anchor='w',pady=(8,6))
        self.preview=tk.Label(modes,text='시작하면 이곳에 게임 화면이 표시됩니다.',font=('맑은 고딕',12),bg='#eaf0f8',fg='#64748b',width=56,height=3);self.preview.pack(pady=(0,2))
        options=card('2   입력 및 선택 기능')
        self.inputmode=tk.StringVar(value='space')
        row=ttk.Frame(options);row.pack(fill='x')
        ttk.Label(row,text='기본 입력').pack(side='left',padx=(0,25))
        ttk.Radiobutton(row,text='Space 키',variable=self.inputmode,value='space').pack(side='left')
        ttk.Radiobutton(row,text='마우스',variable=self.inputmode,value='mouse').pack(side='left')
        row=ttk.Frame(options);row.pack(fill='x',pady=(8,4))
        ttk.Label(row,text='고정 파티 인원').pack(side='left',padx=(0,10))
        self.party_combo=ttk.Combobox(row,textvariable=self.party_mode,values=['1인','2~3인','4인'],state='readonly',width=7)
        self.party_combo.pack(side='left',padx=(0,16))
        ttk.Label(row,text='재입장 던전').pack(side='left',padx=(0,8))
        self.dungeon_combo=ttk.Combobox(row,textvariable=self.target_dungeon,values=list(DUNGEONS),state='readonly',width=15)
        self.dungeon_combo.pack(side='left')
        row=ttk.Frame(options);row.pack(fill='x',pady=(4,4))
        ttk.Label(row,text='4인 파티일 때 내 역할').pack(side='left',padx=(0,10))
        self.role_combo=ttk.Combobox(row,textvariable=self.party_role,values=['파티장','파티원'],state='readonly',width=8)
        self.role_combo.pack(side='left')
        tk.Label(options,text='2~3인: 다른 던전 가기 → 지정 던전 선택 → 입장\n1인·4인: 다시 하기 / 4인 파티원: 준비 완료',font=('맑은 고딕',11),bg='white',fg='#36557d',justify='left').pack(anchor='w',pady=(3,7))
        self.option_states=[]
        def clear_checkbox(label,var):
            row=tk.Frame(options,bg='white',cursor='hand2');row.pack(fill='x',pady=5)
            mark=tk.Canvas(row,width=32,height=34,bg='white',highlightthickness=0,takefocus=1);mark.pack(side='left',padx=(2,8))
            caption=tk.Label(row,text=label,font=('맑은 고딕',13),bg='white',fg='#24334a',anchor='w');caption.pack(side='left')
            state=tk.Label(row,font=('맑은 고딕',11,'bold'),padx=10,pady=5);state.pack(side='right',padx=(8,0))
            def refresh(*args):
                selected=var.get();mark.delete('all')
                mark.create_rectangle(3,5,27,29,fill='#2866d8' if selected else 'white',outline='#2866d8' if selected else '#9aa8bc',width=2)
                if selected:mark.create_line(8,17,13,23,23,11,fill='white',width=3,capstyle='round',joinstyle='round')
                state.configure(text='사용 중' if selected else '사용 안 함',fg='#245cc0' if selected else '#64748b',bg='#eaf1ff' if selected else '#f1f4f8')
            def toggle_option(event=None):
                var.set(not var.get());mark.focus_set();return 'break'
            for widget in (row,mark,caption,state):widget.bind('<Button-1>',toggle_option)
            mark.bind('<space>',toggle_option);mark.bind('<Return>',toggle_option)
            mark.bind('<FocusIn>',lambda e:mark.configure(highlightthickness=1,highlightbackground='#2866d8',highlightcolor='#2866d8'))
            mark.bind('<FocusOut>',lambda e:mark.configure(highlightthickness=0))
            var.trace_add('write',refresh);refresh();self.option_states.append((var,state))
        self.food=tk.BooleanVar(value=False);self.food_used=False;self.food_seen=0;self.food_absent=0;self.food_last=0
        clear_checkbox('음식 B 버튼이 나타나면 음식 먹기',self.food)
        self.break_skill=tk.BooleanVar(value=False);self.break_used=False;self.break_absent=0;self.break_last=0
        clear_checkbox('브레이크 / 익스텐드 감지 시 6번 스킬 사용',self.break_skill)
        settings=card('3   반복 설정')
        row=ttk.Frame(settings);row.pack(fill='x')
        ttk.Label(row,text='완료 횟수').pack(side='left');ttk.Entry(row,textvariable=self.limit,width=5).pack(side='left',padx=8)
        ttk.Label(row,text='0 = 무제한',foreground='#64748b').pack(side='left',padx=(0,22))
        ttk.Label(row,text='진행 없으면 정지').pack(side='left');ttk.Entry(row,textvariable=self.timeout,width=5).pack(side='left',padx=8);ttk.Label(row,text='분').pack(side='left')
        row=ttk.Frame(settings);row.pack(fill='x',pady=(10,0))
        ttk.Label(row,text='인식 기준').pack(side='left');ttk.Entry(row,textvariable=self.threshold,width=6).pack(side='left',padx=8)
        ttk.Label(row,text='기본 0.84 · 인식 확인은 첫 사용 시 권장',foreground='#64748b',font=('맑은 고딕',10)).pack(side='left')
        self.keys={};self.candidate=None;self.stable=0
        self.root.after(250,self.tick)
    def reset(self):
        if self.running:return
        self.state='search';self.count=0;self.pending=None;self.candidate=None;self.stable=0;self.food_used=False;self.result_counted=False;self.result_seen=0
        self.status.set('회차 초기화 완료. 현재 화면을 인식해 해당 단계부터 시작합니다.')
    def stop(self,msg):
        self.running=False;self.party_combo.configure(state='readonly');self.dungeon_combo.configure(state='readonly');self.role_combo.configure(state='readonly');self.status.set(msg);logging.info(msg)
    def toggle(self):
        if self.running:self.stop('일시정지');return
        try:
            self.maxruns=int(self.limit.get());self.th=float(self.threshold.get());self.wait=float(self.timeout.get())*60
            if self.maxruns<0 or not .6<=self.th<=.99 or self.wait<60:raise ValueError()
        except ValueError:messagebox.showerror('설정','횟수 0 이상, 인식 기준 0.60~0.99, 정지 시간 1분 이상을 입력하세요.');return
        self.hwnd=game_window()
        if not self.hwnd:messagebox.showerror('게임 창','마비노기 모바일 창을 찾지 못했습니다. 게임을 실행해 주세요.');return
        if self.pending:self.pending=(self.pending[0],time.monotonic())
        self.running=True;self.last_progress=time.monotonic();self.delay=time.monotonic()+3
        self.party_combo.configure(state='disabled');self.dungeon_combo.configure(state='disabled');self.role_combo.configure(state='disabled')
        logging.info('start party_mode=%s target=%s role=%s',self.party_mode.get(),self.target_dungeon.get(),self.party_role.get())
        self.status.set('3초 후 시작합니다. 게임 창을 클릭해 맨 앞으로 가져오세요.')
    def hotkeys(self):
        for vk,fn in [(0x77,self.toggle),(0x78,lambda:self.stop('F9 즉시정지'))]:
            down=bool(u.GetAsyncKeyState(vk)&0x8000)
            if down and not self.keys.get(vk):fn()
            self.keys[vk]=down
    def tick(self):
        try:
            self.hotkeys()
            if self.running:self.step()
        except Exception as e:
            logging.exception('실행 오류');self.stop('오류로 정지: '+str(e))
        self.root.after(350,self.tick)
    def step(self):
        now=time.monotonic()
        if now<self.delay:return
        if not u.IsWindow(self.hwnd):self.stop('게임 창이 닫혀 정지했습니다.');return
        if now-self.last_progress>self.wait:self.stop('진행 제한 시간을 초과해 정지했습니다. 화면을 확인하세요.');return
        if u.GetForegroundWindow()!=self.hwnd or u.IsIconic(self.hwnd):
            self.status.set('대기: 게임 창을 맨 앞으로 가져오세요.');return
        x,y,w,h=rect(self.hwnd)
        if w<320 or h<200:self.status.set('대기: 게임 내부 화면을 320×200 이상으로 늘려 주세요.');return
        if self.last_size is not None and self.last_size!=(w,h):
            self.resize_until=now+1;self.candidate=None;self.stable=0
            if self.pending:self.pending=(self.pending[0],now)
            logging.info('resize %s -> %s; waiting for stable layout',self.last_size,(w,h))
        self.last_size=(w,h)
        if now<self.resize_until:self.status.set('창 크기 변경 감지 · 화면이 안정되면 자동으로 인식을 재개합니다.');return
        rgb=ImageGrab.grab(bbox=(x,y,x+w,y+h),all_screens=True)
        frame,hits=self.det.scan(rgb)
        valid={k:v for k,v in hits.items() if v[0]>=self.th}
        # Confirm both the modal title and button before accepting an underfilled party.
        if 'challenge_title' not in valid:valid.pop('challenge',None)
        for k,(score,pos) in valid.items():
            px,py=map(int,pos);cv2.rectangle(frame,(px-45,py-20),(px+45,py+20),(255,80,0),3)
        self.photo=ImageTk.PhotoImage(Image.fromarray(frame).resize((460,284)));self.preview.configure(image=self.photo,text='',width=0,height=0)
        scores=' / '.join(f'{NAMES[k]} {v[0]:.2f}' for k,v in hits.items())
        if self.mode.get()=='observe':
            hint=' · 작은 화면: 인식이 안 되면 창을 늘려 주세요.' if w<800 or h<450 else ''
            self.status.set(f'인식 확인 · 클릭 없음 · 화면 {w}×{h}{hint}\n'+scores);return
        observed=screen_stage(valid,self.state)
        if observed!=self.state:
            logging.info('screen recovery %s -> %s',self.state,observed)
            self.state=observed;self.last_progress=now
        result_visible=('retry' in valid or 'other_dungeon' in valid) and 'partyready' not in valid and 'partycancel' not in valid and 'challenge_title' not in valid
        if result_visible:
            self.result_seen+=1
            if self.result_seen>=2 and not self.result_counted:
                self.count+=1;self.result_counted=True;self.last_progress=now
                logging.info('completion observed count=%d',self.count)
                if self.maxruns and self.count>=self.maxruns:
                    self.stop(f'{self.count}회 완료. 결과 화면에서 정지했습니다.');return
        else:
            self.result_seen=0
            if any(k in valid for k in ('compass','break','hud','skip')) and not any(k in valid for k in ('touch','challenge_title','partyready','partycancel')):
                self.result_counted=False
        # A party confirmation can overlay the entry/result screen.
        # Never send Space while the player is already marked ready.
        if 'partycancel' in valid:
            if self.pending:self.pending=None;self.last_progress=now
            self.state='queue';self.candidate=None;self.stable=0
            self.status.set('준비 완료 상태 · 다른 파티원과 입장을 기다립니다.')
            return
        if ('partyready' in valid or 'challenge' in valid) and self.pending and self.pending[0] in ('enter','retry','partyready'):
            self.pending=None;self.state='queue';self.last_progress=now
        if 'break' not in valid:
            self.break_absent+=1
            if self.break_absent>=2:self.break_used=False
        else:self.break_absent=0
        if self.break_skill.get() and self.state=='battle' and not self.pending and 'break' in valid and not self.break_used and now-self.break_last>2:
            if u.GetForegroundWindow()!=self.hwnd:return
            u.keybd_event(0x36,0x07,0,0)
            try:time.sleep(.12)
            finally:u.keybd_event(0x36,0x07,2,0)
            self.break_used=True;self.break_last=now
            logging.info('input=key_6 action=break_skill score=%.3f',hits['break'][0])
            self.status.set('브레이크 표시 감지 → 숫자 6 입력')
            return
        # Re-arm only after the food prompt disappears for several frames.
        if 'food' in valid:
            self.food_seen+=1;self.food_absent=0
        else:
            self.food_seen=0;self.food_absent+=1
            if self.food_absent>=4:self.food_used=False
        if self.food.get() and self.state in ('queue','battle') and not self.pending and self.food_seen>=2 and not self.food_used and now-self.food_last>10:
            if u.GetForegroundWindow()!=self.hwnd:return
            u.keybd_event(0x42,0x30,0,0)
            try:time.sleep(.12)
            finally:u.keybd_event(0x42,0x30,2,0)
            self.food_used=True;self.food_last=now;self.delay=time.monotonic()+1
            logging.info('input=key_B action=food score=%.3f',hits['food'][0])
            self.status.set('음식 B 버튼 감지 → B 입력. 버튼이 사라질 때까지 재입력하지 않습니다.')
            return
        if self.pending:
            key,when=self.pending
            if key not in valid:
                self.pending=None;self.last_progress=now
            elif now-when>30:self.stop(NAMES[key]+' 입력 후 화면 전환이 확인되지 않아 정지했습니다.');return
            else:return
        key=screen_action(valid,self.party_mode.get(),self.target_dungeon.get(),self.party_role.get())
        if key in ('retry','other_dungeon') and not self.result_counted:key=None
        self.stable=self.stable+1 if key and key==self.candidate else 1
        self.candidate=key
        size_note=' · 작은 화면: 인식이 안 되면 창을 늘려 주세요.' if w<800 or h<450 else ''
        party_note=f' · {self.party_role.get()}' if self.party_mode.get()=='4인' else ''
        waiting=' · 파티장의 재도전 요청을 기다립니다.' if self.party_mode.get()=='4인' and self.party_role.get()=='파티원' and result_visible else ''
        self.status.set(f'상태: {STAGE_LABELS.get(self.state,self.state)} · {self.party_mode.get()}{party_note} · 완료 {self.count}회 · 화면 {w}×{h}{waiting}{size_note}\n'+scores)
        if not key or self.stable<2:return
        px,py=hits[key][1]
        if key=='touch':px,py=830,560
        sx,sy=x+int(px*w/1660),y+int(py*h/1024)
        # Recheck ownership immediately before sending mouse input.
        if u.GetForegroundWindow()!=self.hwnd:return
        if u.GetAncestor(u.WindowFromPoint(W.POINT(sx,sy)),2)!=self.hwnd:return
        method='space' if self.inputmode.get()=='space' and key in ('enter','compass','retry','partyready','challenge') else 'mouse'
        if method=='space':
            u.keybd_event(0x20,0x39,0,0)
            try:time.sleep(.12)
            finally:u.keybd_event(0x20,0x39,2,0)
        else:
            u.SetCursorPos(sx,sy);time.sleep(.06)
            u.mouse_event(2,0,0,0,0)
            try:time.sleep(.12)
            finally:u.mouse_event(4,0,0,0,0)
        logging.info('input=%s action=%s score=%.3f point=(%d,%d) count=%d',method,key,hits[key][0],sx,sy,self.count)
        self.state={'enter':'queue','compass':'battle','skip':'battle','touch':'result','retry':'queue','partyready':'queue','challenge':'queue','other_dungeon':'dungeon_list','dungeon_dock':'ready','dungeon_cave':'ready','dungeon_water':'ready'}[key]
        self.pending=(key,now);self.delay=now+1.2;self.last_progress=now;self.stable=0

if __name__=='__main__':
    app=App()
    if '--selftest' in sys.argv:
        app.root.withdraw();app.root.update()
        for var,label in app.option_states:
            assert not var.get() and label.cget('text')=='사용 안 함'
            var.set(True);assert label.cget('text')=='사용 중'
            var.set(False);assert label.cget('text')=='사용 안 함'
        (HOME/'selftest.txt').write_text('OK: GUI, OpenCV, templates, Windows API initialized. No game input sent.',encoding='utf-8')
        app.root.destroy()
    else:app.root.mainloop()
