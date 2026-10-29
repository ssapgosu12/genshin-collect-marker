"""Genshin collection capture: one foreground keypress, one full-screen PNG."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import shutil
import tempfile
import hashlib
import threading
import time
import uuid
import asyncio
import math
from contextlib import nullcontext
import winsound
import random
import wave
from datetime import datetime
from zoneinfo import ZoneInfo
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageChops, ImageEnhance
import numpy as np
import cv2
from official_mark import RegistrationSession

ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
CAPTURES = ROOT / 'captures'
EXE = ROOT / 'upstream-genshin-overlay/build/Release/genshin-overlay.exe'
HDR_EXE = ROOT / 'build-work/capture-hdr.exe'
RAW_LIMIT = 2_000_000_000
RAW_DAYS = 21
CAPTURE_DELAY = .300
SETTINGS = ROOT/'capture-settings.json'
SOUNDS = ROOT/'sounds'
VOICE_CHARACTERS = {'klee':'클레','nahida':'나히다','kachina':'카치나','furina':'푸리나',
    'linnea':'린네아','noelle':'노엘','sucrose':'설탕','diona':'디오나','yae':'야에 미코',
    'raiden':'라이덴 쇼군','ayaka':'아야카','vesna':'베스나','citlali':'시틀라리',
    'columbina':'콜롬비나','sandrone':'산드로네','ganyu':'감우','collei':'콜레이'}
KINDS = {0x30: ('oculus', '신의눈'), 0xBD: ('chest', '상자'), 0xBB: ('mora', '모라상자')}
KEY_OPTIONS = {'0':0x30,'-':0xBD,'=':0xBB,**{str(i):0x30+i for i in range(1,10)},
               **{chr(i):i for i in range(65,91)},**{f'F{i}':0x6F+i for i in range(1,13)},
               **{f'Num{i}':0x60+i for i in range(10)}}
KIND_NAMES={'oculus':'신의눈','chest':'상자','mora':'모라상자'}
RESULT_RE = re.compile(r'map (\d+), group (\d+), floor (\d+)\)\s+lat ([\d.-]+)\s+lng ([\d.-]+)\s+zoom ([\d.]+)\s+score ([\d.]+)\s+search ([\d.]+) ms')


def capture_delay(settings):
    value=float(settings.get('capture_delay_ms',CAPTURE_DELAY*1000))
    if not math.isfinite(value) or not 0<=value<=5000:
        raise ValueError('캡처 대기시간은 0~5000ms로 입력하세요.')
    return value/1000


def sound_path(clip):
    path=(SOUNDS/clip['file']).resolve()
    if not path.is_relative_to(SOUNDS.resolve()) or not path.is_file():
        raise ValueError('음원 파일을 찾을 수 없습니다.')
    with wave.open(str(path),'rb') as stream:
        if not 0<stream.getnframes()/stream.getframerate()<=5.801:
            raise ValueError('음원은 끝 여유를 포함해 5.8초 이내여야 합니다.')
    return path


def audio_volume(value):
    value=float(value)
    if not math.isfinite(value) or not 0<=value<=100:
        raise ValueError('볼륨은 0~100%로 설정하세요.')
    return value


def clip_volume(settings,clip):
    key='capture_volume' if clip['file'].startswith('capture/') else 'voice_volume'
    return audio_volume(settings.get(key,settings.get('sound_volume',100)))


def scale_wav(source,destination,volume):
    volume=audio_volume(volume)
    with wave.open(str(source),'rb') as stream:
        params=stream.getparams()
        if stream.getsampwidth()!=2: raise ValueError('16비트 PCM 음원만 재생합니다.')
        samples=np.frombuffer(stream.readframes(stream.getnframes()),dtype='<i2').astype(np.float32)
    with wave.open(str(destination),'wb') as stream:
        stream.setparams(params)
        stream.writeframes(np.rint(samples*(volume/100)).astype('<i2').tobytes())


def select_sound(bank,settings,kind):
    if not settings.get('sound_enabled',True): return None
    if kind=='saved':
        return next((c for c in bank.get('capture',[]) if c['id']==settings.get('capture_sound','shutter')),None)
    if kind=='error':
        clips=bank.get('failure',[])
        return random.choice(clips) if clips else None
    if kind=='registered':
        enabled=settings.get('voice_characters',{})
        choices=[v['clips'] for key,v in bank.get('success',{}).items() if enabled.get(key,True) and v.get('clips')]
        return random.choice(random.choice(choices)) if choices else None
    return None


def tonemap_hdr(data, exposure_percent=60.0, hdr=True, inverse=False, white_nits=None):
    if not math.isfinite(exposure_percent) or not 1<=exposure_percent<=100:
        raise ValueError('HDR 밝기는 1~100%로 입력하세요.')
    if len(data)<148:
        raise ValueError('HDR 프레임이 잘렸습니다.')
    header=np.frombuffer(data[:148],dtype='<u4')
    width,height=int(header[4]),int(header[3])
    if (header[0]!=0x20534444 or header[32]!=10 or header[33]!=3 or header[35]!=1
            or not 0<width<=16384 or not 0<height<=16384 or len(data)!=148+width*height*8):
        raise ValueError('FP16 HDR 프레임 형식이 잘못됐습니다.')
    rgb=np.frombuffer(data,offset=148,dtype='<f2').reshape(height,width,4)[...,:3].astype(np.float32)
    if not np.isfinite(rgb).all():
        raise ValueError('HDR 프레임에 유효하지 않은 픽셀이 있습니다.')
    rgb=np.maximum(rgb,0)
    if hdr and inverse:
        if white_nits is None or not math.isfinite(white_nits) or white_nits<=0:
            raise ValueError('모니터 SDR 기준 흰색을 읽지 못했습니다. 다시 시도하거나 HDR 역보정을 끄세요.')
        rgb *= 80/white_nits
    elif hdr:
        # Exposure is applied before tone mapping, while highlight detail remains.
        rgb *= (80/203)*(exposure_percent/100)
        luminance=rgb@np.array([.2126,.7152,.0722],dtype=np.float32)
        rgb /= 1+luminance[...,None]
    # With HDR disabled, WGC's linear SDR pixels only need sRGB encoding.
    rgb=np.clip(rgb,0,1)
    encoded=rgb**(1/2.2) if hdr and inverse else np.where(rgb<=.0031308,12.92*rgb,1.055*rgb**(1/2.4)-.055)
    return Image.fromarray(np.rint(encoded*255).astype('uint8'))


def capture_hdr(hwnd, exposure_percent=60.0, inverse=True):
    process=subprocess.run([str(HDR_EXE),'-',str(hwnd)],cwd=ROOT,capture_output=True,
                           timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    details=process.stderr.decode('utf8',errors='replace').strip()
    if process.returncode:
        raise RuntimeError('HDR 캡처 실패: '+details)
    metadata=json.loads(details)
    is_hdr=metadata['output_color_space']==12
    frame=tonemap_hdr(process.stdout,float(exposure_percent),is_hdr,inverse,metadata.get('sdr_white_nits'))
    if frame.size!=(metadata['width'],metadata['height']):
        raise ValueError('캡처 크기와 메타데이터가 일치하지 않습니다.')
    applied=is_hdr and inverse
    metadata.update(method='WGC FP16',hdr_inverse_enabled=inverse,hdr_inverse_applied=applied,
                    tone_mapping='SDR white inverse + gamma 2.2' if applied else ('luminance Reinhard' if is_hdr else 'linear to sRGB'),
                    exposure_percent=None if applied else (exposure_percent if is_hdr else 100))
    return frame,metadata


def notification(kind,text):
    if kind=='saved':
        match=re.search(r'_KST_(oculus|chest|mora)_',text)
        name=KIND_NAMES[match[1]] if match else '수집품'
        return (name+' 캡처됨','위치 인식 대기 중','#245a85',4000)
    if kind=='info' and text.startswith('위치 인식 중:'):
        return ('캡처됨 · 위치 인식 중','아직 지도에 등록되지 않았습니다.','#245a85',0)
    if kind=='located':
        return ('위치 인식됨 · 등록 확인 중',text,'#806015',0)
    if kind=='registered':
        return ('등록 완료',text,'#146441',8000)
    if kind=='error':
        return ('미등록' if text.startswith('미등록') else '처리 오류',text,'#992f35',12000)
    return None


class StatusToast:
    """A reusable click-through, non-activating status window; no game hooks."""
    def __init__(self,parent):
        self.parent=parent
        self.window=tk.Toplevel(parent)
        self.window.title('원신 수집 알림')
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost',True)
        self.title=tk.Label(self.window,font=('',16,'bold'),fg='white',anchor='w')
        self.title.pack(fill='x',padx=16,pady=(12,3))
        self.detail=tk.Label(self.window,font=('',11),fg='white',wraplength=420,justify='left',anchor='w')
        self.detail.pack(fill='x',padx=16,pady=(0,12))
        self.u=C.WinDLL('user32',use_last_error=True)
        self.u.GetParent.argtypes=[W.HWND]
        self.u.GetParent.restype=W.HWND
        self.u.GetWindowLongPtrW.argtypes=[W.HWND,C.c_int]
        self.u.GetWindowLongPtrW.restype=C.c_ssize_t
        self.u.SetWindowLongPtrW.argtypes=[W.HWND,C.c_int,C.c_ssize_t]
        self.u.SetWindowLongPtrW.restype=C.c_ssize_t
        self.u.SetWindowPos.argtypes=[W.HWND,W.HWND,C.c_int,C.c_int,C.c_int,C.c_int,W.UINT]
        self.u.SetLayeredWindowAttributes.argtypes=[W.HWND,W.DWORD,W.BYTE,W.DWORD]
        self.u.GetForegroundWindow.restype=W.HWND
        self.u.MonitorFromWindow.argtypes=[W.HWND,W.DWORD]
        self.u.MonitorFromWindow.restype=W.HANDLE
        self.u.GetMonitorInfoW.argtypes=[W.HANDLE,C.c_void_p]
        self.window.update_idletasks()
        self.hwnd=self.u.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        style=self.u.GetWindowLongPtrW(self.hwnd,-20)
        self.u.SetWindowLongPtrW(self.hwnd,-20,style|0x08000000|0x80|0x20|0x80000)
        self.u.SetLayeredWindowAttributes(self.hwnd,0,242,2)
        self.u.SetWindowLongPtrW(self.hwnd,-8,0)
        self.timer=None

    def show(self,payload,hold_seconds=5.0,fade_seconds=1.0):
        title,detail,color,_duration=payload
        if self.timer:
            self.parent.after_cancel(self.timer)
            self.timer=None
        self.window.configure(bg=color)
        self.title.configure(text=title,bg=color)
        self.detail.configure(text=detail,bg=color)
        class Info(C.Structure):
            _fields_=[('size',W.DWORD),('monitor',W.RECT),('work',W.RECT),('flags',W.DWORD)]
        info=Info()
        info.size=C.sizeof(info)
        handle=self.u.MonitorFromWindow(self.u.GetForegroundWindow(),2)
        if not self.u.GetMonitorInfoW(handle,C.byref(info)):
            return
        width,height=460,116
        x,y=info.monitor.right-width-24,info.monitor.bottom-height-100
        self.window.geometry(f'{width}x{height}+{max(0,x)}+{max(0,y)}')
        self.window.deiconify()
        self.u.SetLayeredWindowAttributes(self.hwnd,0,242,2)
        self.u.SetWindowPos(self.hwnd,W.HWND(-1),x,y,width,height,0x0010|0x0040)
        def fade(start=None):
            start=time.monotonic() if start is None else start
            elapsed=time.monotonic()-start
            if fade_seconds<=0 or elapsed>=fade_seconds:
                self.hide()
                return
            alpha=max(0,round(242*(1-elapsed/fade_seconds)))
            self.u.SetLayeredWindowAttributes(self.hwnd,0,alpha,2)
            self.timer=self.parent.after(30,lambda:fade(start))
        self.timer=self.parent.after(max(0,round(hold_seconds*1000)),fade)

    def hide(self):
        self.window.withdraw()
        self.timer=None


def parse_location(output):
    match = RESULT_RE.search(output)
    if not match:
        raise ValueError('위치를 찾지 못했습니다. 캡처는 보관됩니다.')
    return dict(zip(('map', 'group', 'floor', 'lat', 'lng', 'zoom', 'score', 'search_ms'),
                    [int(v) for v in match.groups()[:3]] + [float(v) for v in match.groups()[3:]]))


def recognize_brightness(input_path, dark_factor=.6, notify=lambda text:None, area=None, single_factor=None):
    if not math.isfinite(dark_factor) or not .01<=dark_factor<=1:
        raise ValueError('인식 밝기는 1~100%로 설정하세요.')
    attempts=[]
    factors=[1.0] if dark_factor==1 else [1.0,dark_factor]
    if single_factor is not None:
        if not math.isfinite(single_factor) or not .01<=single_factor<=1:
            raise ValueError('잘못된 인식 밝기입니다.')
        factors=[single_factor]
    for factor in factors:
        path=input_path
        if factor!=1:
            path=input_path.with_name('frame-dark.png')
            with Image.open(input_path) as source:
                ImageEnhance.Brightness(source.convert('RGB')).enhance(factor).save(path,format='PNG',compress_level=1)
            notify(f'원본 인식 실패 · 밝기 {factor*100:g}%로 재시도 중')
        attempt={'brightness_factor':factor,'search_area':area}
        try:
            command=[str(EXE),'--minimap',str(path)]
            if area: command.extend(str(area[key]) for key in ('map','left','top','right','bottom'))
            process=subprocess.run(command,cwd=EXE.parent,
                capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW)
            attempt.update(exit_code=process.returncode,output=process.stdout,stderr=process.stderr)
            if process.returncode!=0:
                raise RuntimeError('위치 후보를 찾지 못했습니다.')
            location=parse_location(process.stdout)
            attempts.append(attempt)
            return {'status':'located','location':location,'brightness_used':factor,
                    'recognition_attempts':attempts,'exit_code':0,'output':process.stdout,'stderr':process.stderr}
        except (RuntimeError,ValueError,subprocess.TimeoutExpired) as exc:
            attempt['error']=str(exc)
            attempts.append(attempt)
    return {'status':'failed','recognition_attempts':attempts,
            'error':'위치 인식 실패 — 밝기별 시도 기록과 캡처를 보관했습니다.'}


def previous_area(file):
    """Only earlier confirmed captures supply a search region, never a result."""
    try: captured=datetime.strptime(file.name[:22],'%Y%m%d_%H%M%S_%f')
    except ValueError: return None
    for other in sorted(file.parent.glob('*_KST_*.json'),reverse=True):
        if other.stem>=file.stem: continue
        try:
            age=(captured-datetime.strptime(other.name[:22],'%Y%m%d_%H%M%S_%f')).total_seconds()
            if age>1800: break
            record=json.loads(other.read_text(encoding='utf8'))
            if record.get('registration',{}).get('status')!='marked': continue
            loc=record['location']; x,y=float(loc['lng']),float(loc['lat'])
            if not math.isfinite(x) or not math.isfinite(y): continue
            return dict(map=int(loc['map']),left=x-500,top=y-500,right=x+500,bottom=y+500)
        except (ValueError,KeyError,OSError): continue
    return None


def big_map_marker(frame):
    """Read the cyan player arrow; never substitute the map's screen center."""
    if isinstance(frame,Path):
        with Image.open(frame) as image:return big_map_marker(image)
    width,height=frame.size
    scale=720/height
    rgb=cv2.resize(np.asarray(frame.convert('RGB')),(round(width*scale),720))
    red,green,blue=cv2.split(rgb)
    mask=((blue>150)&(green>150)&(red<np.minimum(blue,green)*.25)&
          (np.abs(blue.astype(float)-green.astype(float))<30)).astype('uint8')*255
    mask=cv2.erode(mask,np.ones((3,3),np.uint8))
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    candidates=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        area=cv2.contourArea(contour)
        polygon=cv2.approxPolyDP(contour,.06*cv2.arcLength(contour,True),True)
        if x<rgb.shape[1]*.15 and y<240:continue
        if 15<=w<=38 and 15<=h<=38 and 100<=area<=600 and len(polygon) in (3,4):
            (cx,cy),_=cv2.minEnclosingCircle(contour)
            candidates.append((cx/scale,cy/scale))
    # Reuse the native recognizer's zoom-bar asset to detect maps without a visible player.
    template=cv2.imread(str(ROOT/'upstream-genshin-overlay/zoombar_template.png'),0)
    score=0.0
    if template is not None:
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)[216:504,:round(rgb.shape[1]*.15)]
        for factor in (.30,.33,.35,.38,.40):
            small=cv2.resize(template,None,fx=factor,fy=factor)
            if gray.shape[1]>=small.shape[1]:
                score=max(score,float(cv2.minMaxLoc(cv2.matchTemplate(gray,small,cv2.TM_CCOEFF_NORMED))[1]))
    edges=cv2.Canny(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)[180:540,:min(110,rgb.shape[1])],25,70)
    lines=cv2.HoughLinesP(edges,1,np.pi/180,40,minLineLength=90,maxLineGap=8)
    track=lines is not None and any(20<=int(a)<=100 and abs(int(a)-int(c))<=1 and abs(int(b)-int(d))>=90 for a,b,c,d in lines[:,0])
    return {'is_map':score>=.65 or bool(track),'arrow':candidates[0] if len(candidates)==1 else None,
            'arrow_candidates':len(candidates),'zoom_bar_score':score}


