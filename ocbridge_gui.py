#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCBridge GUI — OpenCode 免费模型桥 (原生桌面界面 + 系统托盘)

打包形态 (两个 exe):
    OCBridge.exe        --windowed 打包 -> 纯 GUI, 双击无任何控制台窗口;
                        子进程/服务全部通过 OCBridge-CLI.exe 执行
    OCBridge-CLI.exe    --console 打包  -> 全部命令行功能 (serve/sync/status/...)

无参数 = GUI; 带子命令 = 命令行透传 (windowed 时报错情况下仍可用 CLI exe)。
"""
import os
import sys
import threading
import time
from pathlib import Path

import bridge

APP_DIR = bridge.APP_DIR
CFG = bridge.load_config()
BRIDGE_PORT = int(CFG.get("port", 8899))
HEALTH_URL = f"http://127.0.0.1:{BRIDGE_PORT}/health"

# windowed 打包时 stdout/stderr 为 None -> 兜底, 防止 print 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout


def hide_console():
    """console 打包误双击时隐藏黑框 (windowed 打包下无操作)。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def _is_frozen():
    return getattr(sys, "frozen", False)


def cli_exe():
    """命令行功能由本程序自身承载 (带参数运行)。"""
    return sys.executable


def service_args():
    if _is_frozen():
        return [cli_exe(), "serve"]
    return [sys.executable, str(APP_DIR / "bridge.py"), "serve"]


def cmdline_args(subcmd):
    if _is_frozen():
        return [cli_exe()] + subcmd
    return [sys.executable, str(APP_DIR / "bridge.py")] + subcmd


