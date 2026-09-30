"""HTTP 服务:JSON API + 单页界面。

启动:python3 main.py
界面页签:制作批次 / 热柜陈列图 / 效期队列 / 温度记录 / 销售差异 / 下架清点 / 日报
"""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import ConflictError, DomainError, NEAR_WINDOW
from .tasks import BackgroundTask, SimClock

STORE_ID = "S1"


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def make_handler(svc, bg: BackgroundTask, clock: SimClock, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静默,保持终端清爽

        # ---------------- 工具 ---------------- #
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _err(self, exc: Exception, code=400):
            self._json({"error": str(exc)}, code)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            raw = self.rfile.read(n).decode()
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return json.loads(raw or "{}")
            return {k: v[0] for k, v in parse_qs(raw).items()}

        # ---------------- 路由 ---------------- #
        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            try:
                if path == "/" or path == "/index.html":
                    self._send(200, (static_dir / "index.html").read_bytes(),
                               "text/html; charset=utf-8")
                elif path == "/app.js":
                    self._send(200, (static_dir / "app.js").read_bytes(),
                               "application/javascript; charset=utf-8")
                elif path == "/styles.css":
                    self._send(200, (static_dir / "styles.css").read_bytes(),
                               "text/css; charset=utf-8")
                elif path == "/api/state":
                    self._state()
                elif path == "/api/events":
                    with svc.lock:
                        self._json([dict(e) for e in svc.events(STORE_ID, int(parse_qs(u.query).get("limit", ["120"])[0]))])
                elif path == "/api/temps":
                    with svc.lock:
                        rows = svc.conn.execute(
                            """SELECT t.*, b.batch_no, p.name product_name FROM temp_readings t
                               JOIN batches b ON b.id=t.batch_id JOIN products p ON p.id=b.product_id
                               WHERE b.store_id=? ORDER BY t.ts DESC LIMIT 150""", (STORE_ID,)).fetchall()
                        self._json([dict(r) for r in rows])
                elif path == "/api/sales":
                    with svc.lock:
                        rows = svc.conn.execute(
                            """SELECT s.*, b.batch_no, p.name product_name, p.base_price FROM sales s
                               JOIN batches b ON b.id=s.batch_id JOIN products p ON p.id=b.product_id
                               WHERE b.store_id=? ORDER BY s.ts DESC LIMIT 150""", (STORE_ID,)).fetchall()
                        self._json([dict(r) for r in rows])
                elif path == "/api/attachment":
                    q = parse_qs(u.query)
                    rid = q.get("id", [""])[0]
                    with svc.lock:
                        row = svc.conn.execute(
                            "SELECT attachment FROM temp_readings WHERE id=?", (rid,)).fetchone()
                    if not row or not row["attachment"]:
                        self._json({"error": "无附件"}, 404)
                    else:
                        f = svc.attachment_dir / row["attachment"]
                        self._send(200, f.read_bytes(), "application/octet-stream")
                elif path == "/api/report":
                    q = parse_qs(u.query)
                    now = clock.now()
                    end = float(q.get("end", [now])[0])
                    hours = float(q.get("hours", ["24"])[0])
                    with svc.lock:
                        self._json(svc.daily_report(STORE_ID, end - hours * 3600, end))
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._err(exc, 500)

        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
            try:
                data = self._body()
                with svc.lock:
                    self._route_post(path, data)
                    svc.commit()
            except (ConflictError,) as exc:
                self._err(exc, 409)
            except DomainError as exc:
                self._err(exc, 400)
            except (ValueError, KeyError) as exc:
                self._err(DomainError(f"参数错误: {exc}"), 400)
            except Exception as exc:
                self._err(exc, 500)

        def _route_post(self, path: str, d: dict):
            role = d.get("role", "clerk")
            actor = d.get("actor") or ("值班店长" if role == "manager" else "当班店员")
            if path == "/api/batch/make":
                bid = svc.make_batch(d["product_id"], int(d["qty"]), actor)
                self._json({"batch_id": bid})
            elif path == "/api/batch/probe":
                import base64
                attachment = base64.b64decode(d["attachment_b64"]) if d.get("attachment_b64") else None
                r = svc.record_probe(d["batch_id"], float(d["temp"]), actor,
                                     attachment=attachment, note=d.get("note", ""))
                self._json(r)
            elif path == "/api/batch/place":
                svc.place_on_shelf(d["batch_id"], d["shelf_id"], actor)
                self._json({"ok": True})
            elif path == "/api/sell":
                r = svc.sell(d["batch_id"], int(d["qty"]), actor, role=role,
                             expected_version=int(d["expected_version"]) if d.get("expected_version") not in (None, "") else None)
                self._json(r)
            elif path == "/api/adjust":
                svc.adjust_qty(d["batch_id"], int(d["new_qty"]),
                               int(d["expected_version"]), actor)
                self._json({"ok": True})
            elif path == "/api/discount":
                svc.set_discount(d["batch_id"], float(d["discount"]), actor, role,
                                 force=bool(d.get("force")))
                self._json({"ok": True})
            elif path == "/api/count":
                r = svc.physical_count(d["batch_id"], int(d["counted_qty"]), actor)
                self._json(r)
            elif path == "/api/waste":
                r = svc.confirm_waste(d["batch_id"], int(d["counted_qty"]), actor,
                                      reason=d.get("reason"))
                self._json(r)
            elif path == "/api/restore":
                svc.restore_from_pending(d["batch_id"], actor, d.get("note", ""))
                self._json({"ok": True})
            elif path == "/api/close":
                ids = svc.close_store(STORE_ID, actor)
                self._json({"pending": ids})
            elif path == "/api/tick":
                counts = bg.tick()
                self._json({"counts": counts, "now": clock.now()})
            else:
                self._json({"error": "not found"}, 404)

        # ---------------- 聚合状态 ---------------- #
        def _state(self):
            with svc.lock:
                now = clock.now()
                products = [dict(r) for r in svc.conn.execute(
                    "SELECT * FROM products WHERE store_id=? ORDER BY category,code", (STORE_ID,))]
                shelves = [dict(r) for r in svc.list_shelves(STORE_ID)]
                batches = []
                for b in svc.list_batches(STORE_ID):
                    d = dict(b)
                    d["minutes_to_expire"] = (round((b["expires_at"] - now) / 60, 1)
                                              if b["expires_at"] else None)
                    d["minutes_on_shelf"] = (round((now - b["out_at"]) / 60, 1)
                                             if b["out_at"] else None)
                    batches.append(d)
                # 陈列图:层位 -> 在柜批次
                placed = {}
                for b in batches:
                    if b["shelf_id"] and b["status"] in ("placed", "pending"):
                        placed.setdefault(b["shelf_id"], []).append(b["id"])
                shelf_load = {sid: {"batch_ids": ids} for sid, ids in placed.items()}
                self._json({
                    "now": now,
                    "near_window": NEAR_WINDOW / 60,
                    "store": STORE_ID,
                    "products": products,
                    "shelves": shelves,
                    "shelf_load": shelf_load,
                    "batches": batches,
                })

    return Handler
