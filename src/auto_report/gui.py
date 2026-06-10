from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext

from .config import APP_NAME, load_config
from .logging_setup import configure_logging
from .service import AutoReportService


GUI_VERSION = "GUI v8 DEDICATED CHROME WINDOW 2026-06-09"


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


class AutoReportGUI:
    def __init__(self):
        self.config = load_config()
        self.logger = configure_logging(self.config)
        self.gui_debug_path = self.config.log_dir / "gui_debug.log"
        self.log_queue: queue.Queue[str] = queue.Queue()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        self.logger.addHandler(handler)

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} - NOW STATUS - {GUI_VERSION}")
        self.root.geometry("900x720")
        self.root.configure(bg="#F8FAFC")
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.status_popup: tk.Toplevel | None = None

        self.status = tk.StringVar(value="待機中")
        self.current_status = tk.StringVar(value=f"NOW STATUS / 當前狀態\n{GUI_VERSION}\n狀態：待機中\n模式：待機")
        self.latest_log_line = "GUI v6 已啟動"
        self.interval_var = tk.StringVar(value=str(self.config.report_interval_seconds))
        self.refresh_var = tk.StringVar(value=str(self.config.refresh_interval_seconds))
        self.mode_text = "待機"
        self.last_event = "GUI 已啟動"
        self.last_update = ""
        self._build_ui()
        self._set_status("待機中", mode="待機", detail="GUI 已啟動")
        self._write_gui_debug("GUI initialized")
        self.root.after(1000, lambda: self._write_gui_debug("GUI after 1s"))
        self.root.after(300, self._drain_logs)
        self.root.after(1000, self._tick_live_status)

    def run(self) -> None:
        self.root.mainloop()

    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 8}

        self.status_canvas = tk.Canvas(
            self.root,
            height=220,
            bg="#0B1220",
            bd=0,
            highlightthickness=0,
        )
        self.status_canvas.pack(fill=tk.X, padx=14, pady=(14, 8))
        self.status_canvas.bind("<Configure>", lambda _event: self._draw_all_canvases())
        self.status_canvas.bind("<Button-1>", lambda _event: self._show_native_status_popup())
        self._write_gui_debug("status_canvas packed")

        buttons = tk.Frame(self.root)
        buttons.pack(fill=tk.X, **pad)
        tk.Button(buttons, text="立即執行一次", command=self.run_once).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(buttons, text="開始常駐服務 /daily", command=self.start_service).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(buttons, text="開始 24/7 循環", command=self.start_loop).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(buttons, text="停止", command=self.stop_loop).pack(side=tk.LEFT)

        self.settings_canvas = tk.Canvas(
            self.root,
            height=130,
            bg="#FFFFFF",
            bd=0,
            highlightthickness=1,
            highlightbackground="#CBD5E1",
        )
        self.settings_canvas.pack(fill=tk.X, padx=14, pady=(4, 8))
        self.settings_canvas.bind("<Configure>", lambda _event: self._draw_all_canvases())

        self.log_canvas = tk.Canvas(
            self.root,
            height=180,
            bg="#F8FAFC",
            bd=0,
            highlightthickness=1,
            highlightbackground="#CBD5E1",
        )
        self.log_canvas.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        self.log_canvas.bind("<Configure>", lambda _event: self._draw_all_canvases())

        self.log_box = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, height=20)
        self.log_box.configure(state=tk.DISABLED)
        self._append_log_line("GUI v6 已啟動。狀態面板使用 Canvas，每秒自動更新。")

    def run_once(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "已有任務正在執行。")
            return
        self.stop_event.clear()
        self._set_status("準備執行", mode="單次報表", detail="準備刷新頁面並發送報表")
        self.worker = threading.Thread(target=self._run_once_worker, daemon=True)
        self.worker.start()

    def start_loop(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "已有任務正在執行。")
            return
        try:
            interval = max(30, int(self.interval_var.get()))
        except ValueError:
            messagebox.showerror(APP_NAME, "循環間隔必須是數字。")
            return
        object.__setattr__(self.config, "report_interval_seconds", interval)
        self._apply_refresh_interval()
        self.stop_event.clear()
        self._set_status("準備啟動", mode="24/7 循環", detail=f"報表間隔 {interval} 秒")
        self.worker = threading.Thread(target=self._loop_worker, daemon=True)
        self.worker.start()

    def start_service(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "已有任務正在執行。")
            return
        if not self._apply_refresh_interval():
            return
        self.stop_event.clear()
        self._set_status("準備啟動", mode="常駐服務 /daily", detail="即將開始每 60 秒刷新與監聽 /daily")
        self.worker = threading.Thread(target=self._service_worker, daemon=True)
        self.worker.start()

    def stop_loop(self) -> None:
        self.stop_event.set()
        self._set_status("正在停止...", detail="已送出停止信號")

    def _run_once_worker(self) -> None:
        self._set_status("執行中", mode="單次報表", detail="正在刷新頁面、讀取資料並發送 Telegram")
        try:
            AutoReportService(self.config, self.logger).run_once(send_telegram=True)
            self._set_status("待機中", mode="待機", detail="單次報表已完成，已返回待機")
        except Exception as exc:
            self.logger.exception("執行失敗: %s", exc)
            self._set_status("待機中", mode="待機", detail=f"上次單次報表失敗，已返回待機: {exc}")

    def _loop_worker(self) -> None:
        self._set_status("24/7 循環中", mode="24/7 循環", detail="循環報表服務運行中")
        try:
            AutoReportService(self.config, self.logger).run_forever(stop_flag=self.stop_event.is_set)
            self._set_status("已停止", detail="24/7 循環已停止")
        except Exception as exc:
            self.logger.exception("循環失敗: %s", exc)
            self._set_status("失敗", detail=str(exc))

    def _service_worker(self) -> None:
        self._set_status("常駐服務中", mode="常駐服務 /daily", detail="每 60 秒刷新頁面，並監聽 Telegram /daily")
        try:
            AutoReportService(self.config, self.logger).run_service(stop_flag=self.stop_event.is_set)
            self._set_status("已停止", detail="常駐服務已停止")
        except Exception as exc:
            self.logger.exception("常駐服務失敗: %s", exc)
            self._set_status("失敗", detail=str(exc))

    def _apply_refresh_interval(self) -> bool:
        try:
            interval = max(30, int(self.refresh_var.get()))
        except ValueError:
            messagebox.showerror(APP_NAME, "刷新間隔必須是數字。")
            return False
        object.__setattr__(self.config, "refresh_interval_seconds", interval)
        self._refresh_current_status()
        return True

    def _set_status(self, status: str, mode: str | None = None, detail: str | None = None) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda: self._set_status(status, mode=mode, detail=detail))
            return
        self.status.set(status)
        if mode is not None:
            self.mode_text = mode
        if detail is not None:
            self.last_event = detail
        self.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._refresh_current_status()

    def _refresh_current_status(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self._refresh_current_status)
            return
        self.current_status.set(
            "\n".join(
                [
                    "NOW STATUS / 當前狀態",
                    GUI_VERSION,
                    f"狀態：{self.status.get()}",
                    f"模式：{self.mode_text}",
                    f"最後更新：{self.last_update or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"刷新間隔：{self.config.refresh_interval_seconds} 秒",
                    f"每日自動發送：{self.config.daily_report_time}",
                    f"訊息：{self.last_event}",
                ]
            )
        )
        self._draw_all_canvases()
        self._update_window_title()

    def _tick_live_status(self) -> None:
        self._draw_all_canvases()
        self._update_window_title()
        self.root.after(1000, self._tick_live_status)

    def _draw_all_canvases(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self._draw_all_canvases)
            return
        self._draw_status_canvas()
        self._draw_settings_canvas()
        self._draw_log_canvas()

    def _draw_status_canvas(self) -> None:
        if not hasattr(self, "status_canvas"):
            return
        canvas = self.status_canvas
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S HKT")
        last_update = self.last_update or now
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#0B1220", outline="")
        canvas.create_rectangle(0, 0, width, 58, fill="#102A43", outline="")
        canvas.create_text(22, 30, anchor="w", fill="#FFFFFF", font=("Helvetica", 22, "bold"), text="NOW STATUS / 當前狀態")
        canvas.create_text(width - 22, 30, anchor="e", fill="#A7F3D0", font=("Menlo", 18, "bold"), text=now)

        status_fill = "#22C55E" if self.status.get() not in {"失敗", "正在停止..."} else "#EF4444"
        canvas.create_rectangle(22, 78, 230, 128, fill=status_fill, outline="")
        canvas.create_text(126, 103, fill="#FFFFFF", font=("Helvetica", 20, "bold"), text=self.status.get())

        detail_lines = [
            f"版本：{GUI_VERSION}",
            f"模式：{self.mode_text}",
            f"最後狀態更新：{last_update}",
            f"刷新間隔：{self.config.refresh_interval_seconds} 秒    每日自動發送：{self.config.daily_report_time}",
            f"訊息：{self._shorten(self.last_event, 80)}",
        ]
        y = 82
        for line in detail_lines:
            canvas.create_text(260, y, anchor="w", fill="#E5E7EB", font=("Helvetica", 14, "bold"), text=line)
            y += 25

    def _draw_settings_canvas(self) -> None:
        if not hasattr(self, "settings_canvas"):
            return
        canvas = self.settings_canvas
        width = max(canvas.winfo_width(), 1)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, 130, fill="#FFFFFF", outline="")
        canvas.create_text(18, 24, anchor="w", fill="#0F172A", font=("Helvetica", 15, "bold"), text="運行設定")
        lines = [
            f"循環間隔：{self.interval_var.get()} 秒    刷新間隔：{self.refresh_var.get()} 秒    每日報表：{self.config.daily_report_time}",
            f"設定檔：{self.config.env_path or '未找到 .env'}",
            f"輸出資料夾：{self.config.output_dir}",
            "Telegram 指令：發送 /daily 會立即刷新網頁並發送抄本。",
        ]
        y = 52
        for line in lines:
            canvas.create_text(18, y, anchor="w", fill="#334155", font=("Helvetica", 12), text=self._shorten(line, 125))
            y += 21

    def _draw_log_canvas(self) -> None:
        if not hasattr(self, "log_canvas"):
            return
        canvas = self.log_canvas
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#F8FAFC", outline="")
        canvas.create_text(18, 24, anchor="w", fill="#0F172A", font=("Helvetica", 15, "bold"), text="即時日誌 / Live Log")
        canvas.create_text(
            18,
            58,
            anchor="w",
            fill="#334155",
            font=("Helvetica", 12, "bold"),
            text=f"最新日誌：{self._shorten(self.latest_log_line, 120)}",
        )
        canvas.create_text(
            18,
            92,
            anchor="w",
            fill="#475569",
            font=("Helvetica", 12),
            text="提示：上方狀態時間每秒自動更新；報表服務會每 60 秒刷新 Chrome。",
        )

    def _shorten(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _update_window_title(self) -> None:
        self.root.title(f"{APP_NAME} - {self.status.get()} - {GUI_VERSION}")

    def _show_native_status_popup(self) -> None:
        messagebox.showinfo("NOW STATUS / 當前狀態", self.current_status.get())

    def _show_startup_status_popup(self) -> None:
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(2500, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        if self.status_popup is not None and self.status_popup.winfo_exists():
            return
        self.status_popup = tk.Toplevel(self.root)
        self.status_popup.title("NOW STATUS / 當前狀態")
        self.status_popup.geometry("680x360+90+90")
        self.status_popup.configure(bg="#082F1B")
        try:
            self.status_popup.attributes("-topmost", True)
            self.status_popup.transient(self.root)
        except Exception:
            pass
        tk.Label(
            self.status_popup,
            text="NOW STATUS / 當前狀態",
            bg="#082F1B",
            fg="#FFFFFF",
            font=("Helvetica", 24, "bold"),
            padx=18,
            pady=14,
        ).pack(fill=tk.X)
        tk.Message(
            self.status_popup,
            textvariable=self.current_status,
            width=620,
            bg="#DDFBE8",
            fg="#062A19",
            font=("Helvetica", 16, "bold"),
            padx=18,
            pady=18,
        ).pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 12))
        tk.Button(self.status_popup, text="我已看到狀態，關閉此提示", command=self.status_popup.destroy).pack(pady=(0, 14))
        self._write_gui_debug("startup status popup shown")
        self.root.after(300, self._show_native_status_popup)

    def _append_log_line(self, line: str) -> None:
        if not hasattr(self, "log_box"):
            return
        self.latest_log_line = line
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, line + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)
        self._draw_all_canvases()

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log_line(line)
            self.last_event = line
            self.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._refresh_current_status()
        self.root.after(300, self._drain_logs)

    def _write_gui_debug(self, reason: str) -> None:
        try:
            self.config.log_dir.mkdir(parents=True, exist_ok=True)
            self.root.update_idletasks()
            lines = [
                "=" * 72,
                f"time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"reason={reason}",
                f"version={GUI_VERSION}",
                f"title={self.root.title()}",
                f"geometry={self.root.winfo_geometry()}",
                f"status_text={self.current_status.get()!r}",
                f"status_canvas_exists={hasattr(self, 'status_canvas')}",
            ]
            if hasattr(self, "status_canvas"):
                lines.extend(
                    [
                        f"status_canvas_manager={self.status_canvas.winfo_manager()}",
                        f"status_canvas_mapped={self.status_canvas.winfo_ismapped()}",
                        f"status_canvas_geometry={self.status_canvas.winfo_geometry()}",
                    ]
                )
            if hasattr(self, "settings_canvas"):
                lines.extend(
                    [
                        f"settings_canvas_manager={self.settings_canvas.winfo_manager()}",
                        f"settings_canvas_mapped={self.settings_canvas.winfo_ismapped()}",
                        f"settings_canvas_geometry={self.settings_canvas.winfo_geometry()}",
                    ]
                )
            if hasattr(self, "log_canvas"):
                lines.extend(
                    [
                        f"log_canvas_manager={self.log_canvas.winfo_manager()}",
                        f"log_canvas_mapped={self.log_canvas.winfo_ismapped()}",
                        f"log_canvas_geometry={self.log_canvas.winfo_geometry()}",
                        f"latest_log={self.latest_log_line!r}",
                    ]
                )
            if self.status_popup is not None and self.status_popup.winfo_exists():
                lines.extend(
                    [
                        f"status_popup_exists=True",
                        f"status_popup_geometry={self.status_popup.winfo_geometry()}",
                    ]
                )
            lines.append("widgets:")
            lines.extend(self._widget_tree_lines(self.root))
            with self.gui_debug_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            self.logger.info("GUI 自檢日誌已寫入: %s (%s)", self.gui_debug_path, reason)
        except Exception as exc:
            self.logger.warning("GUI 自檢日誌寫入失敗: %s", exc)

    def _widget_tree_lines(self, widget, depth: int = 0) -> list[str]:
        indent = "  " * depth
        text = ""
        try:
            value = widget.cget("text")
            if value:
                text = f" text={value!r}"
        except Exception:
            pass
        try:
            textvariable = widget.cget("textvariable")
            if textvariable:
                text += f" textvariable={textvariable!r}"
        except Exception:
            pass
        line = (
            f"{indent}{widget.winfo_class()} name={widget.winfo_name()} "
            f"manager={widget.winfo_manager()} mapped={widget.winfo_ismapped()} "
            f"geometry={widget.winfo_geometry()}{text}"
        )
        lines = [line]
        for child in widget.winfo_children():
            lines.extend(self._widget_tree_lines(child, depth + 1))
        return lines


def run_gui() -> None:
    AutoReportGUI().run()


def run_gui_check() -> Path:
    app = AutoReportGUI()
    app._write_gui_debug("manual gui-check before destroy")
    path = app.gui_debug_path
    app.root.destroy()
    return path
