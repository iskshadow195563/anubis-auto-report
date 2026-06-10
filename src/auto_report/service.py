from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

from .chrome import ChromeController, ChromeError, build_fill_login_js, build_fill_otp_js
from .config import AppConfig
from .history import load_history, merge_history, save_history
from .parser import ParseError, parse_report_payload
from .renderer import render_report_files, workbook_has_formulas
from .telegram_bot import TelegramBot
from .user_settings import load_user_settings, render_message_template, render_telegram_filename


class AutoReportService:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.chrome = ChromeController(config.admin_url, config.admin_domain, config.chrome_poll_seconds)
        self._run_lock = Lock()
        self._logout_notified = False
        self.telegram = TelegramBot(
            token=config.telegram_bot_token,
            primary_chat_id=config.telegram_chat_id,
            copy_chat_ids=config.telegram_copy_chat_ids,
            allowed_chat_ids=config.telegram_allowed_chat_ids,
            allowed_usernames=config.telegram_allowed_usernames,
            state_dir=config.state_dir,
        )

    def run_once(self, send_telegram: bool = True, record_history: bool = False) -> tuple[Path, Path]:
        with self._run_lock:
            self._validate_config()
            self.logger.info("開始讀取 Chrome 管理端頁面")
            tab = self.chrome.ensure_report_tab()
            self.logger.info("使用 Chrome window_id=%s tab=%s url=%s", tab.window_id, tab.tab_index, tab.url)
            tab = self.refresh_page(tab=tab, login_if_needed=True)
            payload = self._read_payload_with_retry(tab)
            report = parse_report_payload(payload)
            history_path = self.config.state_dir / "daily_history.json"
            existing_history = load_history(history_path)
            history_reports = merge_history(report, existing_history) if record_history else existing_history
            image_path, xlsx_path = render_report_files(report, self.config.output_dir, history_reports=history_reports)
            if workbook_has_formulas(xlsx_path):
                raise RuntimeError(f"生成的 XLSX 包含公式，已停止發送: {xlsx_path}")
            if record_history:
                save_history(history_path, history_reports)
                self.logger.info("已更新每日 23:30 HKT 歷史記錄: %s", history_path)
            self.logger.info("已生成報表: %s / %s", image_path, xlsx_path)

            if send_telegram:
                user_settings = load_user_settings(self.config.state_dir)
                caption = (
                    f"币种资金流日报 {report.captured_at.strftime('%Y-%m-%d %H:%M HKT')}\n"
                    f"今日转入 {report.total_in_usd:,.2f} USD\n"
                    f"今日转出 {report.total_out_usd:,.2f} USD\n"
                    f"{report.status} {abs(report.net_usd):,.2f} USD"
                )
                if user_settings.send_preview_image:
                    self._send_photo_with_copy(image_path, caption)
                else:
                    self.logger.info("已按 Web GUI 設定略過 Telegram 預覽圖片")
                if self.config.send_xlsx:
                    document_caption = render_message_template(user_settings.telegram_document_caption, report)
                    document_filename = render_telegram_filename(user_settings.telegram_filename_template, report)
                    self._send_document_with_copy(xlsx_path, document_caption, document_filename)
                self.logger.info("已發送 Telegram")
            return image_path, xlsx_path

    def refresh_page(self, tab=None, login_if_needed: bool = False):
        self._validate_config()
        tab = tab or self.chrome.ensure_report_tab()
        self.logger.info("刷新 Chrome 管理端頁面 window_id=%s tab=%s", tab.window_id, tab.tab_index)
        try:
            if self.config.admin_domain not in tab.url or "/order/liquidity" not in tab.url:
                self.chrome.navigate(tab, self.config.admin_url)
            else:
                self.chrome.reload(tab)
        except ChromeError as exc:
            self.logger.warning("Chrome 刷新失敗，改用新的管理端 tab 重試: %s", exc)
            tab = self.chrome.open_report_tab()
            self.logger.info("已切換到 Chrome window_id=%s tab=%s url=%s", tab.window_id, tab.tab_index, tab.url)
        if login_if_needed:
            self._ensure_logged_in(tab)
        return tab

    def run_forever(self, stop_flag=None) -> None:
        self.logger.info(
            "24/7 循環模式啟動，報表間隔 %s 秒，刷新間隔 %s 秒",
            self.config.report_interval_seconds,
            self.config.refresh_interval_seconds,
        )
        next_refresh = 0.0
        while True:
            if stop_flag and stop_flag():
                self.logger.info("收到停止信號")
                return
            try:
                self.run_once(send_telegram=True)
            except Exception as exc:
                self.logger.exception("執行失敗: %s", exc)
                self._notify_error(exc)
            deadline = time.time() + self.config.report_interval_seconds
            while time.time() < deadline:
                if stop_flag and stop_flag():
                    self.logger.info("收到停止信號")
                    return
                if time.time() >= next_refresh:
                    self._safe_refresh_only()
                    next_refresh = time.time() + self.config.refresh_interval_seconds
                time.sleep(1)

    def run_service(self, stop_flag=None) -> None:
        self._validate_config()
        self.logger.info(
            "常駐服務啟動：每 %s 秒刷新頁面，監聽 Telegram /daily，每日 %s 自動發送",
            self.config.refresh_interval_seconds,
            self.config.daily_report_time,
        )
        next_refresh = 0.0
        last_poll_error = ""
        last_daily_date = ""
        while True:
            if stop_flag and stop_flag():
                self.logger.info("收到停止信號")
                return
            now = time.time()
            if now >= next_refresh:
                self._safe_refresh_only()
                next_refresh = time.time() + self.config.refresh_interval_seconds
            if self._daily_due(last_daily_date):
                last_daily_date = datetime.now().astimezone().strftime("%Y-%m-%d")
                self.logger.info("到達每日 %s，自動生成並發送報表", self.config.daily_report_time)
                try:
                    self.run_once(send_telegram=True, record_history=True)
                except Exception as exc:
                    self.logger.exception("每日自動報表失敗: %s", exc)
                    self._notify_error(exc)
            try:
                for update in self.telegram.get_updates(timeout=self.config.telegram_poll_seconds):
                    if self._is_daily_command(update.text) or update.callback_data == "refresh_daily":
                        self._handle_daily_command(update)
                last_poll_error = ""
            except Exception as exc:
                msg = str(exc)
                if msg != last_poll_error:
                    self.logger.warning("Telegram /daily 監聽失敗: %s", exc)
                    last_poll_error = msg
                time.sleep(5)

    def _handle_daily_command(self, update) -> None:
        if not self.telegram.is_allowed(update):
            self.logger.warning("忽略未授權 /daily chat_id=%s username=%s", update.chat_id, update.username)
            self.telegram.send_message("未授權使用 /daily。", [update.chat_id])
            return
        if update.callback_id:
            self.telegram.answer_callback(update.callback_id, "正在刷新並生成日報...")
        self.logger.info("收到 /daily 指令 chat_id=%s username=%s", update.chat_id, update.username)
        self.telegram.send_message("收到 /daily，正在刷新頁面並生成報表。", [update.chat_id])
        try:
            self.run_once(send_telegram=True)
            self.telegram.send_message("今日報表已完成發送。", [update.chat_id])
        except Exception as exc:
            self.logger.exception("/daily 執行失敗: %s", exc)
            self.telegram.send_message(f"/daily 執行失敗：{exc}", [update.chat_id])

    def _safe_refresh_only(self) -> None:
        try:
            tab = self.chrome.ensure_report_tab()
            tab = self.refresh_page(tab=tab, login_if_needed=False)
            self._auto_login_if_needed(tab)
        except Exception as exc:
            self.logger.warning("定時刷新失敗: %s", exc)

    def _auto_login_if_needed(self, tab) -> None:
        state = self.chrome.detect_login_state(tab)
        if state.get("hasReport"):
            if self._logout_notified:
                self.logger.info("管理端已恢復登入狀態")
            self._logout_notified = False
            return

        logged_out = bool(state.get("hasPassword") or state.get("hasOtp"))
        if not logged_out:
            return

        if not self._logout_notified:
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            self.telegram.send_message(
                "Anubis 管理端已登出，正在自動重新登入。\n"
                f"時間: {now}\n"
                "流程: 先自動輸入帳號密碼；如果出現 Google 驗證碼欄位，Bot 會再提示你回覆 6 位數驗證碼。"
            )
            self._logout_notified = True

        self.logger.warning("檢測到管理端登出，開始自動登入流程")
        self._ensure_logged_in(tab)
        self.telegram.send_message("Anubis 管理端已自動登入成功。")
        self._logout_notified = False

    @staticmethod
    def _is_daily_command(text: str) -> bool:
        return bool(re.match(r"^\s*/daily(?:@\w+)?(?:\s|$)", text or "", re.I))

    def _daily_due(self, last_daily_date: str) -> bool:
        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        if last_daily_date == today:
            return False
        try:
            hour_text, minute_text = self.config.daily_report_time.split(":", 1)
            target_hour = int(hour_text)
            target_minute = int(minute_text)
        except ValueError:
            target_hour, target_minute = 23, 30
        return (now.hour, now.minute) >= (target_hour, target_minute)

    def _validate_config(self) -> None:
        missing = []
        if not self.config.admin_username:
            missing.append("ADMIN_USERNAME")
        if not self.config.admin_password:
            missing.append("ADMIN_PASSWORD")
        if not self.config.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.config.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise RuntimeError("設定缺失: " + ", ".join(missing))

    def _ensure_logged_in(self, tab) -> None:
        state = self.chrome.detect_login_state(tab)
        if state.get("hasReport"):
            return
        if not state.get("hasPassword") and not state.get("hasOtp"):
            self.chrome.navigate(tab, self.config.admin_url)
            time.sleep(2)
            state = self.chrome.detect_login_state(tab)
            if state.get("hasReport"):
                return

        if state.get("hasPassword"):
            self.logger.info("檢測到登入頁，正在輸入帳號密碼")
            raw = self.chrome.execute(tab, build_fill_login_js(self.config.admin_username, self.config.admin_password))
            self.logger.debug("登入表單返回: %s", raw)
            time.sleep(3)
        elif state.get("hasOtp"):
            self._submit_otp(tab)
            return

        state = self._wait_for_state(tab, want_report_or_otp=True, timeout=40)
        if state.get("hasReport"):
            return
        if state.get("hasOtp"):
            self._submit_otp(tab)
            return
        raise RuntimeError("登入後仍未看到今日轉入/轉出報表，請檢查頁面狀態。")

    def _submit_otp(self, tab) -> None:
        self.logger.info("需要 Google 驗證碼，已發送 Telegram 提示")
        user_settings = load_user_settings(self.config.state_dir)
        code = self.telegram.ask_for_otp(
            self.config.otp_timeout_seconds,
            reminder_interval_seconds=user_settings.otp_reminder_interval_seconds,
        )
        raw = self.chrome.execute(tab, build_fill_otp_js(code))
        self.logger.debug("OTP 表單返回: %s", raw)
        state = self._wait_for_state(tab, want_report_or_otp=False, timeout=60)
        if state.get("hasReport"):
            return
        raise RuntimeError("已輸入 Google 驗證碼，但仍未看到今日轉入/轉出報表，請檢查驗證碼是否正確。")

    def _wait_for_state(self, tab, want_report_or_otp: bool, timeout: int) -> dict:
        deadline = time.time() + timeout
        latest = {}
        while time.time() < deadline:
            latest = self.chrome.detect_login_state(tab)
            if latest.get("hasReport"):
                return latest
            if want_report_or_otp and latest.get("hasOtp"):
                return latest
            time.sleep(self.config.chrome_poll_seconds)
        return latest

    def _read_payload_with_retry(self, tab) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                payload = self.chrome.extract_payload(tab)
            except ChromeError as exc:
                last_error = exc
                self.logger.warning("第 %s 次讀取頁面失敗，改用新的管理端 tab: %s", attempt, exc)
                tab = self.refresh_page(tab=self.chrome.open_report_tab(), login_if_needed=True)
                time.sleep(2)
                continue
            try:
                parse_report_payload(payload)
                return payload
            except ParseError as exc:
                last_error = exc
                self.logger.warning("第 %s 次解析頁面失敗: %s", attempt, exc)
                try:
                    self.chrome.navigate(tab, self.config.admin_url)
                except ChromeError as nav_exc:
                    self.logger.warning("重新導向管理端失敗，改用新的管理端 tab: %s", nav_exc)
                    tab = self.chrome.open_report_tab()
                time.sleep(4)
        raise last_error or RuntimeError("頁面解析失敗")

    def _notify_error(self, exc: Exception) -> None:
        try:
            if self.telegram.enabled:
                now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                self.telegram.send_message(f"Auto Report 執行失敗\n時間: {now}\n錯誤: {exc}", [self.config.telegram_chat_id])
        except Exception:
            self.logger.exception("錯誤通知發送失敗")

    def _send_photo_with_copy(self, image_path: Path, caption: str) -> None:
        self.telegram.send_photo(image_path, caption=caption, chat_ids=[self.config.telegram_chat_id])
        for chat_id in self.config.telegram_copy_chat_ids:
            try:
                self.telegram.send_photo(image_path, caption=caption, chat_ids=[chat_id])
            except Exception as exc:
                self.logger.warning("Telegram 抄送圖片失敗 chat_id=%s: %s", chat_id, exc)

    def _send_document_with_copy(self, file_path: Path, caption: str, filename: str) -> None:
        self.telegram.send_document(
            file_path,
            caption=caption,
            filename=filename,
            chat_ids=[self.config.telegram_chat_id],
        )
        if not self.config.send_copy_xlsx:
            return
        for chat_id in self.config.telegram_copy_chat_ids:
            try:
                self.telegram.send_document(file_path, caption=caption, filename=filename, chat_ids=[chat_id])
            except Exception as exc:
                self.logger.warning("Telegram 抄送文件失敗 chat_id=%s: %s", chat_id, exc)
