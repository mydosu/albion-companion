"""阿尔比恩最近活动摆件。只读脑端写的快照 state/widget.json；文件动作只显示路径与时间。"""
import ctypes
from ctypes import wintypes
import json
import os
import re
import socket
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
POS_FILE = os.path.join(HERE, "albion_status_widget_pos.json")
W, H, DETAIL_H = 320, 160, 236
GLASS_ALPHA = 202
FROST = 0.15
GLASS_BASE = (218, 230, 242)
FROST_MS = 50
FROST_SCALE = 6
PINK = "#e95ba5"
FONT_PATH = os.path.join(HERE, "fonts", "ZCOOLKuaiLe-Regular.ttf")
PORT = 47654
CLASS_NAME = "AlbionStatusGlassWidget"
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x80000, 0x80, 0x8
WM_DESTROY, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 2, 0x201, 0x202, 0x200
WM_RBUTTONUP, WM_TIMER, WM_READY = 0x205, 0x113, 0x8001
WM_COPYDATA = 0x4A
WM_SETCURSOR = 0x20

# 脑宿主（integrations/pi-brain/bin/widget_snapshot.py）每 5 秒原子写一次；这里也只按 5 秒轮询，最坏延迟约 10 秒。
SNAPSHOT = os.environ.get("ALBION_WIDGET_SNAPSHOT", r"\\wsl.localhost\Ubuntu\root\albion-pi\state\widget.json")
STALE_S = 60   # 超过这么久没更新就当脑没在写


def fetch(path=None):
    with open(path or SNAPSHOT, encoding="utf-8") as f:
        data = json.load(f)
    stamp = data.get("generated_at") if isinstance(data, dict) else None
    if not isinstance(stamp, (int, float)) or time.time() - stamp > STALE_S:
        raise ValueError("快照过期")
    return data


def fetch_error(last_ok):
    return "暂时读不到最近记录 · " + ("上次成功 " + ago(time.time() - last_ok) if last_ok else "还没读到过")


def clean(value, limit=38):
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or ""))
    value = re.sub(r"(?i)(bearer\s+\S+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)", "[已隐藏]", value)
    value = " ".join(value.split())
    return value[:limit] + ("…" if len(value) > limit else "")


def ago(seconds):
    if seconds is None:
        return "暂无记录"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds//60} 分前"
    if seconds < 86400:
        return f"{seconds//3600} 小时前"
    return f"{seconds//86400} 天前"


def activity_age(data):
    stamp = (data or {}).get("last_activity_at")
    return max(0, time.time()-stamp) if isinstance(stamp, (int, float)) else None


# 「最近动作」= 她动了哪个文件（读或写都算动过），报相对路径 + 多久以前；没动过文件就是"暂无"。
def action_text(data):
    """返回 (这一行给什么文字, 对应时间戳)。"""
    work = (data or {}).get("work") or {}
    stamp = work.get("mtime")
    text = clean(work.get("relative_path") or "暂无", 512)
    age = ago(max(0, time.time() - stamp)) if isinstance(stamp, (int, float)) else ""
    return text + (" · " + age if age else ""), stamp