def recognize_big_map(input_path,marker,dark_factor,notify=lambda text:None):
    point=marker['arrow']
    result={'status':'failed','recognition_mode':'map','map_marker':marker,'recognition_attempts':[]}
    if point is None:
        result['error']='큰 지도에서 플레이어 화살표를 하나로 확인하지 못했습니다. 플레이어 표시가 보이도록 지도를 두세요.'
        return result
    with Image.open(input_path) as source:rgb=np.asarray(source.convert('RGB')).copy()
    x,y=point;radius=max(20,round(rgb.shape[0]*.1))
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    local=gray[max(0,int(y)-radius):min(gray.shape[0],int(y)+radius),max(0,int(x)-radius):min(gray.shape[1],int(x)+radius)]
    cutoff=float(np.percentile(local,80))*.7
    variants=[('active-layer',np.where((gray>=cutoff)[...,None],rgb,0).astype('uint8')),('full-map',rgb)]
    for name,pixels in variants:
        notify('큰 지도 · 플레이어 화살표 위치 검색 · '+name)
        prepared=input_path.with_name('big-map-match.png')
        Image.fromarray(pixels).save(prepared,compress_level=1)
        attempt={'mode':'map','variant':name,'player_pixel':point}
        try:
            run=subprocess.run([str(EXE),'--locate',str(prepared),str(x),str(y)],cwd=EXE.parent,
                capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
            attempt.update(exit_code=run.returncode,output=run.stdout,stderr=run.stderr)
            match=re.search(r'map (\d+), group (\d+), floor (\d+)\)\s+lat ([\d.-]+)\s+lng ([\d.-]+)\s+units/px ([\d.eE+-]+)\s+inliers (\d+)',run.stdout)
            if run.returncode or not match or int(match[7])<20:raise ValueError('큰 지도 지형 매칭 근거가 부족합니다.')
            location=dict(zip(('map','group','floor','lat','lng','units_per_pixel','inliers'),
                [int(match[i]) for i in (1,2,3)]+[float(match[i]) for i in (4,5,6)]+[int(match[7])]))
            result['recognition_attempts'].append(attempt)
            result.update(status='located',location=location,recognition_ui_used='map',output=run.stdout,exit_code=0)
            return result
        except (ValueError,subprocess.TimeoutExpired) as exc:attempt['error']=str(exc)
        result['recognition_attempts'].append(attempt)
    result['error']='큰 지도 위치 인식 실패 — 캡처와 시도 기록을 보관했습니다.'
    return result


def recognize_layouts(file,input_path,dark_factor,notify=lambda text:None,input_lock=None):
    if not math.isfinite(dark_factor) or not .01<=dark_factor<1:
        raise ValueError('어두운 인식 밝기는 1~99%로 설정하세요.')
    with input_lock if input_lock is not None else nullcontext():
        recognition_input(file,input_path,'pc')
    marker=big_map_marker(input_path)
    if marker['is_map']:return recognize_big_map(input_path,marker,dark_factor,notify)
    area=previous_area(file)
    attempts=[]
    for number,(ui,factor) in enumerate([('pc',1.0),('controller',1.0),('pc',dark_factor),('controller',dark_factor)],1):
        if number>1:
            with input_lock if input_lock is not None else nullcontext():
                recognition_input(file,input_path,ui)
        notify(f'{number}/4 · '+('키보드 UI' if ui=='pc' else '패드 UI')+f' · 밝기 {factor*100:g}%')
        # The native call already searches the hinted area then falls back globally.
        result=recognize_brightness(input_path,dark_factor,area=area,single_factor=factor)
        attempts.extend(dict(a,ui=ui,attempt_number=number) for a in result['recognition_attempts'])
        if result['status']=='located':
            result.update(recognition_attempts=attempts,recognition_ui_used=ui)
            return result
    result['recognition_attempts']=attempts
    result['error']='위치 인식 4회 실패 — 키보드·패드 및 원본·어두운 밝기 시도 기록과 캡처를 보관했습니다.'
    return result


def filename(kind):
    stamp = datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d_%H%M%S_%f')
    return f'{stamp}_KST_{kind}_{uuid.uuid4().hex[:8]}.png'


def atomic_json(path, content):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temp, path)


