from __future__ import annotations

import argparse
import sys

from .config import load_config
from .logging_setup import configure_logging
from .service import AutoReportService
from .web_gui import run_web_gui, run_web_gui_check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anubis Bridge Chrome auto report")
    parser.add_argument("--once", action="store_true", help="只執行一次")
    parser.add_argument("--daemon", action="store_true", help="24/7 循環執行")
    parser.add_argument("--service", action="store_true", help="每 60 秒刷新並監聽 Telegram /daily")
    parser.add_argument("--gui", action="store_true", help="開啟本地 Web GUI")
    parser.add_argument("--no-browser", action="store_true", help="開啟 Web GUI 但不自動打開瀏覽器")
    parser.add_argument("--gui-port", type=int, default=8765, help="Web GUI 端口，預設 8765")
    parser.add_argument("--gui-check", action="store_true", help="啟動 Web GUI 自檢並輸出日誌")
    parser.add_argument("--no-telegram", action="store_true", help="生成本地報表但不發送 Telegram")
    args = parser.parse_args(argv)

    if args.gui_check:
        print(f"GUI debug log: {run_web_gui_check()}")
        return 0

    if args.gui or not (args.once or args.daemon or args.service):
        run_web_gui(open_browser=not args.no_browser, port=args.gui_port)
        return 0

    config = load_config()
    logger = configure_logging(config)
    service = AutoReportService(config, logger)
    if args.service:
        service.run_service()
    elif args.daemon:
        service.run_forever()
    else:
        image_path, xlsx_path = service.run_once(send_telegram=not args.no_telegram)
        print(f"PNG: {image_path}")
        print(f"XLSX: {xlsx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