def font(size):
    paths = [FONT_PATH, r"C:\Windows\Fonts\MiSans-Regular.otf",
             r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\arial.ttf"]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_card(data=None, detail=False, error=None, transparent=False,
                scroll_px=0, overflows=None):
    height = DETAIL_H if detail else H
    scale = 3
    im = Image.new("RGBA", (W*scale, height*scale), (0,0,0,0))
    box = (2*scale,2*scale,(W-3)*scale,(height-3)*scale)
    if not transparent:
        frosted = tuple(round(c*(1-FROST)+255*FROST) for c in GLASS_BASE)
        glass = Image.new("RGB", (W*scale, height*scale), frosted)
        gd = ImageDraw.Draw(glass)
        fade_height = max(1, int(height*scale*0.66))
        for y in range(fade_height):
            glow = 0.12 * (1-y/fade_height)**2
            tint = tuple(round(c+(255-c)*glow) for c in frosted)
            gd.line((0,y,W*scale-1,y), fill=tint)
        mask = Image.new("L", (W*scale, height*scale), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=31*scale, fill=GLASS_ALPHA)
        glass.putalpha(mask)
        im.alpha_composite(glass)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(box, radius=31*scale,
                        outline=(235,246,255,185), width=2*scale)
    d.rounded_rectangle((7*scale,7*scale,(W-8)*scale,(height-8)*scale),
                        radius=27*scale, outline=(255,255,255,50), width=scale)

    def line(y, label, value, size=17, color=(255,255,255,255), gap_px=0):
        face = font(size*scale)
        label, value = clean(label, 48), clean(value, 512)
        x = 18*scale
        d.text((x,y*scale), label, font=face, fill=color,
               stroke_width=2, stroke_fill=PINK)
        x += int(d.textlength(label, font=face)) + gap_px*scale
        visible = (W-11)*scale-x
        if visible <= 0:
            return
        width = d.textlength(value, font=face)
        if width <= visible:
            d.text((x,y*scale), value, font=face, fill=color,
                   stroke_width=2, stroke_fill=PINK)
            return
        if overflows is not None:
            overflows.append(y)
        # 标签保持固定；只在剩余宽度内循环移动完整内容，不再截成省略号。
        gap = 40*scale
        cycle = int(width)+gap
        offset = int(scroll_px*scale) % cycle
        strip = Image.new("RGBA", (int(visible), (size+7)*scale), (0,0,0,0))
        sd = ImageDraw.Draw(strip)
        for dx in (-offset, cycle-offset):
            sd.text((dx,0), value, font=face, fill=color,
                    stroke_width=2, stroke_fill=PINK)
        im.alpha_composite(strip, (x,y*scale))

    age = activity_age(data)
    line(18, "阿尔比恩 · 最近活动  ", ago(age), 19)
    if error:
        line(52, "", clean(error, 36))
    elif not data:
        line(52, "", "正在读取最近记录…")
    else:
        tool = data.get("last_tool") or {}
        tool_name = clean(tool.get("tool_name") or data.get("last_action") or "暂无", 512)
        line(52, "最近工具", tool_name, gap_px=11)
        text, stamp = action_text(data)
        line(83, "最近动作", text, gap_px=11)
        today = data.get("today_counts") or {}
        emotion = clean(data.get("emotion") or "neutral", 512)
        line(114, "心情  ", f"{emotion} · 今日 {today.get('message_count',0)} 条 / {today.get('tool_call_count',0)} 次工具")
        if detail:
            d.line((18*scale,148*scale,(W-18)*scale,148*scale), fill=(255,255,255,80), width=scale)
            fmt = lambda ts: time.strftime("%m-%d %H:%M:%S",time.localtime(ts)) if isinstance(ts,(int,float)) else "无"
            line(159, "最近工具时间  ", fmt(tool.get("ts")), 15)
            line(184, "最近动作时间  ", fmt(stamp), 15)
            counts = data.get("counts") or {}
            line(209, "会话累计  ", f"{counts.get('message_count',0)} 条 / {counts.get('tool_call_count',0)} 次工具", 15)
    return im.resize((W,height), Image.Resampling.LANCZOS)


