"""本地启动入口。

    python3 main.py                # 首次自动建库并播种样例,启动服务
    python3 main.py --reset        # 清空数据库重新生成样例
    python3 main.py --port 8080
    python3 main.py --no-bg        # 不启动模拟适配器(手动 /api/tick)

一条命令即可看到:高峰补货、温度持续异常、温度读数缺失、临期重叠、
提前售罄、到期自动下架与闭店报损样例。
"""
from __future__ import annotations

import argparse
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.adapters import SalesAdapter, TemperatureAdapter
from app.db import open_db
from app.seed import STORE, seed
from app.service import HotFoodService
from app.tasks import BackgroundTask, SimClock
from app.web import make_handler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "app" / "static"


def main() -> None:
    ap = argparse.ArgumentParser(description="热柜鲜食出炉与效期报损系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="重建数据库并重新播种")
    ap.add_argument("--scale", type=float, default=30.0, help="模拟时钟倍率(默认30倍)")
    ap.add_argument("--no-bg", action="store_true", help="不启动模拟后台线程")
    args = ap.parse_args()

    db_path = DATA / "hotfood.db"
    if args.reset and db_path.exists():
        for f in DATA.glob("hotfood.db*"):
            f.unlink()

    conn = open_db(db_path)
    clock = SimClock(scale=args.scale)
    svc = HotFoodService(conn, clock=clock.now, attachment_dir=DATA / "attachments")
    temp = TemperatureAdapter()
    sales = SalesAdapter()
    bg = BackgroundTask(svc, STORE[0], clock, temp, sales)

    fresh = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] == 0
    if fresh:
        print("🌱 生成样例数据:高峰补货 / 温度异常 / 临期重叠 / 提前售罄 / 闭店报损 …")
        with svc.lock:
            seed(svc, bg, clock.now())
        print("   样例就绪。")
    if not args.no_bg:
        bg.start()

    handler = make_handler(svc, bg, clock, STATIC)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"♨ 热柜鲜食台已启动: http://localhost:{args.port}")
    print(f"   模拟时钟 {args.scale:g} 倍速 · {'后台模拟运行中' if not args.no_bg else '无后台(可 POST /api/tick)'}")
    print("   Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")
    finally:
        bg.stop()
        httpd.server_close()
        conn.close()


if __name__ == "__main__":
    main()