def capture_metadata(file):
    path = file.with_suffix('.json')
    return json.loads(path.read_text(encoding='utf8')) if path.exists() else {'capture':file.name,'status':'pending'}


def binding_map(bindings):
    if set(bindings)!=set(KIND_NAMES) or any(v not in KEY_OPTIONS for v in bindings.values()):
        raise ValueError('지원되는 단일 키를 종류별로 선택하세요.')
    if len(set(bindings.values()))!=3:
        raise ValueError('세 종류에 서로 다른 키를 지정하세요.')
    return {KEY_OPTIONS[key]:(kind,KIND_NAMES[kind]) for kind,key in bindings.items()}


def delete_capture_images(file):
    root=file.parent.resolve()
    paths=[file,root/'archive'/(file.stem+'.overview.jpg'),root/'archive'/(file.stem+'.minimap.png')]
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('캡처 폴더 밖의 파일은 삭제하지 않습니다.')
    # A tombstone retains any assigned ID; deleting a row must not consume a
    # different marker if an old worker entry is encountered after restart.
    record=capture_metadata(file)
    if record.get('queue_state')!='deleted':
        record['queue_state']='deleted'
        atomic_json(file.with_suffix('.json'),record)
    for path in paths:
        path.unlink(missing_ok=True)


def archive_capture(file):
    """Verify both replacements and commit metadata before removing the full PNG."""
    directory = file.parent.resolve()
    if file.is_symlink() or file.resolve().parent != directory:
        raise ValueError('원본 경로가 캡처 폴더 밖입니다.')
    target = directory / 'archive'
    if target.is_symlink():
        raise ValueError('보관 폴더가 외부 경로를 가리킵니다.')
    target.mkdir(exist_ok=True)
    preview = target / (file.stem+'.overview.jpg')
    minimap = target / (file.stem+'.minimap.png')
    if any(p.is_symlink() for p in (preview,minimap,file.with_suffix('.json'))):
        raise ValueError('보관 파일의 심볼릭 링크는 지원하지 않습니다.')
    modified = file.stat().st_mtime
    metadata=capture_metadata(file)
    with Image.open(file) as source:
        source = source.convert('RGB')
        width,height = source.size
        is_map=metadata.get('recognition_mode')=='map' or big_map_marker(source)['is_map']
        if is_map:
            region=source.copy()
            region.thumbnail((1280,720),Image.Resampling.LANCZOS)
            box=(0,0,region.width,region.height)
            stored_size=list(region.size)
        else:
            box = (0,0,min(width,round(height*384/1080)),min(height,round(height*300/1080)))
            region = source.crop(box)
            stored_size=[width,height]
        small = source.copy()
        small.thumbnail((960,540),Image.Resampling.LANCZOS)
        for dest,image,fmt,options in ((preview,small,'JPEG',{'quality':80,'optimize':True}),
                                       (minimap,region,'PNG',{'compress_level':6})):
            temp = dest.with_suffix(dest.suffix+'.tmp')
            image.save(temp,format=fmt,**options)
            with Image.open(temp) as check:
                check.load()
                if check.size != image.size:
                    raise ValueError('보관 이미지 크기 검증 실패')
                if fmt=='PNG' and ImageChops.difference(check.convert('RGB'),region).getbbox() is not None:
                    raise ValueError('미니맵 픽셀 검증 실패')
            os.replace(temp,dest)
        metadata = capture_metadata(file)
        metadata['archive'] = {'preview':preview.relative_to(directory).as_posix(),
                               'minimap':minimap.relative_to(directory).as_posix(),
                               'source_size':stored_size,'original_size':[width,height],'content':'map' if is_map else 'minimap','region_box':list(box),
                               'region_pixels_sha256':hashlib.sha256(region.tobytes()).hexdigest(),
                               'source_mtime':modified,'archived_at':datetime.now(ZoneInfo('Asia/Seoul')).isoformat()}
        atomic_json(file.with_suffix('.json'),metadata)
        # Reopen final paths, not just their temporary files, before deletion.
        with Image.open(minimap) as check:
            if hashlib.sha256(check.convert('RGB').tobytes()).hexdigest() != metadata['archive']['region_pixels_sha256']:
                raise ValueError('최종 미니맵 검증 실패')
        with Image.open(preview) as check:
            check.load()
    file.unlink()
    return metadata


