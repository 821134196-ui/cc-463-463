#!/usr/bin/env python3
"""一条本地命令启动：生成高峰补货 / 温度异常 / 闭店报损样例并启动服务。

用法：
    python3 run.py                # 重建样例数据并启动（默认）
    python3 run.py --keep-data    # 保留现有数据，仅启动
    python3 run.py --port 8000
"""
import argparse
import sys
import time

from hotcab.adapters import SalesAdapter, TempAdapter
from hotcab.clock import SystemClock
from hotcab.database import connect
from hotcab.scheduler import Scheduler
from hotcab import config, seed
from hotcab.webapp import create_server

STORE_ID = 1


def build_scheduler(svc):
    scenario = seed.load_scenario()
    temp = TempAdapter()
    sales = SalesAdapter()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, batch_no FROM batches").fetchall()
    no2id = {r["batch_no"]: r["id"] for r in rows}
    for no in scenario.get("faulty", []):
        if no in no2id:
            temp.mark_faulty(no2id[no], temp=52.0)
    for no in scenario.get("hot", []):
        if no in no2id:
            sales.mark_hot(no2id[no])
    sched = Scheduler(svc, STORE_ID, temp_adapter=temp,
                      sales_adapter=sales, log=print)
    return sched


def main(argv=None):
    parser = argparse.ArgumentParser(description="热柜鲜食效期与报损系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--keep-data", action="store_true",
                        help="不重建样例数据")
    args = parser.parse_args(argv)

    config.ensure_dirs()
    if args.keep_data:
        print("保留现有数据启动…")
    else:
        print("重建数据库并生成样例（高峰补货 / 温度异常 / 闭店报损）…")
        seed.seed_all(reset=True)
        print("样例数据已生成。")

    httpd, app = create_server(args.host, args.port,
                               clock=SystemClock())
    sched = build_scheduler(app.svc)
    sched.start()

    url = f"http://{args.host}:{args.port}/"
    print("=" * 62)
    print(" 热柜鲜食出炉与效期报损系统已启动")
    print(f" 打开 {url}")
    print(" 身份可在右上角切换：店员（仅预设折扣）/ 店长（例外价）")
    print(" Ctrl+C 退出")
    print("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
        sched.stop()
        httpd.shutdown()
        time.sleep(0.3)
        print("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
