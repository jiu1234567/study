# -*- coding: utf-8 -*-
"""
方案 D：不碰微信，直接盯腾讯文档「最近文档」——群里新分享过来的收集表会自己出现在列表里。

为什么换成这条路（你上一条反馈的问题，我都验证过）：
  1. 小程序卡片右键没有「复制链接」，微信只给标题，拿不到 URL（已实测）。
  2. 你复制出来的文本带前缀「【腾讯文档】会议」，直接粘到地址栏会变成搜索，所以“无法跳转”。
     本脚本用正则只抽 https://docs.qq.com/form/page/<ID>，前缀自动丢掉。
  3. 电脑端“不能填写”的真正原因是没登录：未登录打开表单会显示
     「此文档已设置权限，请登录后使用」。登录后（微信扫码）就能填。

工作原理：
  1. 用专用 profile 启动 Chrome（第一次扫码登录，之后免扫）→ 打开 https://docs.qq.com/desktop
  2. 每 1.5 秒扫一遍「最近文档」，找出所有 /form/page/<ID> 链接
     首次扫描只做基线（不动作），之后每出现一个新 ID 就认为“群里发了新表”
  3. 新标签打开该表 → 等表单放开 → 按 watch 配置填空/单选 → 提交 → 校验「提交成功」
  4. 关掉标签回到桌面页，继续盯

用法：
    双击 watch_docs.bat
    python watch_docs.py --detect-only     # 只打印发现的新收集表，不填不交
    python watch_docs.py --include-existing --dry-run   # 连已存在的也处理，但只填不交
    python watch_docs.py --dump            # 抓一份「最近文档」的 HTML + 截图，用来校准选择器
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from auto_tencent import (
    build_driver,
    fill_answers,
    login_flow,
    submit_form,
    wait_form_ready,
    wait_page_settle,
)
from watch_link import DEFAULT_KEYWORDS, NullSink, Watcher

DESKTOP = "https://docs.qq.com/desktop"
ID_RE = re.compile(r"/form/page/([A-Za-z0-9_\-]{8,64})")
HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg: str = "") -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


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


def form_urls(driver):
    """从「最近文档」页里挖出所有收集表链接，按页面出现顺序（最近的在前）。"""
    found = []
    seen = set()

    def push(raw):
        m = ID_RE.search(raw or "")
        if not m:
            return
        fid = m.group(1)
        if fid in seen:
            return
        seen.add(fid)
        found.append((fid, f"https://docs.qq.com/form/page/{fid}"))

    try:
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
            href = a.get_attribute("href") or ""
            if "/form/page/" in href:
                push(href)
    except WebDriverException:
        pass

    if not found:  # 列表如果是 JS 渲染、没有 <a>，退回扫整页源码
        try:
            for m in ID_RE.finditer(driver.page_source or ""):
                push(m.group(0))
        except WebDriverException:
            pass
    return found


def desktop_ready(driver, timeout=20.0) -> bool:
    """等「最近文档」页渲染出来（或者确认当前在登录页/权限页）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            txt = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except WebDriverException:
            txt = ""
        if any(k in txt for k in ("最近文档", "最近", "我的文档", "与我共享", "登录")):
            return True
        time.sleep(0.3)
    return False


def fill_url(driver, url, args) -> bool:
    """新标签打开、填、提交，然后收尾回桌面页。"""
    t0 = time.time()
    log(f"[开页] {url}")
    before = set(driver.window_handles)
    driver.execute_script("window.open(arguments[0], '_blank');", url)
    time.sleep(0.4)
    new = [h for h in driver.window_handles if h not in before]
    if not new:
        log("[开页] 新标签没打开，改为当前页跳转")
        driver.get(url)
        new = [driver.current_window_handle]

    driver.switch_to.window(new[0])
    try:
        state = wait_page_settle(driver, 15)
        if state == "anon":
            log("[登录] 表单要求登录（未登录时会显示“此文档已设置权限，请登录后使用”）")
            login_flow(driver, "auto", "wechat", args.login_timeout)
        log(f"[开页] 就绪 {time.time() - t0:.2f}s")

        els, attempts = wait_form_ready(driver, args.open_timeout, args.reload_interval)
        if not els:
            log(f"[放弃] 等 {args.open_timeout:.0f}s 表单仍未放开")
            return False
        log(f"[抢填] 刷新 {attempts} 次可填 {time.time() - t0:.2f}s")

        fill_answers(driver, args.text, args.radio, args.check)
        log(f"[抢填] 填完 {time.time() - t0:.2f}s")

        if args.dry_run or args.detect_only:
            log("[演练] 已填未提交")
            shot = os.path.join(HERE, "dryrun.png")
            try:
                driver.save_screenshot(shot)
            except WebDriverException:
                pass
            return False

        ok = submit_form(driver, 15)
        log(f"[结果] {'提交成功' if ok else '未确认'} 总耗时 {time.time() - t0:.2f}s")
        return ok
    finally:
        for h in new:
            try:
                driver.switch_to.window(h)
                driver.close()
            except WebDriverException:
                pass
        try:
            driver.switch_to.window(driver.window_handles[0])
        except WebDriverException:
            pass


