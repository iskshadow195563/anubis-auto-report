from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


JXA_SCRIPT = r"""
ObjC.import("Foundation");

function readTextFile(path) {
  var ns = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  if (ns === undefined || ns === null) {
    throw new Error("Cannot read JS file: " + path);
  }
  return ObjC.unwrap(ns);
}

function chromeApp() {
  var chrome = Application("Google Chrome");
  chrome.includeStandardAdditions = true;
  return chrome;
}

function findWindow(chrome, idText) {
  var windows = chrome.windows();
  for (var i = 0; i < windows.length; i++) {
    try {
      if (String(windows[i].id()) === String(idText)) {
        return windows[i];
      }
    } catch (err) {}
  }
  throw new Error("Chrome window not found: " + idText);
}

function tabFrom(win, tabIndexText) {
  var tabs = win.tabs();
  var idx = Number(tabIndexText || "1") - 1;
  if (idx < 0 || idx >= tabs.length) {
    throw new Error("Chrome tab index out of range: " + tabIndexText);
  }
  return tabs[idx];
}

function listTabs(chrome) {
  var result = [];
  var windows = chrome.windows();
  for (var i = 0; i < windows.length; i++) {
    var win = windows[i];
    var activeIndex = 1;
    try { activeIndex = win.activeTabIndex(); } catch (err) {}
    var tabs = win.tabs();
    for (var j = 0; j < tabs.length; j++) {
      var tab = tabs[j];
      result.push({
        windowId: String(win.id()),
        windowIndex: i + 1,
        tabIndex: j + 1,
        windowTabCount: tabs.length,
        active: j + 1 === activeIndex,
        title: String(tab.title() || ""),
        url: String(tab.url() || "")
      });
    }
  }
  return result;
}

function run(argv) {
  var action = argv[0] || "list";
  var chrome = chromeApp();

  if (action === "list") {
    return JSON.stringify(listTabs(chrome));
  }

  if (action === "exec") {
    var win = findWindow(chrome, argv[1]);
    var tab = tabFrom(win, argv[2]);
    var js = readTextFile(argv[3]);
    return String(tab.execute({ javascript: js }) || "");
  }

  if (action === "navigate") {
    var win2 = findWindow(chrome, argv[1]);
    var tab2 = tabFrom(win2, argv[2]);
    tab2.url = argv[3];
    return "ok";
  }

  if (action === "reload") {
    var winReload = findWindow(chrome, argv[1]);
    var tabReload = tabFrom(winReload, argv[2]);
    try {
      tabReload.reload();
    } catch (errReload) {
      chrome.reload(tabReload);
    }
    return "ok";
  }

  if (action === "newWindow") {
    var before = {};
    var beforeWindows = chrome.windows();
    for (var b = 0; b < beforeWindows.length; b++) {
      try { before[String(beforeWindows[b].id())] = true; } catch (errBefore) {}
    }

    var created = new chrome.Window();
    chrome.windows.push(created);

    var afterWindows = chrome.windows();
    var targetWin = null;
    for (var a = 0; a < afterWindows.length; a++) {
      var candidateId = "";
      try { candidateId = String(afterWindows[a].id()); } catch (errId) {}
      if (candidateId && !before[candidateId]) {
        targetWin = afterWindows[a];
        break;
      }
    }
    if (targetWin === null) {
      throw new Error("Cannot locate newly created Chrome window");
    }

    try { chrome.activate(); } catch (errActivate) {}
    try { targetWin.activeTabIndex = 1; } catch (errIndex) {}
    var newTabs = targetWin.tabs();
    if (!newTabs.length) {
      throw new Error("New Chrome window has no tab");
    }
    newTabs[0].url = argv[1];
    return JSON.stringify({
      windowId: String(targetWin.id()),
      windowIndex: 1,
      tabIndex: 1,
      active: true,
      title: String(newTabs[0].title() || ""),
      url: String(newTabs[0].url() || "")
    });
  }

  if (action === "activate") {
    var win3 = findWindow(chrome, argv[1]);
    try { chrome.activate(); } catch (err) {}
    try { win3.activeTabIndex = Number(argv[2] || "1"); } catch (err2) {}
    return "ok";
  }

  throw new Error("Unknown action: " + action);
}
"""


