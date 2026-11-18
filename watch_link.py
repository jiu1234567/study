# -*- coding: utf-8 -*-
"""
微信群里出现腾讯文档收集表 → 自动嗅探链接 → 自动在后台 Chrome 里填好并提交。

【怎么拿链接】
实测结论：微信 4.x 是 Qt 自绘（MMUIRenderSubWindowHW），UIA 树里只有一个空壳窗口，聊天文本一个字都读不到。
所以这里走两条路，谁先拿到算谁：

1) 剪贴板（0.1 秒，100% 准确）
   在微信里右键消息 →「复制链接」，或拖选文字 Ctrl+C。脚本立刻接管。

2) 后台截图 + OCR（约 3 秒，全自动）
   - 用 PrintWindow(PW_RENDERFULLCONTENT) 抓微信窗口：窗口被别的程序盖住也能抓，10ms，不抢焦点。
   - 先用 64x64 灰度指纹判断画面有没有变化，没变化就不 OCR，省 CPU。
   - 画面变了才 OCR，认出「腾讯文档/收集表/docs.qq/form/page…」的文字块：
       · 块在左侧会话列表里 → 别的群发来的，先点开那个会话
       · 块在右侧聊天区里   → 直接在这条消息上「拖选 + Ctrl+C」
   - 关键：URL 不是 OCR 认出来的，是微信自己复制到剪贴板的，所以不会把 0/O、1/l 认错。
     拖选拿不到再兜底：右键 → OCR 菜单 → 点「复制链接/复制」；还不行就把最近几条消息整块拖一遍。

拿到链接后：后台常驻的 Chrome（profile 持久化，只扫一次码）开新标签 → 等表单放开 → 填 → 提交。
实测：从收到链接到提示提交成功 1.0 秒（页面就绪 0.33s，刷新 1 次即可填 0.42s）。

用法：
    双击 watch.bat                          # 全自动
    python watch_link.py --detect-only      # 只嗅探链接，不填不交（先这样验证一次）
    python watch_link.py --no-ocr           # 只监听剪贴板
    python watch_link.py --dry-run          # 填好但不提交

配置在 watch_config.txt。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import queue
import re
import sys
import threading
import time

import numpy as np
import win32api
import win32clipboard
import win32con
import win32gui
import win32ui
from PIL import Image, ImageGrab

# 高分屏必须声明 DPI 感知，否则 GetWindowRect / SetCursorPos / 截图 三套坐标系会错位
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from auto_tencent import (
    build_driver,
    fill_answers,
    login_flow,
    submit_form,
    wait_form_ready,
    wait_page_settle,
)

FORM_RE = re.compile(r"https?://docs\.qq\.com/form/page/[A-Za-z0-9_\-]+", re.I)
ID_RE = re.compile(r"/form/page/([A-Za-z0-9_\-]+)", re.I)
DEFAULT_KEYWORDS = ["腾讯文档", "收集表", "docs.qq", "form/page", "问卷", "登记表", "报名表"]
WECHAT_CLASS = "Qt51514QWindowIcon"
user32 = ctypes.windll.user32


def log(msg: str = "") -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


# ---------------------------------------------------------------- 剪贴板

def read_clipboard() -> str:
    for _ in range(3):
        try:
            win32clipboard.OpenClipboard()
            try:
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return ""


def write_clipboard(text: str) -> None:
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass


# ---------------------------------------------------------------- 微信窗口

def find_wechat_hwnd():
    """找微信主窗口。注意不能只认可见窗口：微信最小化到托盘时 IsWindowVisible 会变 False。"""
    found = []

    def cb(hwnd, _):
        if win32gui.GetClassName(hwnd) != WECHAT_CLASS:
            return
        if win32gui.GetWindowText(hwnd) in ("微信", "Weixin"):
            found.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def window_rect(hwnd):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.6)
    return win32gui.GetWindowRect(hwnd)


def ensure_visible(hwnd, w=1000, h=720):
    """确保微信窗口落在虚拟桌面内（多显示器也认）。太靠屏幕外就摆回主屏固定尺寸。"""
    vx, vy = win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77)
    vw, vh = win32api.GetSystemMetrics(78), win32api.GetSystemMetrics(79)
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.8)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    inside = (left >= vx - 8 and top >= vy - 8
              and right <= vx + vw + 8 and bottom <= vy + vh + 8)
    outside = (not inside) or (right - left < 500) or (bottom - top < 350)
    if outside:
        x, y = max(sw - w - 20, 10), max(sh - h - 60, 10)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, x, y, w, h,
                              win32con.SWP_SHOWWINDOW)
        time.sleep(0.9)
    return win32gui.GetWindowRect(hwnd)


def capture(hwnd):
    """后台抓图：被遮挡也能抓，不抢焦点。"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w < 200 or h < 200:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hwnd_dc)
    dc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    dc.SelectObject(bmp)
    user32.PrintWindow(hwnd, dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    info = bmp.GetInfo()
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    dc.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    return img


def fingerprint(img) -> str:
    """64x64 灰度指纹，用来判断画面有没有新内容（避开底部输入框的闪烁）。"""
    w, h = img.size
    crop = img.crop((0, 55, w, max(56, h - 90))).convert("L").resize((64, 64))
    return hashlib.md5(np.asarray(crop).tobytes()).hexdigest()


def ocr_blocks(ocr, img):
    arr = np.array(img)[:, :, ::-1].copy()  # RGB -> BGR
    res, _ = ocr(arr)
    out = []
    for box, text, _s in (res or []):
        x1, y1 = min(p[0] for p in box), min(p[1] for p in box)
        x2, y2 = max(p[0] for p in box), max(p[1] for p in box)
        text = (text or "").strip()
        if text:
            out.append((float(y1), float(x1), float(x2), float(y2), text))
    return out


def screen_grab(box):
    return ImageGrab.grab(bbox=box, all_screens=True)


def bring_front(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    time.sleep(0.25)


def drag_select(rect, block, pad=26):
    y1, x1, x2, y2, _ = block
    sx1, sy1 = int(rect[0] + x1) + 2, int(rect[1] + max(y1 - pad, 0)) + 2
    sx2, sy2 = int(rect[0] + x2) - 2, int(rect[1] + y2 + pad) - 2
    win32api.SetCursorPos((sx1, sy1))
    time.sleep(0.12)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    for k in range(1, 11):
        win32api.SetCursorPos((sx1 + (sx2 - sx1) * k // 10, sy1 + (sy2 - sy1) * k // 10))
        time.sleep(0.012)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.2)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(0x43, 0, 0, 0)
    time.sleep(0.03)
    win32api.keybd_event(0x43, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.25)


def right_click_copy(ocr, rect, block):
    y1, x1, x2, y2, _ = block
    cx, cy = int(rect[0] + (x1 + x2) / 2), int(rect[1] + (y1 + y2) / 2)
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    time.sleep(0.5)

    box = (cx - 30, cy - 40, cx + 320, cy + 420)
    hit = next((b for b in ocr_blocks(ocr, screen_grab(box)) if "复制" in b[4]), None)
    if not hit:
        win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
        win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
        return ""
    win32api.SetCursorPos((int(box[0] + hit[1]) + 25, int(box[1] + hit[0]) + 8))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.45)
    return read_clipboard()


# ---------------------------------------------------------------- 填表工人

class Filler(threading.Thread):
    def __init__(self, args):
        super().__init__(daemon=True, name="filler")
        self.args = args
        self.q = queue.Queue()
        self.done = set()
        self.driver = None
        self.ready = threading.Event()

    def offer(self, url: str):
        m = ID_RE.search(url)
        fid = m.group(1) if m else url
        if fid in self.done:
            log(f"[跳过] 已处理过 {fid}")
            return
        self.done.add(fid)
        self.q.put(url)

    def run(self):
        log("[浏览器] 启动 Chrome 并复用登录态（profile 目录）")
        self.driver = build_driver(self.args.browser, self.args.profile, False)
        self.ready.set()
        while True:
            url = self.q.get()
            if url is None:
                return
            try:
                self.handle(url)
            except Exception as exc:
                log(f"[填表出错] {type(exc).__name__}: {str(exc)[:200]}")

    def handle(self, url: str):
        d = self.driver
        t0 = time.time()
        log(f"[开页] {url}")
        d.switch_to.new_window("tab")
        d.get(url)
        state = wait_page_settle(d, 15)
        if state == "anon":
            log("[登录] 未登录，需要扫一次码（之后免扫）")
            login_flow(d, "auto", "wechat", self.args.login_timeout)
        log(f"[开页] 就绪 {time.time() - t0:.2f}s")

        els, attempts = wait_form_ready(d, self.args.open_timeout, self.args.reload_interval)
        if not els:
            log(f"[放弃] 等 {self.args.open_timeout:.0f}s 表单没放开")
            return
        log(f"[抢填] 刷新 {attempts} 次后表单可填 {time.time() - t0:.2f}s")

        fill_answers(d, self.args.text, self.args.radio, self.args.check)
        log(f"[抢填] 填完 {time.time() - t0:.2f}s")
        if self.args.dry_run:
            log("[演练] 不提交，窗口留着")
            return
        ok = submit_form(d, 15)
        log(f"[结果] {'提交成功' if ok else '未确认'} 总耗时 {time.time() - t0:.2f}s")
        if ok:
            try:
                d.close()
                d.switch_to.window(d.window_handles[0])
            except Exception:
                pass


# ---------------------------------------------------------------- 嗅探

class Watcher:
    def __init__(self, args, sink):
        self.args = args
        self.sink = sink
        self.auto_open = getattr(args, "auto_open", False)
        self.keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        self.seen_text = set()
        self.seen_url = set()
        self.ocr = None
        self.hwnd = None
        self.last_fp = None
        self.last_full_ocr = 0.0

    # ---- 剪贴板：最快的一条路
    def poll_clipboard(self):
        text = read_clipboard()
        if not text:
            return
        for m in FORM_RE.finditer(text):
            url = m.group(0)
            if url in self.seen_url:
                continue
            self.seen_url.add(url)
            log(f"[剪贴板] 抓到链接 {url}")
            self.sink.offer(url)

    def init_ocr(self):
        from rapidocr_onnxruntime import RapidOCR

        self.ocr = RapidOCR()
        self.hwnd = find_wechat_hwnd()
        if not self.hwnd:
            log("[微信] 没找到微信窗口（进程没开？）—— OCR 关闭，剪贴板监听继续")
            return False
        rect = ensure_visible(self.hwnd)
        img = capture(self.hwnd)
        if img is None:
            log(f"[微信] 抓不到窗口内容（rect={rect}）—— OCR 关闭")
            return False
        log(f"[微信] 窗口 {rect}，后台抓图 {img.size}，开始盯")
        ocr_blocks(self.ocr, img)  # 预热，第一次推理要 3 秒
        return True

    def click_conversation(self, rect, block):
        win32gui.SetForegroundWindow(self.hwnd)
        time.sleep(0.2)
        rect = win32gui.GetWindowRect(self.hwnd)
        cx = int(rect[0] + (block[1] + block[2]) / 2)
        cy = int(rect[1] + (block[0] + block[3]) / 2)
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.8)
        return win32gui.GetWindowRect(self.hwnd)

    def try_copy(self, rect, block):
        old = read_clipboard()
        write_clipboard("")
        drag_select(rect, block)
        text = read_clipboard()
        if not FORM_RE.search(text or ""):
            text = right_click_copy(self.ocr, rect, block) or text
        write_clipboard(old)
        return text or ""

    def open_card(self, block):
        """点开卡片/链接：单次左键点击消息块中心（小程序卡片点一下就打开）。"""
        bring_front(self.hwnd)
        rect = win32gui.GetWindowRect(self.hwnd)
        cx = int(rect[0] + (block[1] + block[2]) / 2)
        cy = int(rect[1] + (block[0] + block[3]) / 2)
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.15)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        log(f"[点击] 已点开 {block[4][:24]!r}")
        time.sleep(1.2)

    def find_miniprogram_hwnd(self):
        found = []

        def cb(hwnd, _):
            try:
                if "Chrome_WidgetWin" in win32gui.GetClassName(hwnd) \
                        and "腾讯文档" in win32gui.GetWindowText(hwnd):
                    found.append(hwnd)
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)
        return found[0] if found else None

    def try_skip_ad(self, wait_s=8.0):
        """腾讯文档小程序常带几秒开屏广告：只点「跳过」按钮（左上角），不点广告本身。"""
        started = time.time()
        while time.time() - started < wait_s:
            time.sleep(0.7)
            hwnd = self.find_miniprogram_hwnd()
            if not hwnd or win32gui.IsIconic(hwnd):
                continue
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] < 300 or rect[3] - rect[1] < 300:
                continue
            img = capture(hwnd)
            if img is None:
                continue
            h = img.size[1]
            # 只在顶部区域找带「跳过」字样的按钮；按 y、x 排序，取最靠上（通常左上角）的那个
            skips = [b for b in ocr_blocks(self.ocr, img)
                     if b[0] <= h * 0.3 and "跳过" in b[4]]
            if not skips:
                continue
            b = sorted(skips, key=lambda t: (t[0], t[1]))[0]
            cx = int(rect[0] + (b[1] + b[2]) / 2)
            cy = int(rect[1] + (b[0] + b[3]) / 2)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.25)
            win32api.SetCursorPos((cx, cy))
            time.sleep(0.1)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            log(f"[广告] 已点「跳过」({b[4][:10]!r})，等文档页加载")
            time.sleep(2.0)
            return True
        log("[广告] 顶部没找到「跳过」按钮（可能没有广告，或广告已结束）")
        return False

    def poll_ocr(self):
        img = capture(self.hwnd)
        if img is None:
            return
        fp = fingerprint(img)
        changed = fp != self.last_fp
        self.last_fp = fp
        if not changed and time.time() - self.last_full_ocr < 10:
            return
        self.last_full_ocr = time.time()

        w, h = img.size
        blocks = ocr_blocks(self.ocr, img)
        hits = [b for b in blocks if any(k in b[4] for k in self.keywords)]
        if not hits:
            return
        hits.sort(key=lambda b: b[0], reverse=True)

        # 左侧会话列表命中：别的群发来的 → 点开它
        side = [b for b in hits if b[2] < w * 0.42]
        if side:
            log(f"[OCR] 会话列表命中 {side[0][4][:24]!r}，点开该会话")
            rect = self.click_conversation(win32gui.GetWindowRect(self.hwnd), side[0])
            img = capture(self.hwnd)
            if img is None:
                return
            blocks = ocr_blocks(self.ocr, img)
            hits = [b for b in blocks
                    if any(k in b[4] for k in self.keywords) and b[1] > w * 0.42]
            hits.sort(key=lambda b: b[0], reverse=True)
            if not hits:
                log("[OCR] 打开会话后没看到文档消息（卡片或已滚出视野）")
                return

        for b in hits[:3]:
            key = hashlib.md5(b[4].encode("utf-8")).hexdigest()
            if key in self.seen_text:
                continue
            self.seen_text.add(key)
            log(f"[OCR] 聊天区命中 {b[4][:36]!r}")

            # 模式一：只负责“点开”，让文档进「最近文档」，由 watch_docs 接手填写
            if self.auto_open:
                self.open_card(b)
                self.try_skip_ad()
                log("[OCR] 已点开。文档会出现在「最近文档」里，watch_docs 会接手填写")
                return

            # 模式二：自己从消息里抠链接（文本链接/链接卡片才有效）
            bring_front(self.hwnd)
            text = self.try_copy(win32gui.GetWindowRect(self.hwnd), b)
            m = FORM_RE.search(text or "")
            if m:
                url = m.group(0)
                log(f"[OCR] 拿到链接 {url}")
                if url not in self.seen_url:
                    self.seen_url.add(url)
                    self.sink.offer(url)
                return
            log(f"[OCR] 这条没取到链接：{(text or '')[:50]!r}")
            if "[小程序]" in (text or ""):
                log("[OCR] 这是「腾讯文档」小程序卡片：微信不会把 URL 放进剪贴板，"
                    "脚本没法用。解决办法见 README「小程序卡片怎么办」。")
                self.skipped_card = True
                return

        # 兜底：把最近消息整块拖一遍（应对卡片、多行消息）；限流，20 秒最多一次
        if self.auto_open or getattr(self, "skipped_card", False):
            return
        if time.time() - getattr(self, "fallback_at", 0) < 20:
            return
        tail = [b for b in blocks if b[1] > w * 0.42 and b[3] > h * 0.45]
        if tail:
            block = (min(b[0] for b in tail), min(b[1] for b in tail),
                     max(b[2] for b in tail), max(b[3] for b in tail), "")
            log("[OCR] 兜底：拖选最近消息区域")
            bring_front(self.hwnd)
            m = FORM_RE.search(self.try_copy(win32gui.GetWindowRect(self.hwnd), block) or "")
            self.fallback_at = time.time()
            if m and m.group(0) not in self.seen_url:
                self.seen_url.add(m.group(0))
                log(f"[OCR] 兜底拿到 {m.group(0)}")
                self.sink.offer(m.group(0))

    def run(self):
        use_ocr = not self.args.no_ocr and self.init_ocr()
        log("[就绪] 监听中。Ctrl+C 退出")
        while True:
            self.poll_clipboard()
            if use_ocr:
                try:
                    self.poll_ocr()
                except Exception as exc:
                    log(f"[OCR] 异常 {type(exc).__name__}: {str(exc)[:150]}")
            time.sleep(self.args.poll_ms / 1000.0)