def no_window_flags():
    import subprocess
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def make_icon_image(size=64):
    """绘制程序图标 (蓝底白字 OC)。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(2, size // 32)
    d.ellipse((m, m, size - m, size - m), fill=(29, 101, 237, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.42))
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", int(size * 0.42))
        except Exception:
            font = ImageFont.load_default()
    d.text((size / 2, size / 2 + size * 0.02), "OC", fill="white", anchor="mm", font=font)
    return img


class Tray:
    """系统托盘 (pystray)。"""

    def __init__(self, gui):
        import pystray
        self.gui = gui
        self.icon = pystray.Icon(
            "ocbridge",
            make_icon_image(64),
            "OCBridge — OpenCode 免费模型桥",
            menu=pystray.Menu(
                pystray.MenuItem("显示主界面", self._show, default=True),
                pystray.MenuItem("启动服务", self._start),
                pystray.MenuItem("停止服务", self._stop),
                pystray.MenuItem("同步到 WorkBuddy", self._sync),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ),
        )

    def _dispatch(self, fn):
        try:
            self.gui.root.after(0, fn)
        except Exception:
            pass

    def _show(self, *_a):
        self._dispatch(self.gui.show_window)

    def _start(self, *_a):
        self._dispatch(self.gui.start_service)

    def _stop(self, *_a):
        self._dispatch(self.gui.stop_service)

    def _sync(self, *_a):
        self._dispatch(self.gui.sync_models)

    def _quit(self, *_a):
        self._dispatch(lambda: self.gui.quit_app(ask=True))

    def run(self):
        threading.Thread(target=self.icon.run, daemon=True).start()

    def notify(self, msg, title="OCBridge"):
        try:
            self.icon.notify(msg, title)
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


class BridgeGUI:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.svc_proc = None
        self.sync_running = False
        self.dl_running = False
        self.login_running = False
        self.tray = None

        root = tk.Tk()
        self.root = root
        root.title("OCBridge — OpenCode 免费模型桥")
        root.geometry("680x600")
        root.minsize(620, 540)
        root.withdraw()          # 先隐藏, 待全部构建完成后再显示 (消除启动空窗闪烁)
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"+{(sw - 680) // 2}+{max(0, (sh - 600) // 3)}")

        # 窗口图标
        try:
            from PIL import ImageTk
            self._win_icon = ImageTk.PhotoImage(make_icon_image(64))
            root.iconphoto(True, self._win_icon)
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        # ---------- 状态区 ----------
        sframe = ttk.LabelFrame(root, text=" 状态 ")
        sframe.pack(fill="x", padx=10, pady=(10, 4))
        grid = ttk.Frame(sframe)
        grid.pack(fill="x", padx=8, pady=6)

        F = ("Microsoft YaHei UI", 10)
        self.lb_service = ttk.Label(grid, text="桥服务:  检测中...", font=F)
        self.lb_service.grid(row=0, column=0, sticky="w", pady=2)
        self.lb_login = ttk.Label(grid, text="登录状态: 检测中...", font=F)
        self.lb_login.grid(row=1, column=0, sticky="w", pady=2)
        self.lb_cli = ttk.Label(grid, text="opencode: 检测中...", font=F)
        self.lb_cli.grid(row=2, column=0, sticky="w", pady=2)

        btns = ttk.Frame(sframe)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_start = ttk.Button(btns, text="▶ 启动服务", command=self.start_service)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(btns, text="■ 停止服务", command=self.stop_service, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="↻ 刷新", command=self.refresh).pack(side="left", padx=(0, 6))
        self.btn_login = ttk.Button(btns, text="登录 OpenCode", command=self.open_login)
        self.btn_login.pack(side="left")
        # "下载引擎"急救按钮: 默认隐藏, 仅当探测不到 opencode 引擎时自动出现
        self.btn_dl = ttk.Button(btns, text="⬇ 下载引擎", command=self.download_opencode)

        # ---------- 模型区 ----------
        mframe = ttk.LabelFrame(root, text=" 免费模型 ")
        mframe.pack(fill="both", expand=True, padx=10, pady=4)
        mtop = ttk.Frame(mframe)
        mtop.pack(fill="x", padx=8, pady=(6, 2))
        self.lb_models = ttk.Label(mtop, text="模型: -", font=F)
        self.lb_models.pack(side="left")
        self.btn_sync = ttk.Button(mtop, text="同步到 WorkBuddy", command=self.sync_models)
        self.btn_sync.pack(side="right")
        ttk.Button(mtop, text="更多免费资源", command=self.open_resources).pack(side="right", padx=(0, 6))

        self.model_list = tk.Listbox(mframe, height=7, font=("Consolas", 10),
                                     activestyle="none", selectmode="browse")
        self.model_list.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        # ---------- 日志区 ----------
        lframe = ttk.LabelFrame(root, text=" 日志 ")
        lframe.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(lframe, height=7, font=("Consolas", 9), wrap="word",
                                state="disabled", background="#fafafa")
        lsb = ttk.Scrollbar(lframe, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.log_text.pack(fill="both", expand=True, padx=(8, 0), pady=4)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---------- 托盘 ----------
        try:
            self.tray = Tray(self)
            self.tray.run()
            tray_ok = True
        except Exception as e:
            tray_ok = False
            self.log(f"托盘初始化失败: {e}")

        self.log(f"OCBridge v{bridge.VERSION} 已启动" + ("(已启用系统托盘, 关闭窗口将最小化到托盘)" if tray_ok else ""))
        self.refresh()
        # 全部就绪, 显示窗口 (用户看到的第一帧就是完整界面)
        try:
            root.deiconify()
            root.lift()
            root.focus_force()
        except Exception:
            pass
        self.root.after(3000, self._auto_refresh)

    # -------------------------------------------------- 工具
    def log(self, msg):
        try:
            ts = time.strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{ts}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _health(self, timeout=2.0):
        import urllib.request
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
                return True, bridge.json.loads(r.read().decode("utf-8"))
        except Exception:
            return False, {}

    # -------------------------------------------------- 窗口
    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def hide_window(self):
        try:
            self.root.withdraw()
        except Exception:
            pass

    # -------------------------------------------------- 状态刷新
    def refresh(self):
        ok, info = self._health()
        running = ok and info.get("ok") is not None

        if running:
            self.lb_service.configure(text=f"桥服务:  ● 运行中  (127.0.0.1:{BRIDGE_PORT})",
                                      foreground="#0a7d32")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(
                state="normal" if (self.svc_proc and self.svc_proc.poll() is None) else "disabled")
        else:
            self.lb_service.configure(text="桥服务:  ○ 未运行", foreground="#b03030")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

        login_ok, login_msg = bridge.check_login()
        if login_ok:
            self.lb_login.configure(text=f"登录状态: ✓ {login_msg}", foreground="#0a7d32")
        else:
            self.lb_login.configure(text=f"登录状态: ✗ {login_msg}", foreground="#b03030")

        cli = bridge.find_opencode_cli(CFG, allow_extract=False)
        has_xz = (bridge.APP_DIR / "opencode" / "opencode.exe.xz").exists()
        if cli:
            self.lb_cli.configure(text=f"opencode: ✓ {cli}", foreground="#0a7d32")
            if self.btn_dl.winfo_manager():
                self.btn_dl.pack_forget()          # 引擎正常时隐藏急救按钮
        elif has_xz:
            self.lb_cli.configure(text="opencode: ✓ 内置引擎 (压缩存放, 首次启动服务时自动解压一次)",
                                  foreground="#0a7d32")
            if self.btn_dl.winfo_manager():
                self.btn_dl.pack_forget()
        else:
            self.lb_cli.configure(
                text="opencode: ✗ 未找到 — 点右侧 [⬇ 下载引擎] 自动修复",
                foreground="#b07000")
            if not self.btn_dl.winfo_manager():
                try:
                    self.btn_dl.pack(side="left", padx=(0, 6), before=self.btn_login)
                except Exception:
                    self.btn_dl.pack(side="left", padx=(0, 6))

        models = bridge.load_cached_models()
        self.lb_models.configure(text=f"免费模型: {len(models)} 个")
        self.model_list.delete(0, "end")
        for m in models:
            tools = "工具✓" if m.get("tools") else "工具-"
            self.model_list.insert("end", f"  {m['id']:<42s} {tools}  ctx={m.get('context')}")

    def _auto_refresh(self):
        # 后台线程做健康检查, 避免阻塞界面
        def worker():
            try:
                ok, info = self._health(timeout=1.2)
                running = ok and info.get("ok") is not None
            except Exception:
                running = False
            self.root.after(0, lambda: self._apply_service_state(running))
            self.root.after(3000, self._auto_refresh)
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.root.after(3000, self._auto_refresh)

    def _apply_service_state(self, running):
        try:
            if running:
                self.lb_service.configure(text=f"桥服务:  ● 运行中  (127.0.0.1:{BRIDGE_PORT})",
                                          foreground="#0a7d32")
                self.btn_start.configure(state="disabled")
                if self.svc_proc and self.svc_proc.poll() is None:
                    self.btn_stop.configure(state="normal")
            else:
                self.lb_service.configure(text="桥服务:  ○ 未运行", foreground="#b03030")
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                if self.svc_proc and self.svc_proc.poll() is not None:
                    self.log(f"服务进程已退出 (exit={self.svc_proc.returncode})")
                    self.svc_proc = None
        except Exception:
            pass

    # -------------------------------------------------- 服务控制
    def start_service(self):
        import subprocess
        ok, _ = self._health(timeout=1)
        if ok:
            self.log("已有服务在运行, 无需重复启动")
            self.refresh()
            return
        if not bridge.check_login()[0]:
            self.log("警告: opencode 未登录 — 请先在 OpenCode 客户端登录")
            self.show_window()
        has_engine = bool(bridge.find_opencode_cli(CFG, allow_extract=False)) or \
            (bridge.APP_DIR / "opencode" / "opencode.exe.xz").exists()
        if not has_engine:
            self.log("找不到 opencode 引擎, 请先点 [⬇ 下载引擎] 或安装 OpenCode 客户端")
            self.show_window()
            return
        logf = open(APP_DIR / "opencode_serve.log", "ab")
        try:
            xz = bridge.APP_DIR / "opencode" / "opencode.exe.xz"
            exe = bridge.APP_DIR / "opencode" / "opencode.exe"
            first_time = xz.exists() and not exe.exists()
            self.svc_proc = subprocess.Popen(
                service_args(), stdout=logf, stderr=subprocess.STDOUT,
                cwd=str(APP_DIR), creationflags=no_window_flags())
            if first_time:
                self.log(f"服务已启动 (pid={self.svc_proc.pid}) — 首次使用正在解压内置引擎, 约 10-20 秒后可用...")
            else:
                self.log(f"服务已启动 (pid={self.svc_proc.pid}), 等待就绪...")
            self.root.after(2500, self.refresh)
        except Exception as e:
            self.log(f"启动失败: {e}")

    def stop_service(self):
        from tkinter import messagebox
        if self.svc_proc and self.svc_proc.poll() is None:
            self.svc_proc.terminate()
            try:
                self.svc_proc.wait(timeout=6)
            except Exception:
                self.svc_proc.kill()
            self.svc_proc = None
            self.log("服务已停止 (opencode 后端会空闲 5 分钟后自动退出)")
            self.root.after(800, self.refresh)
        else:
            self.log("当前没有由本界面启动的服务进程")
            messagebox.showinfo("提示", "没有可停止的服务。\n如果服务由其他方式启动, 请关闭对应窗口/进程。")

    # -------------------------------------------------- 同步 / 下载
    def _run_cli_stream(self, subcmd, tag):
        import subprocess
        try:
            proc = subprocess.Popen(
                cmdline_args(subcmd), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=str(APP_DIR),
                creationflags=no_window_flags())
            for line in proc.stdout:
                text = line.decode("utf-8", "replace").rstrip()
                if text and not text.startswith("="):
                    self.root.after(0, self.log, f"[{tag}] {text}")
            code = proc.wait()
            self.root.after(0, self.log, f"[{tag}] 完成 (exit={code})")
        except Exception as e:
            self.root.after(0, self.log, f"[{tag}] 失败: {e}")

    # -------------------------------------------------- 更多资源 / 同步 / 下载
    def open_resources(self):
        try:
            import webbrowser
            webbrowser.open("https://link3.cc/haochihc")
            self.log("已打开: 更多免费资源 -> https://link3.cc/haochihc")
        except Exception as e:
            self.log(f"打开链接失败: {e}")

    def sync_models(self):
        if self.sync_running:
            self.log("同步正在进行中, 请稍候...")
            return
        self.sync_running = True
        self.btn_sync.configure(state="disabled")
        self.log("开始同步免费模型到 WorkBuddy (首次探测约 1-2 分钟)...")

        def worker():
            self._run_cli_stream(["sync"], "同步")
            self.sync_running = False
            self.root.after(0, lambda: (self.btn_sync.configure(state="normal"), self.refresh()))

        threading.Thread(target=worker, daemon=True).start()

    def download_opencode(self):
        if self.dl_running:
            self.log("下载正在进行中...")
            return
        self.dl_running = True
        self.btn_dl.configure(state="disabled")
        self.log("开始下载 opencode (约 100MB, 走 npm 国内镜像)...")

        def worker():
            self._run_cli_stream(["setup", "--download"], "下载")
            self.dl_running = False
            self.root.after(0, lambda: (self.refresh(), self.show_window()))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------- 登录 OpenCode (device flow)
    def open_login(self):
        if self.login_running:
            self.log("登录流程进行中, 请在弹出的窗口完成操作...")
            return
        self.login_running = True
        self.log("正在发起登录 (无需安装 opencode 客户端)...")

        def starter():
            try:
                dev = bridge.start_device_login()
            except Exception as e:
                self.root.after(0, self.log, f"登录发起失败: {e}")
                self.login_running = False
                return
            self.root.after(0, lambda: self._show_login_dialog(dev))

        threading.Thread(target=starter, daemon=True).start()

    def _show_login_dialog(self, dev):
        import tkinter as tk
        from tkinter import ttk

        dlg = tk.Toplevel(self.root)
        dlg.title("登录 OpenCode")
        dlg.geometry("500x300")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()

        font_main = ("Microsoft YaHei UI", 10)
        ttk.Label(dlg, text="在浏览器中完成登录，并输入以下代码：", font=font_main).pack(pady=(16, 2))
        code_lbl = tk.Label(dlg, text=dev["user_code"], font=("Consolas", 26, "bold"), fg="#1d65ed")
        code_lbl.pack(pady=4)

        row = ttk.Frame(dlg)
        row.pack(pady=6)

        def copy_code():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(dev["user_code"])
                status_var.set("代码已复制到剪贴板")
            except Exception:
                pass

        def open_browser():
            try:
                import webbrowser
                webbrowser.open(dev["verification_url"])
            except Exception:
                pass

        ttk.Button(row, text="复制代码", command=copy_code).pack(side="left", padx=5)
        ttk.Button(row, text="打开浏览器", command=open_browser).pack(side="left", padx=5)

        status_var = tk.StringVar(value="等待授权中...")
        ttk.Label(dlg, textvariable=status_var, foreground="#666", font=font_main).pack(pady=8)
        ttk.Label(dlg, text=f"链接: {dev['verification_url']}", foreground="#888",
                  font=("Microsoft YaHei UI", 8), wraplength=460).pack(pady=(0, 8))

        def on_ok(tok):
            try:
                db = bridge.save_login_credentials(tok)
                self.log(f"登录成功! 凭据已保存到: {db}")
                status_var.set("✓ 登录成功!")
                self.refresh()
                dlg.after(1000, dlg.destroy)
            except Exception as e:
                status_var.set(f"保存失败: {e}")
                self.log(f"登录凭据保存失败: {e}")
            finally:
                self.login_running = False

        def on_fail(msg):
            status_var.set(f"✗ 失败: {msg}")
            self.log(f"登录失败: {msg}")
            self.login_running = False

        def poller():
            try:
                tok = bridge.poll_device_login(
                    dev["device_code"], dev["interval"], dev["expires_in"],
                    progress=lambda left: self.root.after(0, status_var.set,
                                                          f"等待授权中... (剩余 {left}s)"))
                self.root.after(0, on_ok, tok)
            except Exception as e:
                self.root.after(0, on_fail, str(e))

        threading.Thread(target=poller, daemon=True).start()

        # 自动打开浏览器
        try:
            import webbrowser
            webbrowser.open(dev["verification_url"])
        except Exception:
            pass

        def on_dlg_close():
            if self.login_running:
                self.login_running = False
                self.log("已关闭登录窗口 (若已在浏览器完成授权, 稍后仍会自动生效)")
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", on_dlg_close)

    # -------------------------------------------------- 关闭 / 退出
    def on_close(self):
        """点 X: 有托盘时最小化到托盘; 无托盘时询问。"""
        if self.tray is not None:
            self.hide_window()
            self.tray.notify("程序已最小化到托盘, 双击托盘图标可重新打开", "OCBridge")
            return
        self.quit_app(ask=True)

    def quit_app(self, ask=True):
        from tkinter import messagebox
        if ask and self.svc_proc and self.svc_proc.poll() is None:
            r = messagebox.askyesno(
                "退出",
                "桥服务正在运行。\n\n[是] 停止服务并退出\n[否] 保持服务后台运行并退出")
            if r:
                try:
                    self.svc_proc.terminate()
                except Exception:
                    pass
        if self.tray is not None:
            self.tray.stop()
        try:
            self.root.destroy()
        except Exception:
            pass


def run_gui():
    try:
        gui = BridgeGUI()
        gui.root.mainloop()
    except Exception as e:
        try:
            with open(APP_DIR / "gui_error.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} GUI error: {e}\n")
        except Exception:
            pass
        raise


def main():
    if len(sys.argv) > 1:
        # 命令行模式 (由 OCBridge-CLI.exe 承载; GUI exe 误带参数时也尽量工作)
        bridge.main()
        return
    hide_console()
    run_gui()


if __name__ == "__main__":
    main()