def maintain_storage(directory=CAPTURES, now=None, limit=RAW_LIMIT):
    now = time.time() if now is None else now
    originals = sorted(directory.glob('*_KST_*.png'),key=lambda p:p.stat().st_mtime)
    total = sum(p.stat().st_size for p in originals)
    converted,errors = [],[]
    for file in originals:
        try:
            metadata = capture_metadata(file)
            if metadata.get('queue_state')=='deleted':
                size=file.stat().st_size
                delete_capture_images(file)
                total-=size
                continue
            finished = metadata.get('status')=='located' and (not metadata.get('register_requested') or metadata.get('registration',{}).get('status')=='marked')
            eligible = metadata.get('queue_state')=='held' or finished or now-file.stat().st_mtime>=RAW_DAYS*86400 or total>limit
            if eligible:
                size=file.stat().st_size
                archive_capture(file)
                total-=size
                converted.append(file.name)
        except Exception as exc:
            errors.append(file.name+': '+str(exc))
    return {'archived':converted,'raw_bytes':total,'errors':errors,'over_limit':total>limit}


def recognition_input(file, destination, ui_mode='pc'):
    if file.exists():
        shutil.copyfile(file,destination)
    else:
        metadata = capture_metadata(file)['archive']
        directory = file.parent.resolve()
        region_path = (directory/metadata['minimap']).resolve()
        if not region_path.is_relative_to(directory):
            raise ValueError('잘못된 미니맵 보관 경로')
        width,height = metadata['source_size']
        if not (0<width<=16384 and 0<height<=16384):
            raise ValueError('잘못된 원본 크기')
        with Image.open(region_path) as region:
            region = region.convert('RGB')
            if hashlib.sha256(region.tobytes()).hexdigest()!=metadata['region_pixels_sha256']:
                raise ValueError('미니맵 보관본 손상')
            frame=Image.new('RGB',(width,height))
            frame.paste(region,tuple(metadata['region_box'][:2]))
            frame.save(destination,format='PNG',compress_level=1)
    if ui_mode=='controller':
        with Image.open(destination) as original:
            frame=original.convert('RGB')
        scale=frame.height/1080
        # Pad UI: centered near (207,135), diameter 168 at 1080p.
        box=tuple(round(v*scale) for v in (123,51,291,219))
        crop=frame.crop(box)
        left,top,side=(math.floor(v*scale+.5) for v in (64,21,208))
        output=Image.new('RGB',frame.size)
        output.paste(crop.resize((side,side),Image.Resampling.LANCZOS),(left,top))
        output.save(destination,format='PNG',compress_level=1)
    elif ui_mode!='pc':
        raise ValueError('알 수 없는 UI 모드')


class KeyEdges:
    def __init__(self):
        self.down = set()

    def event(self, key, pressed, allowed, mapping=None):
        mapping=KINDS if mapping is None else mapping
        if not pressed:
            self.down.discard(key)
            return None
        repeated = key in self.down
        self.down.add(key)
        if not repeated and allowed and key in mapping:
            return mapping[key][0]
        return None