# ---------------------------------------------------------------- 入口

def load_cfg(path: str) -> dict:
    cfg = {"text": [], "radio": [], "check": []}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip().lower(), v.strip()
                if k in ("text", "radio", "check"):
                    cfg[k].append(v)
                elif k:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg


class NullSink:
    def offer(self, url: str):
        log(f"[只嗅探] 命中链接：{url}")


def main() -> int:
    here = __file__.rsplit("\\", 1)[0]
    ap = argparse.ArgumentParser(description="微信腾讯文档：自动嗅探 + 自动填表")
    ap.add_argument("--config", default=here + "\\watch_config.txt")
    ap.add_argument("--text", action="append", default=[])
    ap.add_argument("--radio", action="append", default=[])
    ap.add_argument("--check", action="append", default=[])
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge"])
    ap.add_argument("--profile", default="profile")
    ap.add_argument("--poll-ms", type=int, default=700)
    ap.add_argument("--open-timeout", type=float, default=120)
    ap.add_argument("--reload-interval", type=float, default=0.5)
    ap.add_argument("--login-timeout", type=float, default=300)
    ap.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS))
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--detect-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto-open", action="store_true",
                    help="检测到文档消息/卡片就点开它（配合 watch_docs 用，让文档进「最近文档」）")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    provided = {a.split("=")[0] for a in sys.argv[1:] if a.startswith("--")}
    for key in ("text", "radio", "check"):
        if "--" + key not in provided and cfg[key]:
            setattr(args, key, cfg[key])
    for key in ("browser", "profile", "keywords"):
        if "--" + key.replace("_", "-") not in provided and cfg.get(key):
            setattr(args, key, cfg[key])
    for key in ("poll_ms", "open_timeout", "reload_interval", "login_timeout"):
        if "--" + key.replace("_", "-") not in provided and cfg.get(key):
            setattr(args, key, type(getattr(args, key))(cfg[key]))
    for flag, key in (("--detect-only", "detect_only"), ("--dry-run", "dry_run"),
                      ("--auto-open", "auto_open")):
        if flag not in provided and str(cfg.get(key, "")).lower() in ("1", "true", "yes", "y"):
            setattr(args, key, True)

    if not (args.text or args.radio or args.check):
        raise SystemExit("watch_config.txt 里至少要有一个答案（text=/radio=/check=）")

    log(f"[配置] 答案 text={args.text} radio={args.radio} check={args.check}")
    log(f"[配置] 关键词 {args.keywords}")

    if args.detect_only:
        log("[模式] 只嗅探链接，不开浏览器")
        sink = NullSink()
    else:
        sink = Filler(args)
        sink.start()
        sink.ready.wait(timeout=180)
        if args.dry_run:
            log("[模式] 演练：会填但不提交")

    try:
        Watcher(args, sink).run()
    except KeyboardInterrupt:
        log("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