EXTRACT_JS = r"""
(() => {
  const visibleText = (el) => (el && el.innerText ? el.innerText.trim() : "");
  const tables = Array.from(document.querySelectorAll("table")).map((table) =>
    Array.from(table.querySelectorAll("tr")).map((tr) =>
      Array.from(tr.querySelectorAll("th,td")).map((cell) => cell.innerText.trim())
    )
  );
  const inputs = Array.from(document.querySelectorAll("input,textarea")).map((el) => ({
    type: el.type || el.tagName.toLowerCase(),
    name: el.name || "",
    id: el.id || "",
    placeholder: el.placeholder || "",
    autocomplete: el.autocomplete || "",
    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  }));
  return JSON.stringify({
    url: location.href,
    title: document.title,
    text: visibleText(document.body),
    tables,
    inputs
  });
})()
"""


LOGIN_DETECT_JS = r"""
(() => {
  const text = (document.body && document.body.innerText || "").toLowerCase();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const inputs = Array.from(document.querySelectorAll("input,textarea")).filter(visible);
  const hasPassword = inputs.some((el) => (el.type || "").toLowerCase() === "password");
  const hasOtp = inputs.some((el) => {
    const s = [el.name, el.id, el.placeholder, el.autocomplete, el.getAttribute("aria-label")].join(" ").toLowerCase();
    return /totp|otp|2fa|auth|code|token|google|验证码|驗證碼|验证|驗証/.test(s);
  });
  const hasReport = /今日转入|今日轉入/.test(document.body ? document.body.innerText : "");
  return JSON.stringify({ url: location.href, title: document.title, hasPassword, hasOtp, hasReport });
})()
"""


RELOAD_JS = r"""
(() => {
  location.reload();
  return "ok";
})()
"""


def build_fill_login_js(username: str, password: str) -> str:
    return f"""
(() => {{
  const username = {json.dumps(username)};
  const password = {json.dumps(password)};
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const setValue = (el, value) => {{
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const inputs = Array.from(document.querySelectorAll("input,textarea")).filter(visible);
  const passwordInput = inputs.find((el) => (el.type || "").toLowerCase() === "password");
  const userInput = inputs.find((el) => {{
    const s = [el.name, el.id, el.placeholder, el.autocomplete, el.type].join(" ").toLowerCase();
    return /user|account|login|email|phone|账号|帳號|用户名|用戶名/.test(s) && (el.type || "text").toLowerCase() !== "password";
  }}) || inputs.find((el) => (el.type || "text").toLowerCase() !== "password");
  if (!userInput || !passwordInput) {{
    return JSON.stringify({{ ok: false, error: "login inputs not found" }});
  }}
  setValue(userInput, username);
  setValue(passwordInput, password);
  const buttons = Array.from(document.querySelectorAll("button,input[type=submit],input[type=button]")).filter(visible);
  const button = buttons.find((el) => /login|sign in|登录|登入|提交|確認|确认/.test((el.innerText || el.value || "").toLowerCase())) || buttons[0];
  if (button) {{
    button.click();
  }} else {{
    passwordInput.form && passwordInput.form.requestSubmit ? passwordInput.form.requestSubmit() : passwordInput.dispatchEvent(new KeyboardEvent("keydown", {{ key: "Enter", bubbles: true }}));
  }}
  return JSON.stringify({{ ok: true }});
}})()
"""


