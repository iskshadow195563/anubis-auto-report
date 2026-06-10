from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    chat_id: str
    text: str
    username: str = ""
    callback_id: str = ""
    callback_data: str = ""


class TelegramBot:
    def __init__(
        self,
        token: str,
        primary_chat_id: str,
        copy_chat_ids: list[str],
        allowed_chat_ids: list[str],
        allowed_usernames: list[str],
        state_dir: Path,
    ):
        self.token = token
        self.primary_chat_id = primary_chat_id
        self.copy_chat_ids = copy_chat_ids
        self.allowed_chat_ids = set(str(v) for v in allowed_chat_ids if v)
        self.allowed_usernames = {v.lstrip("@").lower() for v in allowed_usernames if v}
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.offset_path = self.state_dir / "telegram_offset.json"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.primary_chat_id)

    def all_chat_ids(self, include_copy: bool = True) -> list[str]:
        ids = [self.primary_chat_id]
        if include_copy:
            ids.extend(self.copy_chat_ids)
        result = []
        for chat_id in ids:
            if chat_id and chat_id not in result:
                result.append(chat_id)
        return result

    def send_message(self, text: str, chat_ids: list[str] | None = None) -> None:
        for chat_id in chat_ids or self.all_chat_ids():
            self._post("sendMessage", {"chat_id": chat_id, "text": text})

    def answer_callback(self, callback_id: str, text: str = "正在處理...") -> None:
        if callback_id:
            self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def send_photo(
        self,
        image_path: Path,
        caption: str = "",
        include_copy: bool = True,
        chat_ids: list[str] | None = None,
    ) -> None:
        for chat_id in chat_ids or self.all_chat_ids(include_copy):
            self._post_multipart("sendPhoto", {"chat_id": chat_id, "caption": caption}, "photo", image_path)

    def send_document(
        self,
        file_path: Path,
        caption: str = "",
        filename: str | None = None,
        include_copy: bool = True,
        chat_ids: list[str] | None = None,
    ) -> None:
        for chat_id in chat_ids or self.all_chat_ids(include_copy):
            self._post_multipart(
                "sendDocument",
                {"chat_id": chat_id, "caption": caption},
                "document",
                file_path,
                upload_filename=filename,
            )

    def ask_for_otp(self, timeout_seconds: int, reminder_interval_seconds: int = 0) -> str:
        self.send_message("管理端需要 Google 驗證碼，請直接回覆 6 位數驗證碼。", self.all_chat_ids())
        deadline = time.time() + timeout_seconds
        next_reminder = time.time() + reminder_interval_seconds if reminder_interval_seconds else None
        while time.time() < deadline:
            for update in self.get_updates(timeout=15):
                if not self._is_allowed(update):
                    continue
                match = re.search(r"\b(\d{6,8})\b", update.text)
                if match:
                    return match.group(1)
            if next_reminder is not None and time.time() >= next_reminder:
                remaining_minutes = max(1, int((deadline - time.time() + 59) // 60))
                self.send_message(
                    f"仍在等待 Google 驗證碼，請直接回覆 6 位數驗證碼。剩餘約 {remaining_minutes} 分鐘。",
                    self.all_chat_ids(),
                )
                next_reminder = time.time() + reminder_interval_seconds
            time.sleep(1)
        raise TelegramError("等待 Telegram Google 驗證碼逾時。")

    def drain_updates(self) -> None:
        updates = self.get_updates(timeout=1)
        if updates:
            self._save_offset(max(update.update_id for update in updates) + 1)

    def get_updates(self, timeout: int = 10) -> list[TelegramUpdate]:
        offset = self._load_offset()
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            params["offset"] = offset
        data = self._post("getUpdates", params, timeout=timeout + 5)
        result = data.get("result") or []
        updates: list[TelegramUpdate] = []
        max_update_id = offset - 1 if offset else None
        for item in result:
            update_id = int(item.get("update_id"))
            max_update_id = max(update_id, max_update_id or update_id)
            msg = item.get("message") or {}
            cb = item.get("callback_query") or {}
            if cb:
                cb_msg = cb.get("message") or {}
                chat = cb_msg.get("chat") or {}
                sender = cb.get("from") or {}
                text = ""
                callback_id = str(cb.get("id") or "")
                callback_data = str(cb.get("data") or "")
            else:
                chat = msg.get("chat") or {}
                sender = msg.get("from") or {}
                text = str(msg.get("text") or "")
                callback_id = ""
                callback_data = ""
            updates.append(
                TelegramUpdate(
                    update_id=update_id,
                    chat_id=str(chat.get("id") or ""),
                    text=text,
                    username=str(sender.get("username") or chat.get("username") or ""),
                    callback_id=callback_id,
                    callback_data=callback_data,
                )
            )
        if max_update_id is not None:
            self._save_offset(max_update_id + 1)
        return updates

    def is_allowed(self, update: TelegramUpdate) -> bool:
        return self._is_allowed(update)

    def _is_allowed(self, update: TelegramUpdate) -> bool:
        if update.chat_id in self.allowed_chat_ids:
            return True
        return update.username.lower().lstrip("@") in self.allowed_usernames

    def _load_offset(self) -> int | None:
        if not self.offset_path.exists():
            return None
        try:
            data = json.loads(self.offset_path.read_text(encoding="utf-8"))
            value = data.get("offset")
            return int(value) if value is not None else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _save_offset(self, offset: int) -> None:
        self.offset_path.write_text(json.dumps({"offset": offset}, ensure_ascii=False), encoding="utf-8")

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _post(self, method: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        if not self.token:
            raise TelegramError("未設定 TELEGRAM_BOT_TOKEN。")
        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(self._api_url(method), data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramError(f"Telegram API 請求失敗: {method}: {exc}") from exc
        if not data.get("ok"):
            raise TelegramError(f"Telegram API 返回錯誤: {method}: {data}")
        return data

    def _post_multipart(
        self,
        method: str,
        params: dict[str, str],
        file_field: str,
        file_path: Path,
        upload_filename: str | None = None,
    ) -> dict[str, Any]:
        boundary = "----AnubisAutoReport" + uuid4().hex
        parts: list[bytes] = []
        for key, value in params.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        filename = upload_filename or file_path.name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(file_path.read_bytes())
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(self._api_url(method), data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramError(f"Telegram 檔案發送失敗: {method}: {exc}") from exc
        if not data.get("ok"):
            raise TelegramError(f"Telegram API 返回錯誤: {method}: {data}")
        return data