def check():
    sample = {"last_activity_at":time.time()-15, "last_tool":{"tool_name":"terminal","ts":time.time()},
              "work":{"relative_path":"游戏/读着走.txt","mtime":time.time()-60},
              "emotion":"curious","today_counts":{"message_count":3,"tool_call_count":2},
              "counts":{"message_count":98,"tool_call_count":14}}
    image = render_card(sample)
    alpha = image.getchannel("A")
    assert image.mode == "RGBA" and image.size == (W,H)
    assert all(alpha.getpixel(p)==0 for p in ((0,0),(W-1,0),(0,H-1),(W-1,H-1)))
    assert alpha.getpixel((W//2,H//2)) == GLASS_ALPHA
    assert any(alpha.histogram()[1:255])
    assert clean("hello\nBearer abc",40) == "hello [已隐藏]"
    assert ago(15) == "15 秒前"
    # 最近动作 = 她动了哪个文件（读或写）
    # （2026-10-08 修：原来只认 write/patch，且读文件不记路径 → 她读书、翻本子时这一行一直卡在旧文件上）
    now = time.time()
    assert action_text({"work":{"relative_path":"游戏/读着走.txt","mtime":now}})[0].startswith("游戏/读着走.txt")
    assert action_text({"work":{"relative_path":"家/阿尔比恩的食谱本.md","mtime":now-3600}})[0].startswith("家/阿尔比恩的食谱本.md · 1 小时前")
    assert action_text({"work":{}})[0] == "暂无"
    # 只读脑端快照：新鲜的照用，过期/缺失/坏文件都报错，错误行带上次成功时间
    import tempfile
    snap = os.path.join(tempfile.mkdtemp(prefix="widget-check-"), "widget.json")
    try:
        fetch(snap)
        raise AssertionError("缺文件应报错")
    except OSError:
        pass
    for bad in ("{", json.dumps({"generated_at":now-STALE_S-5}), json.dumps({"emotion":"x"})):
        open(snap,"w",encoding="utf-8").write(bad)
        try:
            fetch(snap)
            raise AssertionError("应报错：" + bad)
        except ValueError:
            pass
    open(snap,"w",encoding="utf-8").write(json.dumps(dict(sample,generated_at=now)))
    assert fetch(snap)["emotion"] == "curious"
    assert fetch_error(now-120) == "暂时读不到最近记录 · 上次成功 2 分前"
    assert fetch_error(None).endswith("还没读到过")
    print("ok")


def _compose(overlay, backdrop):
    height = overlay.height
    mask = Image.new("L", (W, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((2, 2, W-3, height-3), radius=31,
                                            fill=GLASS_ALPHA)
    base = Image.blend(backdrop.convert("RGB"), Image.new("RGB", (W, height),
                       (255, 255, 255)), FROST).convert("RGBA")
    base.putalpha(mask)
    return Image.alpha_composite(base, overlay)


def _frame_delta(a, b):
    return float(np.abs(np.asarray(a, dtype=np.int16) -
                        np.asarray(b, dtype=np.int16)).mean())


def _frost_setup(overlay):
    size = (max(1, W//FROST_SCALE), max(1, overlay.height//FROST_SCALE))
    content = overlay.getchannel("A").point(lambda a: 255 if a > 8 else 0)
    content = content.resize(size, Image.Resampling.BILINEAR)
    radius = max(3, 17//FROST_SCALE)
    if radius % 2 == 0:
        radius += 1
    content = content.point(lambda a: 255 if a > 8 else 0)
    content = content.filter(ImageFilter.MaxFilter(radius))
    valid = (np.asarray(content) == 0).astype(np.float32)
    blur = max(1, 9//FROST_SCALE)
    mask = Image.fromarray(np.uint8(valid*255), "L")
    weights = np.asarray(mask.filter(ImageFilter.GaussianBlur(blur)), dtype=np.float32)
    inverse = 255.0/np.maximum(weights[:, :, None], 1)
    confidence = np.minimum(weights[:, :, None]/96.0, 1.0)
    return size, valid, inverse, confidence, blur


def _frost_fast(small, setup, previous=None):
    size, valid, inverse, confidence, blur = setup
    old = previous.resize(size, Image.Resampling.BILINEAR) if previous else None
    rgb = np.asarray(small, dtype=np.float32)
    if old is not None:
        old_rgb = np.asarray(old, dtype=np.float32)
        alpha = old_rgb[:, :, 3:4]/255.0
        rgb = np.clip((rgb-old_rgb[:, :, :3]*alpha)/np.maximum(1.0-alpha, 0.08), 0, 255)
    weighted = Image.fromarray(np.uint8(rgb*valid[:, :, None]), "RGB")
    colors = np.asarray(weighted.filter(ImageFilter.GaussianBlur(blur)), dtype=np.float32)
    estimate = np.clip(colors*inverse, 0, 255)
    fallback = rgb[valid > 0].mean(axis=0)
    filled = np.uint8(np.clip(estimate*confidence+fallback*(1.0-confidence), 0, 255))
    return Image.fromarray(filled, "RGB")


user32 = ctypes.windll.user32
gdi = ctypes.windll.gdi32
kernel = ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x",wintypes.LONG),("y",wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx",wintypes.LONG),("cy",wintypes.LONG)]


class BLEND(ctypes.Structure):
    _fields_ = [("op",ctypes.c_byte),("flags",ctypes.c_byte),
                ("constant",ctypes.c_byte),("format",ctypes.c_byte)]


class RECT(ctypes.Structure):
    _fields_ = [("left",wintypes.LONG),("top",wintypes.LONG),
                ("right",wintypes.LONG),("bottom",wintypes.LONG)]


class HEADER(ctypes.Structure):
    _fields_ = [("size",wintypes.DWORD),("width",wintypes.LONG),("height",wintypes.LONG),
                ("planes",wintypes.WORD),("bpp",wintypes.WORD),("compression",wintypes.DWORD),
                ("bytes",wintypes.DWORD),("xppm",wintypes.LONG),("yppm",wintypes.LONG),
                ("colors",wintypes.DWORD),("important",wintypes.DWORD)]


class INFO(ctypes.Structure):
    _fields_ = [("header",HEADER),("color",wintypes.DWORD)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style",wintypes.UINT),("proc",WNDPROC),("cls_extra",ctypes.c_int),
                ("wnd_extra",ctypes.c_int),("instance",wintypes.HINSTANCE),
                ("icon",wintypes.HICON),("cursor",wintypes.HANDLE),("background",wintypes.HBRUSH),
                ("menu",wintypes.LPCWSTR),("name",wintypes.LPCWSTR)]


class COPYDATA(ctypes.Structure):
    _fields_ = [("tag",ctypes.c_size_t),("size",wintypes.DWORD),("data",ctypes.c_void_p)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = (wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASS),)
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,
    wintypes.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,
    wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = (wintypes.LPCWSTR,wintypes.LPCWSTR)
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = (POINT,)
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND,wintypes.UINT)
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = (wintypes.HWND,wintypes.UINT)
user32.GetClassNameW.argtypes = (wintypes.HWND,wintypes.LPWSTR,ctypes.c_int)


def _covered_by_window(hwnd,x,y,w,h):
    foreign = []
    for fx in (0.15,0.85):
        for fy in (0.2,0.8):
            hit = user32.WindowFromPoint(POINT(x+int(w*fx),y+int(h*fy)))
            if not hit or hit == hwnd:
                continue
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hit,name,128)
            if name.value == "Windows.UI.Core.CoreWindow":
                return True
            # shell 的弹层（快速设置=ControlCenterWindow 等）走独立 band、由 DWM 直接合成在最上层，
            # 不进 HWND z 序链 ⇒ 上面那套「谁在我上面」永远查不到它。命中窗口的顶层根
            # 是 topmost 且不是我们自己 ⇒ 它一定画在我们上面。
            if user32.GetWindowLongW(user32.GetAncestor(hit, 2), -20) & 0x8:
                return True
            # Chromium 内容窗口（同席、Hermes 桌面端等）照常算遮挡；只放过 AIRI 透明模型层，
            # 它盖在上面也看不见。以前整类放过，这些窗口盖住挂件时还在截它们的画面反算背景 ⇒ 疯狂闪烁、变色。
            root_title = ctypes.create_unicode_buffer(16)
            user32.GetWindowTextW(user32.GetAncestor(hit, 2), root_title, 16)
            if name.value != "Chrome_RenderWidgetHostHWND" or root_title.value != "AIRI":
                foreign.append(hit)
    if not foreign:
        return False
    above = set()
    other = user32.GetWindow(hwnd,3)  # GW_HWNDPREV
    while other:
        above.add(other)
        other = user32.GetWindow(other,3)
    return any(hit in above or user32.GetAncestor(hit,2) in above for hit in foreign)
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = (wintypes.HWND,)
user32.ReleaseDC.argtypes = (wintypes.HWND,wintypes.HDC)
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = (wintypes.HINSTANCE,ctypes.c_void_p)
user32.SetCursor.argtypes = (wintypes.HANDLE,)
user32.UpdateLayeredWindow.argtypes = (wintypes.HWND,wintypes.HDC,ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE),wintypes.HDC,ctypes.POINTER(POINT),wintypes.DWORD,
    ctypes.POINTER(BLEND),wintypes.DWORD)
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = (wintypes.HMENU,wintypes.UINT,ctypes.c_size_t,wintypes.LPCWSTR)
user32.TrackPopupMenu.argtypes = (wintypes.HMENU,wintypes.UINT,ctypes.c_int,ctypes.c_int,
    ctypes.c_int,wintypes.HWND,ctypes.c_void_p)
user32.DestroyMenu.argtypes = (wintypes.HMENU,)
user32.ShowWindow.argtypes = (wintypes.HWND,ctypes.c_int)
user32.SetWindowPos.argtypes = (wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,
    ctypes.c_int,ctypes.c_int,wintypes.UINT)
user32.SetCapture.argtypes = (wintypes.HWND,)
user32.DestroyWindow.argtypes = (wintypes.HWND,)
user32.PostMessageW.argtypes = (wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
user32.SetTimer.argtypes = (wintypes.HWND,ctypes.c_size_t,wintypes.UINT,ctypes.c_void_p)
user32.KillTimer.argtypes = (wintypes.HWND,ctypes.c_size_t)
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),wintypes.HWND,wintypes.UINT,wintypes.UINT)
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.SendMessageTimeoutW.argtypes = (wintypes.HWND,wintypes.UINT,wintypes.WPARAM,
    wintypes.LPARAM,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t))
gdi.CreateCompatibleDC.restype = wintypes.HDC
gdi.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi.StretchBlt.argtypes = (wintypes.HDC,ctypes.c_int,ctypes.c_int,ctypes.c_int,
    ctypes.c_int,wintypes.HDC,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.DWORD)
gdi.SetStretchBltMode.argtypes = (wintypes.HDC,ctypes.c_int)
gdi.CreateDIBSection.restype = wintypes.HBITMAP
gdi.CreateDIBSection.argtypes = (wintypes.HDC,ctypes.POINTER(INFO),wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),wintypes.HANDLE,wintypes.DWORD)
gdi.SelectObject.restype = wintypes.HGDIOBJ
gdi.SelectObject.argtypes = (wintypes.HDC,wintypes.HGDIOBJ)
gdi.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi.DeleteDC.argtypes = (wintypes.HDC,)
kernel.GetModuleHandleW.restype = wintypes.HMODULE