def build_fill_otp_js(code: str) -> str:
    return f"""
(() => {{
  const code = {json.dumps(code)};
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const setValue = (el, value) => {{
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const inputs = Array.from(document.querySelectorAll("input,textarea")).filter(visible);
  const otpInput = inputs.find((el) => {{
    const s = [el.name, el.id, el.placeholder, el.autocomplete, el.getAttribute("aria-label")].join(" ").toLowerCase();
    return /totp|otp|2fa|auth|code|token|google|验证码|驗證碼|验证|驗証/.test(s);
  }}) || inputs.find((el) => (el.maxLength >= 4 && el.maxLength <= 8)) || inputs[inputs.length - 1];
  if (!otpInput) {{
    return JSON.stringify({{ ok: false, error: "otp input not found" }});
  }}
  setValue(otpInput, code);
  const buttons = Array.from(document.querySelectorAll("button,input[type=submit],input[type=button]")).filter(visible);
  const button = buttons.find((el) => /confirm|verify|submit|登录|登入|提交|確認|确认|验证|驗證/.test((el.innerText || el.value || "").toLowerCase())) || buttons[0];
  if (button) {{
    button.click();
  }} else {{
    otpInput.form && otpInput.form.requestSubmit ? otpInput.form.requestSubmit() : otpInput.dispatchEvent(new KeyboardEvent("keydown", {{ key: "Enter", bubbles: true }}));
  }}
  return JSON.stringify({{ ok: true }});
}})()
"""


@dataclass(frozen=True)
class ChromeTab:
    window_id: str
    window_index: int
    tab_index: int
    window_tab_count: int
    url: str
    title: str
    active: bool = False


class ChromeError(RuntimeError):
    pass