def dump(driver):
    html = os.path.join(HERE, "docs_dump.html")
    png = os.path.join(HERE, "docs_dump.png")
    with open(html, "w", encoding="utf-8") as f:
        f.write(driver.page_source or "")
    try:
        driver.save_screenshot(png)
    except WebDriverException:
        pass
    log(f"[dump] 已保存 {html} / {png}")
    for fid, url in form_urls(driver):
        log(f"[dump] 收集表 {fid} -> {url}")


def main() -> int:
    ap = argparse.ArgumentParser(description="盯腾讯文档「最近文档」，新收集表自动填写")
    ap.add_argument("--config", default=os.path.join(HERE, "watch_config.txt"))
    ap.add_argument("--text", action="append", default=[])
    ap.add_argument("--radio", action="append", default=[])
    ap.add_argument("--check", action="append", default=[])
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge"])
    ap.add_argument("--profile", default="profile")
    ap.add_argument("--poll-ms", type=int, default=1500)
    ap.add_argument("--refresh-s", type=float, default=2.0,
                    help="每隔多少秒重新进一次「最近文档」页（列表是进页面时拉的，要重进才看得到新分享）")
    ap.add_argument("--open-timeout", type=float, default=120)
    ap.add_argument("--reload-interval", type=float, default=0.5)
    ap.add_argument("--login-timeout", type=float, default=300)
    ap.add_argument("--include-existing", action="store_true",
                    help="启动时已在列表里的收集表也处理（默认只处理启动之后新出现的）")
    ap.add_argument("--card-clicker", action="store_true",
                    help="并行启动微信卡片点击器：发现腾讯文档消息/卡片就点开，让文档进「最近文档」")
    ap.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS),
                    help="卡片点击器的识别关键词，逗号分隔")
    ap.add_argument("--detect-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", action="store_true", help="保存最近文档页的 HTML/截图后退出")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    provided = {a.split("=")[0] for a in sys.argv[1:] if a.startswith("--")}
    for key in ("text", "radio", "check"):
        if "--" + key not in provided and cfg[key]:
            setattr(args, key, cfg[key])
    for key in ("browser", "profile", "keywords"):
        if "--" + key not in provided and cfg.get(key):
            setattr(args, key, cfg[key])
    for key in ("poll_ms", "open_timeout", "reload_interval", "login_timeout", "refresh_s"):
        if "--" + key.replace("_", "-") not in provided and cfg.get(key):
            setattr(args, key, type(getattr(args, key))(cfg[key]))
    for flag, key in (("--card-clicker", "card_clicker"),):
        if flag not in provided and str(cfg.get(key, "")).lower() in ("1", "true", "yes", "y"):
            setattr(args, key, True)

    log(f"[配置] 答案 text={args.text} radio={args.radio} check={args.check}")

    if args.card_clicker:
        import types

        wl = types.SimpleNamespace(keywords=args.keywords, poll_ms=500,
                                   auto_open=True, no_ocr=False)
        threading.Thread(target=lambda: Watcher(wl, NullSink()).run(),
                         daemon=True, name="card-clicker").start()
        log("[点击器] 已启动：发现腾讯文档消息/卡片会自动点开，点开后文档会进「最近文档」")

    driver = build_driver(args.browser, args.profile, False)
    handled = set()
    try:
        log(f"[浏览器] 打开 {DESKTOP}")
        driver.get(DESKTOP)
        desktop_ready(driver)
        time.sleep(1.0)

        state = wait_page_settle(driver, 10)
        if state != "authed":
            log(f"[登录] 腾讯文档未登录（页面状态 {state}），需要微信扫码一次（之后永久免扫）")
            login_flow(driver, "auto", "wechat", args.login_timeout)
            driver.get(DESKTOP)
            desktop_ready(driver)
            time.sleep(1.5)

        if args.dump:
            dump(driver)
            log("[dump] 浏览器保持打开，按回车（或直接关窗口）结束")
            try:
                input("按回车关闭浏览器 ...")
            except (EOFError, OSError):
                pass
            return 0

        baseline = {fid for fid, _ in form_urls(driver)}
        if args.include_existing:
            log(f"[基线] 列表里现有 {len(baseline)} 个收集表，按要求一并处理")
            baseline = set()
        else:
            log(f"[基线] 列表里现有 {len(baseline)} 个收集表，只处理之后新出现的")
        handled |= baseline

        log("[就绪] 盯「最近文档」中，Ctrl+C 退出")
        last_refresh = time.time()
        while True:
            try:
                if "/desktop" not in (driver.current_url or ""):
                    driver.get(DESKTOP)
                    desktop_ready(driver)
                    last_refresh = time.time()
                urls = form_urls(driver)
                fresh = [(fid, url) for fid, url in urls if fid not in handled]
                for fid, url in fresh:
                    handled.add(fid)
                    log(f"[发现] 新收集表 {fid}")
                    if args.detect_only:
                        log(f"[只嗅探] {url}")
                        continue
                    fill_url(driver, url, args)
                    driver.get(DESKTOP)
                    desktop_ready(driver)
                    last_refresh = time.time()

                # 列表是进页面时拉的，别人新分享给你的表要重新进页面才看得到
                if time.time() - last_refresh > args.refresh_s:
                    driver.get(DESKTOP)
                    desktop_ready(driver)
                    last_refresh = time.time()
            except WebDriverException as exc:
                log(f"[异常] {type(exc).__name__}: {str(exc)[:120]}")
                time.sleep(1.0)
            time.sleep(args.poll_ms / 1000.0)
    except KeyboardInterrupt:
        log("\n已退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