class _CardCapture:
    def __init__(self, height):
        self.full_height = height
        self.width,self.height = max(1,W//FROST_SCALE),max(1,height//FROST_SCALE)
        self.screen = user32.GetDC(None)
        self.dc = gdi.CreateCompatibleDC(self.screen)
        info = INFO()
        info.header.size = ctypes.sizeof(HEADER)
        info.header.width,info.header.height = self.width,-self.height
        info.header.planes,info.header.bpp = 1,32
        self.pixels = ctypes.c_void_p()
        self.bitmap = gdi.CreateDIBSection(self.screen,ctypes.byref(info),0,
                                            ctypes.byref(self.pixels),None,0)
        if not self.screen or not self.dc or not self.bitmap or not self.pixels.value:
            raise OSError("card capture allocation failed")
        self.previous = gdi.SelectObject(self.dc,self.bitmap)
        gdi.SetStretchBltMode(self.dc,4)

    def grab(self,x,y):
        if not gdi.StretchBlt(self.dc,0,0,self.width,self.height,self.screen,
                              x,y,W,self.full_height,0x00CC0020|0x40000000):
            raise OSError("StretchBlt failed")
        return Image.frombuffer("RGBA",(self.width,self.height),
                ctypes.string_at(self.pixels,self.width*self.height*4),
                "raw","BGRA",0,1).convert("RGB")

    def close(self):
        gdi.SelectObject(self.dc,self.previous)
        gdi.DeleteObject(self.bitmap)
        gdi.DeleteDC(self.dc)
        user32.ReleaseDC(None,self.screen)


class _CardUploader:
    def __init__(self,height):
        self.screen = user32.GetDC(None)
        self.dc = gdi.CreateCompatibleDC(self.screen)
        info = INFO()
        info.header.size = ctypes.sizeof(HEADER)
        info.header.width,info.header.height = W,-height
        info.header.planes,info.header.bpp = 1,32
        self.pixels = ctypes.c_void_p()
        self.bitmap = gdi.CreateDIBSection(self.dc,ctypes.byref(info),0,
                                            ctypes.byref(self.pixels),None,0)
        if not self.screen or not self.dc or not self.bitmap or not self.pixels.value:
            raise OSError("layered DIB allocation failed")
        self.previous = gdi.SelectObject(self.dc,self.bitmap)
        self.size,self.source,self.blend = SIZE(W,height),POINT(),BLEND(0,0,255,1)

    def upload(self,hwnd,image,x,y):
        raw = image.convert("RGBa").tobytes("raw","BGRa")
        ctypes.memmove(self.pixels,raw,len(raw))
        dest = POINT(x,y)
        if not user32.UpdateLayeredWindow(hwnd,self.screen,ctypes.byref(dest),
                    ctypes.byref(self.size),self.dc,ctypes.byref(self.source),
                    0,ctypes.byref(self.blend),2):
            raise OSError("UpdateLayeredWindow failed")

    def close(self):
        gdi.SelectObject(self.dc,self.previous)
        gdi.DeleteObject(self.bitmap)
        gdi.DeleteDC(self.dc)
        user32.ReleaseDC(None,self.screen)


class Widget:
    def __init__(self):
        user32.SetProcessDPIAware()
        self.detail = False
        self.drag = None
        self.loading = False
        self.data = None
        self.error = None
        self.last_ok = None
        overflows = []
        self.overlay = render_card(transparent=True,overflows=overflows)
        self.has_marquee = bool(overflows)
        self.marquee_started_at = time.monotonic()
        self.marquee_step = 0
        self.frost_setup = _frost_setup(self.overlay)
        self.image = None
        self.frost = None
        self.pending_frost = None
        self.last_motion_at = time.monotonic()
        self.last_screen_preview = None
        self.was_covered = False
        self.covered_overlay = None
        self.capture = _CardCapture(H)
        self.uploader = _CardUploader(H)
        try:
            pos = json.load(open(POS_FILE,encoding="utf-8"))
            self.x,self.y = int(pos["x"]),int(pos["y"])
        except (OSError,ValueError,KeyError,TypeError):
            self.x = user32.GetSystemMetrics(0)-W-28
            self.y = user32.GetSystemMetrics(1)-H-68
        self.proc = WNDPROC(self.handle)
        cls = WNDCLASS()
        cls.proc = self.proc
        cls.instance = kernel.GetModuleHandleW(None)
        cls.name = CLASS_NAME
        self.arrow = user32.LoadCursorW(None,ctypes.c_void_p(32512))
        cls.cursor = self.arrow
        if not user32.RegisterClassW(ctypes.byref(cls)):
            raise OSError("RegisterClassW failed")
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED|WS_EX_TOOLWINDOW,CLASS_NAME,"阿尔比恩最近活动",
            0x80000000,self.x,self.y,W,H,None,None,cls.instance,None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        self.paint()
        user32.ShowWindow(self.hwnd,4)
        user32.SetTimer(self.hwnd,2,FROST_MS,None)
        self.refresh()

    def paint(self):
        frost = self.frost
        try:
            if self.has_marquee:
                step = int(max(0,time.monotonic()-self.marquee_started_at-0.8)*35)
                if step != self.marquee_step:
                    self.overlay = render_card(self.data,self.detail,self.error,
                                               transparent=True,scroll_px=step)
                    self.frost_setup = _frost_setup(self.overlay)
                    self.last_screen_preview = None
                    self.marquee_step = step
            covered = _covered_by_window(self.hwnd,self.x,self.y,W,self.capture.full_height)
            if covered:
                self.was_covered = True
                if self.image is not None and self.covered_overlay is self.overlay:
                    return
                self.covered_overlay = self.overlay
                image = (_compose(self.overlay,frost.resize(self.overlay.size,Image.Resampling.BILINEAR))
                         if frost is not None else render_card(self.data,self.detail,self.error))
                self.uploader.upload(self.hwnd,image,self.x,self.y)
                self.image = image
                return
            if self.was_covered:
                self.last_screen_preview = None
                self.pending_frost = None
                self.was_covered = False
            self.covered_overlay = None
            preview = self.capture.grab(self.x,self.y)
            if (self.pending_frost is None and self.last_screen_preview is not None and
                    self.image is not None and _frame_delta(preview,self.last_screen_preview)<0.25):
                return
            self.last_screen_preview = preview
            frost = _frost_fast(preview,self.frost_setup,self.image)
            now = time.monotonic()
            if self.frost is not None:
                delta = _frame_delta(frost,self.frost)
                if delta > 18 and now-self.last_motion_at > 0.25 and not self.drag:
                    if self.pending_frost is None or _frame_delta(frost,self.pending_frost)>18:
                        self.pending_frost = frost
                        frost = self.frost
                    else:
                        self.pending_frost = None
                        self.last_motion_at = now
                else:
                    self.pending_frost = None
                    if delta > 1:
                        self.last_motion_at = now
                if frost is not self.frost:
                    frost = Image.blend(self.frost,frost,0.9 if self.drag else 0.75)
            image = _compose(self.overlay,frost.resize(self.overlay.size,Image.Resampling.BILINEAR))
        except Exception:
            frost = self.frost
            image = (_compose(self.overlay,frost.resize(self.overlay.size,Image.Resampling.BILINEAR))
                     if frost is not None else render_card(self.data,self.detail,self.error))
        self.uploader.upload(self.hwnd,image,self.x,self.y)
        self.image,self.frost = image,frost

    def rebuild(self):
        height = DETAIL_H if self.detail else H
        if self.capture.full_height != height:
            self.capture.close()
            self.uploader.close()
            self.capture = _CardCapture(height)
            self.uploader = _CardUploader(height)
            self.image,self.frost,self.pending_frost = None,None,None
        overflows = []
        self.overlay = render_card(self.data,self.detail,self.error,
                                   transparent=True,overflows=overflows)
        self.has_marquee = bool(overflows)
        self.marquee_started_at = time.monotonic()
        self.marquee_step = 0
        self.frost_setup = _frost_setup(self.overlay)
        self.last_screen_preview = None
        self.paint()

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        def work():
            try:
                data = fetch()
                self.data,self.error,self.last_ok = data,None,time.time()
            except (OSError,ValueError):
                self.error = fetch_error(self.last_ok)
            finally:
                self.loading = False
                user32.PostMessageW(self.hwnd,WM_READY,0,0)
        threading.Thread(target=work,daemon=True).start()

    def save_position(self):
        temp = POS_FILE+".tmp"
        try:
            with open(temp,"w",encoding="utf-8") as file:
                json.dump({"x":self.x,"y":self.y},file)
            os.replace(temp,POS_FILE)
        except OSError:
            pass

    def screenshot(self,path):
        screen = ImageGrab.grab(all_screens=True)
        vx,vy = user32.GetSystemMetrics(76),user32.GetSystemMetrics(77)
        height = DETAIL_H if self.detail else H
        screen.crop((self.x-vx,self.y-vy,self.x-vx+W,self.y-vy+height)).save(path)

    def handle(self,hwnd,msg,wp,lp):
        if msg == WM_SETCURSOR:
            user32.SetCursor(self.arrow)
            return 1
        if msg == WM_COPYDATA:
            packet = ctypes.cast(lp,ctypes.POINTER(COPYDATA)).contents
            if packet.tag != 1 or not packet.data or packet.size > 65536:
                return 0
            try:
                self.screenshot(ctypes.wstring_at(packet.data,packet.size//2).rstrip("\x00"))
                return 1
            except (OSError,ValueError):
                return 0
        if msg == WM_LBUTTONDOWN:
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            self.drag = (pt.x-self.x,pt.y-self.y,False)
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self.drag:
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            dx,dy,moved = self.drag
            x,y = pt.x-dx,pt.y-dy
            if moved or abs(x-self.x)+abs(y-self.y) >= 3:
                self.drag = (dx,dy,True)
                self.x,self.y = x,y
                user32.SetWindowPos(hwnd,None,x,y,0,0,0x0001|0x0004|0x0010)
            return 0
        if msg == WM_LBUTTONUP and self.drag:
            moved = self.drag[2]
            self.drag = None
            user32.ReleaseCapture()
            if moved:
                self.save_position()
                self.paint()
            else:
                self.detail = not self.detail
                user32.SetWindowPos(hwnd,None,self.x,self.y,W,DETAIL_H if self.detail else H,
                                    0x0004|0x0010)
                self.rebuild()
                self.refresh()
            return 0
        if msg == WM_RBUTTONUP:
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu,0,1,"退出")
            choice = user32.TrackPopupMenu(menu,2|0x100,pt.x,pt.y,0,hwnd,None)
            user32.DestroyMenu(menu)
            if choice == 1:
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_READY:
            self.rebuild()
            user32.KillTimer(hwnd,1)
            # 统一 5 秒轮询（原来近 5 分钟有活动 10 秒、否则 30 秒）；工具一调完就落库，最坏延迟约 10 秒。
            user32.SetTimer(hwnd,1,5*1000,None)
            return 0
        if msg == WM_TIMER:
            if wp == 1:
                self.refresh()
            elif wp == 2:
                self.paint()
            return 0
        if msg == WM_DESTROY:
            user32.KillTimer(hwnd,1)
            user32.KillTimer(hwnd,2)
            self.capture.close()
            self.uploader.close()
            self.save_position()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd,msg,wp,lp)

    def run(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg),None,0,0)>0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def request_shot(path):
    hwnd = user32.FindWindowW(CLASS_NAME,None)
    if not hwnd:
        raise SystemExit("摆件未运行")
    payload = ctypes.create_unicode_buffer(os.path.abspath(path))
    packet = COPYDATA(1,ctypes.sizeof(payload),ctypes.cast(payload,ctypes.c_void_p))
    result = ctypes.c_size_t()
    sent = user32.SendMessageTimeoutW(hwnd,WM_COPYDATA,0,ctypes.addressof(packet),
                                      2,5000,ctypes.byref(result))
    if not sent or result.value != 1:
        raise SystemExit("截图失败")
    print(os.path.abspath(path))


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif "--dump-json" in sys.argv:
        print(json.dumps(fetch(),ensure_ascii=False,separators=(",",":")))
    elif "--render-png" in sys.argv:
        idx = sys.argv.index("--render-png")
        path = os.path.abspath(sys.argv[idx+1])
        render_card().save(path)
        print(path)
    elif "--shot" in sys.argv:
        request_shot(sys.argv[sys.argv.index("--shot")+1])
    else:
        guard = socket.socket()
        try:
            guard.bind(("127.0.0.1",PORT))
        except OSError:
            sys.exit(0)
        guard.listen(1)
        try:
            Widget().run()
        finally:
            guard.close()