class ChromeController:
    def __init__(self, admin_url: str, admin_domain: str, poll_seconds: float = 2.0):
        self.admin_url = admin_url
        self.admin_domain = admin_domain
        self.poll_seconds = poll_seconds
        self._script_path = self._ensure_jxa_script()

    def list_tabs(self) -> list[ChromeTab]:
        raw = self._osascript(["list"], timeout=35)
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise ChromeError(f"Chrome 返回的視窗資料不是 JSON: {raw[:200]}") from exc
        tabs = []
        for item in data:
            tabs.append(
                ChromeTab(
                    window_id=str(item.get("windowId") or ""),
                    window_index=int(item.get("windowIndex") or 0),
                    tab_index=int(item.get("tabIndex") or 0),
                    window_tab_count=int(item.get("windowTabCount") or 0),
                    url=str(item.get("url") or ""),
                    title=str(item.get("title") or ""),
                    active=bool(item.get("active")),
                )
            )
        return tabs

    def ensure_report_tab(self, force_new: bool = False) -> ChromeTab:
        self._open_chrome_if_needed()
        if force_new:
            return self.open_report_tab()

        tabs = self.list_tabs()
        domain_tabs = [tab for tab in tabs if self.admin_domain in tab.url]
        if domain_tabs:
            isolated_tabs = [tab for tab in domain_tabs if tab.window_tab_count <= 6]
            if isolated_tabs:
                report_tabs = [tab for tab in isolated_tabs if "/order/liquidity" in tab.url]
                return self._select_best_tab(report_tabs or isolated_tabs)
            return self.open_report_tab()

        active = next((tab for tab in tabs if tab.active), None) or (tabs[0] if tabs else None)
        if active is None:
            subprocess.run(["open", "-a", "Google Chrome", self.admin_url], check=False)
            time.sleep(2)
            tabs = self.list_tabs()
            active = next((tab for tab in tabs if tab.active), None) or tabs[0]
        self.navigate(active, self.admin_url)
        return active

    def open_report_tab(self) -> ChromeTab:
        self._open_chrome_if_needed()
        try:
            raw = self._osascript(["newWindow", self.admin_url], timeout=20)
            created = json.loads(raw)
            window_id = str(created.get("windowId") or "")
        except (ChromeError, json.JSONDecodeError) as exc:
            window_id = ""
            subprocess.run(["open", "-a", "Google Chrome", self.admin_url], check=False)
            last_error: Exception | None = exc
        else:
            last_error = None

        deadline = time.time() + 18
        fallback: ChromeTab | None = None
        while time.time() < deadline:
            time.sleep(1)
            try:
                tabs = self.list_tabs()
            except ChromeError as exc:
                last_error = exc
                continue
            window_tabs = [tab for tab in tabs if window_id and tab.window_id == window_id]
            if window_tabs:
                tab = window_tabs[0]
                if self.admin_domain not in tab.url:
                    self.navigate(tab, self.admin_url)
                    continue
                self.activate(tab)
                return self._fresh_tab(tab)

            domain_tabs = [tab for tab in tabs if self.admin_domain in tab.url and tab.window_tab_count <= 6]
            if domain_tabs:
                fallback = self._select_best_tab(domain_tabs)
                self.activate(fallback)
                return self._fresh_tab(fallback)

        if fallback is not None:
            return self._fresh_tab(fallback)
        if last_error is not None:
            raise ChromeError(f"無法開啟 Chrome 專用管理端視窗: {last_error}") from last_error
        raise ChromeError("無法開啟 Chrome 專用管理端視窗")

    def navigate(self, tab: ChromeTab, url: str) -> None:
        self._osascript(["navigate", tab.window_id, str(tab.tab_index), url], timeout=20)
        time.sleep(self.poll_seconds)

    def reload(self, tab: ChromeTab) -> None:
        self._osascript(["reload", tab.window_id, str(tab.tab_index)], timeout=20)
        time.sleep(max(self.poll_seconds, 3))

    def activate(self, tab: ChromeTab) -> None:
        self._osascript(["activate", tab.window_id, str(tab.tab_index)], timeout=12)

    def execute(self, tab: ChromeTab, javascript: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tmp:
            tmp.write(javascript)
            tmp_path = tmp.name
        try:
            return self._osascript(["exec", tab.window_id, str(tab.tab_index), tmp_path], timeout=35)
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass

    def extract_payload(self, tab: ChromeTab) -> dict[str, Any]:
        raw = self.execute(tab, EXTRACT_JS)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChromeError(f"頁面抽取結果不是 JSON: {raw[:300]}") from exc

    def detect_login_state(self, tab: ChromeTab) -> dict[str, Any]:
        raw = self.execute(tab, LOGIN_DETECT_JS)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def _open_chrome_if_needed(self) -> None:
        try:
            self.list_tabs()
            return
        except ChromeError:
            subprocess.run(["open", "-a", "Google Chrome", self.admin_url], check=False)
            time.sleep(3)

    @staticmethod
    def _tab_key(tab: ChromeTab) -> tuple[str, int]:
        return (tab.window_id, tab.tab_index)

    @staticmethod
    def _select_best_tab(tabs: list[ChromeTab]) -> ChromeTab:
        return sorted(
            tabs,
            key=lambda tab: (
                "/order/liquidity" in tab.url,
                tab.window_tab_count <= 6,
                tab.active,
                -tab.window_index,
                tab.tab_index,
            ),
            reverse=True,
        )[0]

    def _fresh_tab(self, tab: ChromeTab) -> ChromeTab:
        tabs = [item for item in self.list_tabs() if self._tab_key(item) == self._tab_key(tab)]
        if not tabs:
            raise ChromeError(f"Chrome tab 已不存在 window_id={tab.window_id} tab={tab.tab_index}")
        return tabs[0]

    def _osascript(self, args: list[str], timeout: int = 25) -> str:
        cmd = ["osascript", "-l", "JavaScript", str(self._script_path), *args]
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            action = args[0] if args else "unknown"
            target = ""
            if action in {"exec", "navigate", "reload", "activate"} and len(args) >= 3:
                target = f" window_id={args[1]} tab={args[2]}"
            raise ChromeError(f"Chrome AppleScript/JXA {action}{target} 超時 {timeout} 秒") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            if "not authorized" in stderr.lower() or "not allowed" in stderr.lower():
                stderr += "\n請到 系統設定 > 隱私權與安全性 > 自動化，允許終端機或本程式控制 Google Chrome。"
            raise ChromeError(stderr or "osascript 執行失敗")
        return (proc.stdout or "").strip()

    def _ensure_jxa_script(self) -> Path:
        path = Path(tempfile.gettempdir()) / "anubis_auto_report_chrome_jxa.js"
        if not path.exists() or path.read_text(encoding="utf-8") != JXA_SCRIPT:
            path.write_text(JXA_SCRIPT, encoding="utf-8")
        return path