class Windows:
    def __init__(self):
        self.u = C.WinDLL('user32', use_last_error=True)
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.u.SetProcessDpiAwarenessContext.argtypes = [C.c_void_p]
        self.u.SetProcessDpiAwarenessContext(C.c_void_p(-4))
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetAsyncKeyState.argtypes = [C.c_int]
        self.u.GetAsyncKeyState.restype = C.c_short
        self.u.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
        self.u.MonitorFromWindow.restype = W.HANDLE
        self.u.GetMonitorInfoW.argtypes = [W.HANDLE, C.c_void_p]
        self.u.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.u.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
        self.k.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
        self.k.CreateMutexW.restype = W.HANDLE
        self.mutex = self.k.CreateMutexW(None, False, 'Local\\GenshinCollectCapture')
        if C.get_last_error() == 183:
            raise RuntimeError('이미 캡처 프로그램이 실행 중입니다.')

    def game(self):
        hwnd = self.u.GetForegroundWindow()
        title, cls = C.create_unicode_buffer(256), C.create_unicode_buffer(128)
        self.u.GetWindowTextW(hwnd, title, len(title))
        self.u.GetClassNameW(hwnd, cls, len(cls))
        return hwnd if cls.value == 'UnityWndClass' and title.value in ('원신', 'Genshin Impact', '原神') else None

    def monitor_rect(self, hwnd):
        class Info(C.Structure):
            _fields_ = [('cbSize', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]
        info = Info()
        info.cbSize = C.sizeof(info)
        if not self.u.GetMonitorInfoW(self.u.MonitorFromWindow(hwnd, 2), C.byref(info)):
            raise C.WinError(C.get_last_error())
        r = info.monitor
        client, point = W.RECT(), W.POINT()
        self.u.GetClientRect(hwnd, C.byref(client))
        self.u.ClientToScreen(hwnd, C.byref(point))
        # The recognizer's minimap coordinates assume a full-screen game frame.
        if abs(point.x-r.left)>2 or abs(point.y-r.top)>2 or abs(client.right-(r.right-r.left))>2 or abs(client.bottom-(r.bottom-r.top))>2:
            raise RuntimeError('원신을 전체화면/테두리 없는 전체화면으로 전환하세요.')
        return r.left, r.top, r.right, r.bottom

    def hook(self, emit, stop, ready, notify, get_bindings=lambda:KINDS):
        # Read only these three key states; do not intercept game input.
        edges = KeyEdges()
        ready.set()
        notify(('info', '준비됨 — 설정된 캡처 키를 사용하세요.'))
        previous=None
        while not stop.is_set():
            hwnd = self.game()
            modifiers = any(self.u.GetAsyncKeyState(k) & 0x8000 for k in (0x10,0x11,0x12,0x5B,0x5C))
            mapping=get_bindings()
            signature=tuple(sorted(mapping.items()))
            if signature!=previous:
                edges.down={k for k in mapping if self.u.GetAsyncKeyState(k)&0x8000}
                previous=signature
            for key in mapping:
                pressed = bool(self.u.GetAsyncKeyState(key) & 0x8000)
                if pressed and key not in edges.down:
                    notify(('info', f'입력 확인: VK={key}, 원신={bool(hwnd)}, 보조키={modifiers}'))
                kind = edges.event(key, pressed, bool(hwnd) and not modifiers,mapping)
                if kind:
                    emit((kind, hwnd, time.monotonic()))
            stop.wait(.01)


class App:
    def __init__(self):
        self.win = Windows()
        CAPTURES.mkdir(exist_ok=True)
        (ROOT/'inspection').mkdir(exist_ok=True)
        self.ui = tk.Tk()
        self.ui.title('원신 수집품 캡처')
        self.ui.geometry('980x650')
        self.messages, self.capture_jobs, self.recognize_jobs = queue.Queue(), queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.closing = False
        self.active = set()
        self.lock = threading.Lock()
        self.storage_lock = threading.Lock()
        self.worker_closed=threading.Event()
        self.shutdown_sent=False
        self.processing=None
        self.settings=json.loads(SETTINGS.read_text(encoding='utf8')) if SETTINGS.exists() else {
            'max_distance':20.0,'bindings':{'oculus':'0','chest':'-','mora':'='}}
        self.login_ready=threading.Event()
        if self.settings.get('onboarding_complete',False):self.login_ready.set()
        self.settings.setdefault('notification_hold',5.0)
        self.settings.setdefault('notification_fade',1.0)
        self.settings.setdefault('recognition_dark_percent',60.0)
        if self.settings['recognition_dark_percent']==100:
            self.settings['recognition_dark_percent']=60.0
        self.settings.setdefault('hdr_exposure_percent',60.0)
        self.settings.setdefault('hdr_inverse_enabled',True)
        self.settings.setdefault('sound_enabled',True)
        self.settings.setdefault('sound_volume',100.0)
        self.settings.setdefault('voice_volume',self.settings['sound_volume'])
        self.settings.setdefault('capture_volume',self.settings['sound_volume'])
        self.settings.setdefault('capture_delay_ms',300.0)
        self.settings.setdefault('capture_sound','shutter')
        self.settings.setdefault('voice_characters',{key:True for key in VOICE_CHARACTERS})
        capture_delay(self.settings)
        manifest=SOUNDS/'manifest.json'
        self.sound_bank=json.loads(manifest.read_text(encoding='utf8')) if manifest.exists() else {}
        self.keymap=binding_map(self.settings['bindings'])
        self.max_distance=float(self.settings['max_distance'])
        if not math.isfinite(self.max_distance) or self.max_distance<=0:
            raise ValueError('설정 파일의 거리 제한이 잘못됐습니다.')
        self.key_text=tk.StringVar()
        self.update_key_text()
        ttk.Label(self.ui,textvariable=self.key_text,font=('',16)).pack(pady=12)
        ttk.Label(self.ui, text='캡처 → 위치 인식 → 같은 종류의 가까운 미획득 항목 1개 등록\n지상/지하 구분 · 지하 세부 층 무시 · 실패 시 보관·알림').pack()
        self.status = tk.StringVar(value='시작 중…')
        ttk.Label(self.ui, textvariable=self.status, wraplength=920).pack(pady=10)
        self.toast=StatusToast(self.ui)
        row = ttk.Frame(self.ui)
        row.pack()
        ttk.Label(row,text='거리 제한(약 m)').pack(side='left')
        self.distance_text=tk.StringVar(value=f'{self.max_distance:g}')
        ttk.Entry(row,textvariable=self.distance_text,width=6).pack(side='left')
        self.distance_text.trace_add('write',self.distance_changed)
        ttk.Button(row, text='캡처 폴더', command=lambda: os.startfile(CAPTURES)).pack(side='left',padx=5)
        ttk.Button(row,text='설정',command=self.binding_menu).pack(side='left',padx=5)
        ttk.Button(row,text='지도 열기',command=lambda:self.recognize_jobs.put('login')).pack(side='left',padx=5)
        queue_frame=ttk.LabelFrame(self.ui,text='미인식·미등록 큐 — 재시도: 원본 UI 우선, 실패 시 패드 UI')
        queue_frame.pack(fill='both',expand=True,padx=10,pady=10)
        self.queue_view=tk.StringVar(value='대기')
        view=ttk.Combobox(queue_frame,textvariable=self.queue_view,values=('대기','보관','전체'),state='readonly',width=10)
        view.pack(anchor='w')
        view.bind('<<ComboboxSelected>>',lambda _:self.refresh_queue())
        self.queue_tree=ttk.Treeview(queue_frame,columns=('kind','state','reason'),show='tree headings',selectmode='extended',height=10)
        self.queue_tree.heading('#0',text='캡처 시각 / 파일')
        self.queue_tree.column('#0',width=280)
        for col,title,width in [('kind','종류',80),('state','상태',90),('reason','미등록 사유',390)]:
            self.queue_tree.heading(col,text=title)
            self.queue_tree.column(col,width=width)
        scroll=ttk.Scrollbar(queue_frame,orient='vertical',command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        self.queue_tree.pack(fill='both',expand=True)
        controls=ttk.Frame(queue_frame)
        controls.pack(fill='x',pady=5)
        for all_items,prefix in [(False,'선택'),(True,'전체')]:
            for action,label in [('retry','재시도'),('held','보관'),('deleted','삭제')]:
                ttk.Button(controls,text=prefix+' '+label,command=lambda a=action,b=all_items:self.queue_action(a,b)).pack(side='left',padx=3)
        ttk.Button(controls,text='미리보기',command=self.preview_selected).pack(side='right')
        self.queue_dirty=True
        self.log = tk.Text(self.ui, height=5, state='disabled')
        self.log.pack(fill='both',expand=True,padx=10,pady=10)
        self.ui.protocol('WM_DELETE_WINDOW', self.close)
        if not self.login_ready.is_set():self.recognize_jobs.put('login')
        self.recognize_jobs.put(None)
        self.ui.after(3600000,self.schedule_cleanup)
        # Recover interrupted deletions once, rather than rereading all archive
        # metadata on every capture.
        for metadata_file in CAPTURES.glob('*_KST_*.json'):
            try:
                record=json.loads(metadata_file.read_text(encoding='utf8'))
                if record.get('queue_state')=='deleted':
                    self.recognize_jobs.put(('delete',metadata_file.with_suffix('.png')))
                elif record.get('registration',{}).get('status')!='marked' and record.get('register_requested'):
                    record['registration']['max_distance']=self.max_distance
                    atomic_json(metadata_file,record)
            except (ValueError,OSError) as exc:
                self.messages.put(('error',str(exc)))
        # A one-time recovery scan; no periodic directory polling.
        for file in self.pending_files(only_unprocessed=True):
            self.enqueue(file)
        threading.Thread(target=self.capture_worker, daemon=True).start()
        threading.Thread(target=self.recognize_worker, daemon=True).start()
        threading.Thread(target=self.win.hook, args=(self.capture_jobs.put,self.stop,threading.Event(),self.messages.put,lambda:self.keymap),daemon=True).start()
        self.ui.after(100,self.poll)

    def enqueue(self, path):
        with self.lock:
            if path in self.active:
                return
            self.active.add(path)
        self.recognize_jobs.put(path)

    def capture_worker(self):
        while True:
            kind, hwnd, pressed = self.capture_jobs.get()
            temp = None
            try:
                if not self.login_ready.is_set():
                    raise ValueError('먼저 지도 열기에서 공식 지도 로그인을 완료하세요.')
                maximum=self.max_distance
                if maximum is None:
                    raise ValueError('거리 제한에 양수를 입력하세요.')
                with self.storage_lock:
                    report=maintain_storage(CAPTURES)
                    if report['over_limit']:
                        raise RuntimeError('원본 보관 용량 정리에 실패했습니다. 기존 파일을 보존하고 새 캡처를 중지합니다.')
                time.sleep(max(0,capture_delay(self.settings)-(time.monotonic()-pressed)))
                if self.win.game() != hwnd:
                    raise RuntimeError('저장 전에 원신 포커스가 바뀌어 캡처하지 않았습니다. 다시 눌러주세요.')
                frame,capture_info = capture_hdr(hwnd,self.settings['hdr_exposure_percent'],self.settings['hdr_inverse_enabled'])
                file = CAPTURES / filename(kind)
                temp = file.with_suffix('.png.tmp')
                with self.storage_lock:
                    atomic_json(file.with_suffix('.json'),{'capture':file.name,'kind':kind,'status':'pending','capture_info':capture_info,
                        'register_requested':True,'registration':{'status':'pending','max_distance':maximum}})
                frame.save(temp, format='PNG', compress_level=6)
                with self.storage_lock:
                    room=RAW_LIMIT-temp.stat().st_size
                    report=maintain_storage(CAPTURES,limit=max(0,room))
                    if room<0 or report['over_limit']:
                        raise RuntimeError('원본 2GB 한도를 확보하지 못했습니다. 보관 오류를 확인하고 다시 눌러주세요.')
                    os.replace(temp,file)
                self.messages.put(('saved',file.name))
                self.enqueue(file)
                self.cleanup()
            except Exception as exc:
                self.messages.put(('error',str(exc)))
            finally:
                if temp is not None and temp.exists():
                    try:
                        temp.unlink()
                    except OSError as exc:
                        self.messages.put(('error','임시 캡처 정리 실패: '+str(exc)))
                self.capture_jobs.task_done()

    def recognize_worker(self):
        loop=asyncio.new_event_loop()
        session=RegistrationSession()
        try:
            while True:
                file=self.recognize_jobs.get()
                try:
                    if isinstance(file,Path):
                        with self.lock:
                            self.processing=file
                    if file is False:
                        break
                    if file is None:
                        self.cleanup()
                    elif isinstance(file,tuple) and file[0]=='delete':
                        with self.storage_lock:
                            delete_capture_images(file[1])
                        self.messages.put(('refresh',''))
                    elif file=='login':
                        if not self.login_ready.is_set():
                            self.messages.put(('setup_pending','첫 실행: 공식 지도에 로그인해 주세요. 확인되면 캡처 키가 활성화됩니다.'))
                            loop.run_until_complete(session.ensure_login(self.stop))
                            self.login_ready.set()
                            self.messages.put(('setup_complete','로그인 확인 완료 — 준비됨. 설정된 캡처 키를 사용하세요.'))
                        else:
                            loop.run_until_complete(session.open(visible=True))
                            self.messages.put(('info','공식 지도를 열었습니다. 지도 창을 닫아도 캡처 앱은 계속 실행됩니다.'))
                    else:
                        loop.run_until_complete(self.process_capture(file,session))
                except asyncio.CancelledError:
                    self.messages.put(('info','지도 작업이 중단됐습니다. 캡처 앱은 계속 실행됩니다.'))
                except Exception as exc:
                    self.messages.put(('error','미등록: '+str(exc)))
                finally:
                    if isinstance(file,Path):
                        with self.lock:
                            self.processing=None
                            self.active.discard(file)
                        self.messages.put(('refresh',''))
                    self.recognize_jobs.task_done()
        finally:
            try:
                loop.run_until_complete(session.close())
            finally:
                loop.close()
                self.worker_closed.set()

    def read_event(self,file):
        with self.storage_lock:
            return capture_metadata(file)

    def write_event(self,file,updates):
        with self.storage_lock:
            record=capture_metadata(file)
            record.update(updates)
            atomic_json(file.with_suffix('.json'),record)

    def reserved_points(self,file,map_id):
        reserved=set()
        with self.storage_lock:
            for other in CAPTURES.glob('*_KST_*.json'):
                if other==file.with_suffix('.json'):
                    continue
                record=json.loads(other.read_text(encoding='utf8'))
                target=record.get('registration',{}).get('target')
                if target and target['map']==map_id:
                    reserved.add(target['id'])
        return reserved

    async def process_capture(self,file,session):
        record=self.read_event(file)
        if record.get('queue_state') in ('held','deleted') or record.get('registration',{}).get('status')=='marked':
            return
        if record.get('status')!='located':
            started=time.monotonic()
            result={'capture':file.name,'status':'failed','llm_calls':0}
            self.messages.put(('info','위치 인식 중: '+file.name))
            try:
                work=CAPTURES/'.work'
                work.mkdir(exist_ok=True)
                with tempfile.TemporaryDirectory(dir=work) as temp_dir:
                    input_path=Path(temp_dir)/'frame.png'
                    percent=getattr(self,'settings',{}).get('recognition_dark_percent',60.0)
                    result.update(recognize_layouts(file,input_path,float(percent)/100,
                        lambda text:self.messages.put(('info','위치 인식 중: '+text)),input_lock=self.storage_lock))
                if result['status']=='failed':
                    self.messages.put(('error','미등록: '+file.name+': '+result['error']))
            except Exception as exc:
                result['error']=str(exc)
                self.messages.put(('error','미등록: '+file.name+': '+str(exc)))
            finally:
                result['elapsed_seconds']=round(time.monotonic()-started,3)
                try:
                    with self.storage_lock:
                        existing=capture_metadata(file)
                        existing.pop('error',None)
                        existing.pop('location',None)
                        existing.update(result)
                        atomic_json(file.with_suffix('.json'),existing)
                except Exception as exc:
                    self.messages.put(('error','인식 결과 저장 실패: '+str(exc)))
                    return
        record=self.read_event(file)
        if record.get('status')=='located' and record.get('register_requested'):
            self.messages.put(('located','공식 지도에 저장됐는지 확인하고 있습니다.'))
            state=await session.register(file,self.read_event,self.write_event,self.reserved_points)
            if state['status']=='marked':
                target=state['target']
                plane='지하' if target['underground'] else '지상'
                self.messages.put(('registered',f'{KIND_NAMES.get(record["kind"],record["kind"])} · {plane} · 거리 약 {target["distance"]:.1f}m · 핀 {target["id"]}'))
            else:
                self.messages.put(('error','미등록: '+state.get('reason','저장 확인 실패')))
        self.cleanup()

    def pending_files(self, only_unprocessed=False):
        files={p.with_suffix('.png') for p in CAPTURES.glob('*_KST_*.json')}|set(CAPTURES.glob('*_KST_*.png'))
        for file in sorted(files):
            try:
                record=capture_metadata(file)
                if record.get('queue_state') in ('held','deleted'):
                    continue
                status=record.get('status','pending')
                registration=record.get('registration',{})
                if record.get('register_requested'):
                    wanted=registration.get('status')!='marked'
                    if only_unprocessed:
                        wanted=wanted and (status=='pending' or (status=='located' and (registration.get('status') in ('pending','sending') or bool(registration.get('target')))))
                else:
                    wanted=status=='pending' if only_unprocessed else status!='located'
            except (ValueError,OSError):
                wanted=True
            if wanted:
                yield file

    def retry(self):
        for file in self.pending_files():
            self.enqueue(file)

    def update_key_text(self):
        self.key_text.set('     '.join(f'{self.settings["bindings"][k]}  {name}' for k,name in KIND_NAMES.items()))

    def binding_menu(self):
        dialog=tk.Toplevel(self.ui)
        dialog.title('키·알림·인식 설정')
        dialog.transient(self.ui)
        ttk.Label(dialog,text='보조키 없는 단일 키 · 입력은 게임에도 전달됩니다.').grid(row=0,column=0,columnspan=2,padx=12,pady=12)
        choices={}
        for row,(kind,name) in enumerate(KIND_NAMES.items(),1):
            ttk.Label(dialog,text=name).grid(row=row,column=0,padx=12,pady=6)
            choices[kind]=tk.StringVar(value=self.settings['bindings'][kind])
            ttk.Combobox(dialog,textvariable=choices[kind],values=list(KEY_OPTIONS),state='readonly').grid(row=row,column=1,padx=12)
        hold=tk.StringVar(value=f'{self.settings["notification_hold"]:g}')
        fade=tk.StringVar(value=f'{self.settings["notification_fade"]:g}')
        ttk.Label(dialog,text='알림 표시 시간(초)').grid(row=4,column=0,padx=12,pady=6)
        ttk.Entry(dialog,textvariable=hold).grid(row=4,column=1,padx=12)
        ttk.Label(dialog,text='페이드 시간(초, 0=바로 닫기)').grid(row=5,column=0,padx=12,pady=6)
        ttk.Entry(dialog,textvariable=fade).grid(row=5,column=1,padx=12)
        dark=tk.StringVar(value=f'{self.settings["recognition_dark_percent"]:g}')
        ttk.Label(dialog,text='어두운 인식 밝기(%, 1~99)').grid(row=6,column=0,padx=12,pady=6)
        ttk.Entry(dialog,textvariable=dark).grid(row=6,column=1,padx=12)
        ttk.Label(dialog,text='키보드 → 패드 → 어두운 키보드 → 어두운 패드 (최대 4회)').grid(row=7,column=0,columnspan=2,padx=12,pady=6)
        exposure=tk.StringVar(value=f'{self.settings["hdr_exposure_percent"]:g}')
        ttk.Label(dialog,text='HDR 캡처 밝기(%, 낮을수록 어둡게)').grid(row=8,column=0,padx=12,pady=6)
        exposure_entry=ttk.Entry(dialog,textvariable=exposure)
        exposure_entry.grid(row=8,column=1,padx=12)
        inverse=tk.BooleanVar(value=self.settings['hdr_inverse_enabled'])
        def toggle_inverse():
            exposure_entry.configure(state='disabled' if inverse.get() else 'normal')
        ttk.Checkbutton(dialog,text='HDR 역보정 (기본 켜짐)',variable=inverse,command=toggle_inverse).grid(row=9,column=0,columnspan=2,padx=12,pady=6)
        toggle_inverse()
        ttk.Label(dialog,text='켜짐: 모니터 SDR 흰색 역보정 + 감마 2.2\n꺼짐: 위 밝기로 Reinhard 변환 · 다음 캡처부터 적용').grid(row=10,column=0,columnspan=2,padx=12,pady=6)
        def apply():
            values={k:v.get() for k,v in choices.items()}
            try:
                mapping=binding_map(values)
                hold_value,fade_value=float(hold.get()),float(fade.get())
                if not math.isfinite(hold_value) or hold_value<=0 or not math.isfinite(fade_value) or fade_value<0:
                    raise ValueError('표시 시간은 양수, 페이드 시간은 0 이상으로 입력하세요.')
                dark_value=float(dark.get())
                if not math.isfinite(dark_value) or not 1<=dark_value<100:
                    raise ValueError('인식 밝기는 1~99%로 입력하세요.')
                exposure_value=float(exposure.get())
                if not math.isfinite(exposure_value) or not 1<=exposure_value<=100:
                    raise ValueError('HDR 밝기는 1~100%로 입력하세요.')
                new=dict(self.settings,bindings=values,notification_hold=hold_value,notification_fade=fade_value,
                         recognition_dark_percent=dark_value,hdr_exposure_percent=exposure_value,hdr_inverse_enabled=inverse.get())
                atomic_json(SETTINGS,new)
                self.settings=new
                self.keymap=mapping
                self.update_key_text()
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror('키 설정',str(exc),parent=dialog)
        ttk.Button(dialog,text='저장',command=apply).grid(row=11,column=0,columnspan=2,pady=12)
        ttk.Button(dialog,text='소리·캡처 대기시간 설정',command=self.sound_menu).grid(row=12,column=0,columnspan=2,pady=6)

    def play_clip(self,clip,volume=None):
        try:
            value=clip_volume(self.settings,clip) if volume is None else audio_volume(volume)
            winsound.PlaySound(None,0)
            if value==0:return
            path=sound_path(clip)
            if value!=100:
                cache=SOUNDS/'.playback';cache.mkdir(exist_ok=True)
                self._audio_slot=1-getattr(self,'_audio_slot',0)
                target=cache/f'volume-{self._audio_slot}.wav'
                scale_wav(path,target,value);path=target
            winsound.PlaySound(str(path),winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
        except (OSError,ValueError,RuntimeError,wave.Error) as exc:
            self.messages.put(('info','소리 재생 실패: '+str(exc)))

    def sound_menu(self):
        dialog=tk.Toplevel(self.ui)
        dialog.title('소리·캡처 대기시간 설정')
        dialog.transient(self.ui)
        master=tk.BooleanVar(value=self.settings['sound_enabled'])
        voice_volume=tk.DoubleVar(value=self.settings.get('voice_volume',self.settings.get('sound_volume',100)))
        capture_volume=tk.DoubleVar(value=self.settings.get('capture_volume',self.settings.get('sound_volume',100)))
        delay=tk.StringVar(value=f'{self.settings["capture_delay_ms"]:g}')
        ttk.Checkbutton(dialog,text='소리 켜기 (캡처·성공·실패)',variable=master).grid(row=0,column=0,columnspan=3,sticky='w',padx=12,pady=8)
        ttk.Label(dialog,text='키 입력 후 캡처 대기시간 (ms, 기본 300)').grid(row=1,column=0,columnspan=2,padx=12)
        ttk.Entry(dialog,textvariable=delay,width=9).grid(row=1,column=2,padx=12)
        ttk.Label(dialog,text='패드 → 키보드 UI 전환을 기다립니다. 1000ms = 1초').grid(row=2,column=0,columnspan=3,pady=6)
        volume_frame=ttk.Frame(dialog);volume_frame.grid(row=3,column=0,columnspan=3,sticky='ew',padx=12)
        for row,(label,variable) in enumerate([('캐릭터 음성 볼륨 (성공·실패)',voice_volume),('캡처 SE 볼륨',capture_volume)]):
            ttk.Label(volume_frame,text=label).grid(row=row,column=0,sticky='w')
            ttk.Scale(volume_frame,from_=0,to=100,variable=variable,length=240).grid(row=row,column=1,padx=8)
            ttk.Spinbox(volume_frame,from_=0,to=100,textvariable=variable,width=6).grid(row=row,column=2)
            ttk.Label(volume_frame,text='%').grid(row=row,column=3)
        capture=tk.StringVar(value=self.settings['capture_sound'])
        frame=ttk.LabelFrame(dialog,text='캡처 SE — 하나 선택')
        frame.grid(row=4,column=0,columnspan=3,sticky='ew',padx=12,pady=8)
        for i,clip in enumerate(self.sound_bank.get('capture',[])):
            ttk.Radiobutton(frame,text=clip['name'],variable=capture,value=clip['id']).grid(row=i,column=0,sticky='w',padx=8)
            ttk.Button(frame,text='듣기',command=lambda c=clip:self.play_clip(c,capture_volume.get())).grid(row=i,column=1,padx=8)
        voices=ttk.LabelFrame(dialog,text='등록 성공 — 켜진 캐릭터 중 랜덤 (일부 캐릭터 최대 6종)')
        voices.grid(row=5,column=0,columnspan=3,padx=12,pady=8,sticky='ew')
        enabled={}
        for i,(key,name) in enumerate(VOICE_CHARACTERS.items()):
            clips=self.sound_bank.get('success',{}).get(key,{}).get('clips',[])
            enabled[key]=tk.BooleanVar(value=self.settings['voice_characters'].get(key,True))
            cell=ttk.Frame(voices);cell.grid(row=i//3,column=i%3,sticky='w',padx=6,pady=3)
            button=ttk.Checkbutton(cell,text=f'{name} ({len(clips)}종)' if clips else f'{name} (음원 없음)',variable=enabled[key])
            button.pack(side='left')
            if clips:ttk.Button(cell,text='▶',width=2,command=lambda cs=clips:self.play_clip(random.choice(cs),voice_volume.get())).pack(side='left')
            else:button.state(['disabled'])
        failures=ttk.LabelFrame(dialog,text='등록 실패 — 랜덤')
        failures.grid(row=6,column=0,columnspan=3,padx=12,pady=6,sticky='ew')
        for i,clip in enumerate(self.sound_bank.get('failure',[])):
            ttk.Button(failures,text=clip.get('name',f'실패 {i+1}'),command=lambda c=clip:self.play_clip(c,voice_volume.get())).pack(side='left',padx=5,pady=4)
        def save_sound():
            try:
                updated=dict(self.settings,sound_enabled=master.get(),voice_volume=audio_volume(voice_volume.get()),capture_volume=audio_volume(capture_volume.get()),capture_delay_ms=float(delay.get()),
                    capture_sound=capture.get(),voice_characters={k:v.get() for k,v in enabled.items()})
                capture_delay(updated)
                if updated['capture_sound'] not in {c['id'] for c in self.sound_bank.get('capture',[])}:
                    raise ValueError('캡처 효과음을 선택하세요.')
                atomic_json(SETTINGS,updated);self.settings=updated
                winsound.PlaySound(None,0)
                dialog.destroy()
            except (ValueError,OSError) as exc:messagebox.showerror('소리 설정',str(exc),parent=dialog)
        ttk.Button(dialog,text='저장',command=save_sound).grid(row=7,column=0,columnspan=3,pady=12)

    def queue_records(self):
        files={p.with_suffix('.png') for p in CAPTURES.glob('*_KST_*.json')}|set(CAPTURES.glob('*_KST_*.png'))
        for file in sorted(files):
            try:
                record=self.read_event(file)
                state=record.get('queue_state','active')
                if state=='deleted' or record.get('registration',{}).get('status')=='marked':
                    continue
                pending=record.get('status')!='located' or record.get('register_requested')
                if pending or state=='held':
                    yield file,record
            except Exception as exc:
                yield file,{'status':'failed','error':'메타데이터 오류: '+str(exc)}

    def refresh_queue(self):
        selected=set(self.queue_tree.selection())
        self.queue_tree.delete(*self.queue_tree.get_children())
        for file,record in self.queue_records():
            held=record.get('queue_state')=='held'
            if self.queue_view.get()=='대기' and held or self.queue_view.get()=='보관' and not held:
                continue
            state='보관' if held else ('미인식' if record.get('status')=='failed' else '미등록')
            with self.lock:
                if file in self.active:
                    state='처리 중' if file==self.processing else '처리 대기'
            reason=record.get('registration',{}).get('reason') or record.get('error','')
            self.queue_tree.insert('', 'end',iid=file.name,text=file.stem,values=(KIND_NAMES.get(record.get('kind'),'—'),state,reason))
            if file.name in selected:
                self.queue_tree.selection_add(file.name)

    def act_on_file(self,file,action):
        with self.lock:
            if file==self.processing:
                return False
            with self.storage_lock:
                record=capture_metadata(file)
                if record.get('queue_state')=='deleted' or record.get('registration',{}).get('status')=='marked':
                    return False
                if action=='retry':
                    record['queue_state']='active'
                    if record.get('status')=='failed':
                        record['recognition_ui']='auto'
                    registration=record.get('registration')
                    if registration is not None:
                        registration['max_distance']=self.max_distance
                    atomic_json(file.with_suffix('.json'),record)
                else:
                    record['queue_state']=action
                    atomic_json(file.with_suffix('.json'),record)
        if action=='retry':
            self.enqueue(file)
        elif action=='deleted':
            self.recognize_jobs.put(('delete',file))
        else:
            self.recognize_jobs.put(None)
        return True

    def queue_action(self,action,all_items=False):
        names=self.queue_tree.get_children() if all_items else self.queue_tree.selection()
        if not names:
            return
        if action=='retry' and self.max_distance is None:
            messagebox.showerror('거리 제한','양수 거리 제한을 입력하세요.')
            return
        if action=='deleted' and not messagebox.askyesno('큐 항목 삭제',f'{len(names)}개 항목의 캡처·보관 이미지를 삭제합니다.\n공식 지도는 변경하지 않습니다. 삭제할까요?'):
            return
        done=0
        for name in names:
            try:
                done+=self.act_on_file(CAPTURES/name,action)
            except Exception as exc:
                self.messages.put(('error',str(exc)))
        self.status.set(f'{done}/{len(names)}개 요청 처리. 현재 인식·등록 중인 항목은 완료 후 조작할 수 있습니다.')
        self.refresh_queue()

    def preview_selected(self):
        selection=self.queue_tree.selection()
        if selection:
            file=CAPTURES/selection[0]
            record=self.read_event(file)
            path=(CAPTURES/record['archive']['preview']) if record.get('archive') else file
            if path.resolve().is_relative_to(CAPTURES.resolve()) and path.exists():
                os.startfile(path)

    def distance_changed(self,*args):
        try:
            value=float(self.distance_text.get())
            self.max_distance=value if math.isfinite(value) and value>0 else None
            if self.max_distance is not None:
                self.settings['max_distance']=self.max_distance
                atomic_json(SETTINGS,self.settings)
        except ValueError:
            self.max_distance=None

    def cleanup(self):
        with self.storage_lock:
            report=maintain_storage(CAPTURES)
        if report['archived']:
            self.messages.put(('info',f'원본 {len(report["archived"])}장 축소 보관 완료'))
        for error in report['errors']:
            self.messages.put(('error','원본 보존: '+error))

    def schedule_cleanup(self):
        if not self.closing:
            self.recognize_jobs.put(None)
            self.ui.after(3600000,self.schedule_cleanup)

    def poll(self):
        while not self.messages.empty():
            kind,text=self.messages.get()
            if kind=='setup_complete':
                self.settings['onboarding_complete']=True
                atomic_json(SETTINGS,self.settings)
                self.status.set(text)
                kind='info'
            elif kind=='setup_pending':
                self.status.set(text)
                kind='info'
            elif text.startswith('준비됨') and not self.login_ready.is_set():
                text='첫 실행: 공식 지도 로그인을 기다리고 있습니다.'
            self.queue_dirty=True
            if kind=='refresh':
                continue
            payload=notification(kind,text)
            if payload:
                self.toast.show(payload,self.settings['notification_hold'],self.settings['notification_fade'])
            clip=select_sound(self.sound_bank,self.settings,kind) if kind!='error' or text.startswith('미등록') else None
            if clip:self.play_clip(clip)
            if payload or self.status.get()=='시작 중…':
                self.status.set((payload[0]+': '+payload[1]) if payload else text)
            self.log.configure(state='normal')
            self.log.insert('end',f'{datetime.now().strftime("%H:%M:%S")} {text}\n')
            self.log.see('end')
            self.log.configure(state='disabled')
            with (ROOT/'inspection/capture-runtime.log').open('a',encoding='utf8') as log:
                log.write(f'{datetime.now().isoformat()} {kind}: {text}\n')
        if self.queue_dirty:
            self.refresh_queue()
            self.queue_dirty=False
        if self.closing and not self.capture_jobs.unfinished_tasks and not self.recognize_jobs.unfinished_tasks:
            if not self.shutdown_sent:
                self.shutdown_sent=True
                self.recognize_jobs.put(False)
            elif self.worker_closed.is_set():
                self.ui.destroy()
                return
        self.ui.after(100,self.poll)

    def close(self):
        self.stop.set()
        self.closing=True
        self.status.set('대기 중인 캡처와 인식을 마친 뒤 종료합니다.')

    def run(self):
        self.ui.mainloop()


def distribution_self_test():
    """Offline components plus an anonymous browser; no game input or account writes."""
    from official_mark import async_playwright
    report={'components':{},'ok':False}
    try:
        tk_root=tk.Tk();tk_root.withdraw();tk_root.update();tk_root.destroy()
        report['components']['tk']=True
        report['components']['timezone']=datetime.now(ZoneInfo('Asia/Seoul')).utcoffset().total_seconds()==32400
        bank=json.loads((SOUNDS/'manifest.json').read_text(encoding='utf8'))
        clips=[c for v in bank['success'].values() for c in v['clips']]+bank['failure']+bank['capture']
        for clip in clips:sound_path(clip)
        report['components']['audio_files']=len(clips)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            frame=Path(temporary)/'blank.png';Image.new('RGB',(256,144)).save(frame)
            probe=subprocess.run([str(EXE),'--locate',str(frame)],cwd=EXE.parent,capture_output=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
            report['components']['locator']=probe.returncode==2
            probe=subprocess.run([str(HDR_EXE)],capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
            report['components']['hdr_binary']=probe.returncode==1
            async def browser_probe():
                async with async_playwright() as runtime:
                    browser=await runtime.chromium.launch_persistent_context(str(Path(temporary)/'browser'),channel='chrome',headless=True)
                    value=await browser.pages[0].evaluate('6*7')
                    await browser.close()
                    return value==42
            report['components']['browser']=asyncio.run(browser_probe())
        report['ok']=all(report['components'].values())
    except Exception as exc:report['error']=str(exc)
    atomic_json(ROOT/'self-test.json',report)
    return 0 if report['ok'] else 1


if __name__=='__main__':
    if '--self-test' in sys.argv:sys.exit(distribution_self_test())
    try:
        App().run()
    except Exception as exc:
        messagebox.showerror('원신 수집품 캡처',str(exc))
