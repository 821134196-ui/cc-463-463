"""零依赖 WSGI 应用 + wsgiref 开发服务器，提供页面与 JSON API。"""
import base64
import json
import os
import re
from wsgiref.simple_server import make_server, WSGIRequestHandler

from . import config
from .database import connect
from .errors import (AuthError, ConflictError, HotCabError, NotFoundError,
                     StateError, ValidationError)
from .service import BatchService

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STORE_ID = 1

STATUS_LABELS = {
    "draft": "待测温", "checked": "待上柜", "on_sale": "销售中",
    "pending_off": "待下架", "sold_out": "已售罄", "loss": "已报损",
}
REASON_LABELS = {
    "expired": "到期", "temp": "温度持续不合格", "manual": "人工下架",
    "close": "闭店剩余", "wrong_shelf": "层位错误",
}
AUDIT_LABELS = {
    "temp_missing": "温度读数缺失", "wrong_shelf": "上错层位",
    "qty_discrepancy": "批次数量差异", "early_soldout": "提前售罄",
    "near_overlap": "临期重叠", "near_expiry": "进入临期",
    "wrong_off": "误下架已撤销", "close_remain": "闭店剩余",
    "expired": "到期自动下架", "temp_fail": "温度连续不合格",
    "discount": "降价留痕",
}


class QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


class App:
    def __init__(self, clock=None, db_path=None):
        from .clock import SystemClock
        self.clock = clock or SystemClock()
        self.svc = BatchService(self.clock, db_path)
        self.db_path = db_path

    # ---------- 工具 ----------
    def _json_body(self, env):
        try:
            length = int(env.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise ValidationError("请求体格式错误")
        raw = env["wsgi.input"].read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体须为 JSON 对象")
        data.setdefault("_role", env.get("HTTP_X_ROLE", "clerk"))
        data.setdefault("_actor",
                        self._resolve_actor(env.get("HTTP_X_ACTOR", "")))
        return data

    def _resolve_actor(self, actor):
        """X-Actor 传用户名（ASCII），落库留痕用中文姓名。"""
        if not actor:
            return ""
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT name FROM users WHERE username=?",
                               (actor,)).fetchone()
        return row["name"] if row else actor

    def _ref_data(self):
        with connect(self.db_path) as conn:
            products = [dict(r) for r in conn.execute(
                "SELECT * FROM products ORDER BY id")]
            shelves = [dict(r) for r in conn.execute(
                "SELECT s.*, c.code cabinet_code, c.store_id FROM shelves s"
                " JOIN cabinets c ON c.id=s.cabinet_id ORDER BY c.id,"
                " s.position")]
            users = [dict(r) for r in conn.execute(
                "SELECT * FROM users ORDER BY id")]
            store = conn.execute("SELECT * FROM stores WHERE id=?",
                                 (STORE_ID,)).fetchone()
        return {"products": products, "shelves": shelves, "users": users,
                "store": dict(store) if store else None,
                "status_labels": STATUS_LABELS,
                "reason_labels": REASON_LABELS,
                "audit_labels": AUDIT_LABELS,
                "preset_discounts": [{"from_min": m, "rate": r,
                                      "label": l}
                                     for m, r, l in config.PRESET_DISCOUNTS],
                "near_minutes": config.NEAR_EXPIRY_MINUTES}

    # ---------- 路由 ----------
    def api(self, method, path, env):
        data = self._json_body(env) if method == "POST" else {}
        svc, sid = self.svc, STORE_ID

        if method == "GET" and path == "/api/ref":
            return self._ref_data()
        if method == "GET" and path == "/api/batches":
            qs = env.get("QUERY_STRING", "")
            status = None
            m = re.search(r"(?:^|&)status=([^&]+)", qs)
            if m:
                status = m.group(1)
            return {"items": svc.list_batches(store_id=sid, status=status,
                                              date=self.clock.today().isoformat())}
        if method == "POST" and path == "/api/batches":
            b = svc.create_batch(sid, int(data["product_id"]),
                                 int(data["qty"]), actor=data.get("_actor", ""))
            return b
        m_one = re.fullmatch(r"/api/batches/(\d+)", path)
        if m_one and method == "GET":
            return svc.get_batch(int(m_one.group(1)))
        m = re.fullmatch(r"/api/batches/(\d+)/([a-z\-]+)", path)
        if m:
            bid, action = int(m.group(1)), m.group(2)
            ver = data.get("version")
            if method == "GET" and action == "temps":
                return {"items": svc.temp_history(bid)}
            if method == "POST" and action == "check-temp":
                temp = data.get("temp")
                attachment = None
                if data.get("attachment"):
                    attachment = (data["attachment"].get("filename", "shot"),
                                  base64.b64decode(
                                      data["attachment"]["data_base64"]))
                return svc.check_temp(bid, temp, attachment=attachment,
                                      actor=data.get("_actor", ""),
                                      note=data.get("note", ""))
            if method == "POST" and action == "assign":
                return svc.assign_shelf(bid, int(data["shelf_id"]),
                                        actor=data.get("_actor", ""),
                                        expected_version=ver)
            if method == "POST" and action == "sell":
                return svc.sell(bid, int(data["qty"]),
                                expected_version=ver,
                                actor=data.get("_actor", ""))
            if method == "POST" and action == "discount":
                return svc.apply_discount(
                    bid, int(data["rate"]),
                    role=data.get("role", data.get("_role", "clerk")),
                    actor=data.get("_actor", ""), expected_version=ver,
                    label=data.get("label"))
            if method == "POST" and action == "pending-manual":
                return svc.mark_pending_manual(bid,
                                               actor=data.get("_actor", ""),
                                               expected_version=ver)
            if method == "POST" and action == "restore":
                return svc.restore_wrong_off(bid,
                                             actor=data.get("_actor", ""),
                                             expected_version=ver)
            if method == "POST" and action == "loss":
                b, meta = svc.confirm_loss(
                    bid, int(data["counted_qty"]),
                    actor=data.get("_actor", ""), expected_version=ver,
                    reason=data.get("reason"))
                return {"batch": b, "meta": meta}
            raise NotFoundError("未知操作")
        if method == "GET" and path == "/api/queue":
            return {"items": svc.near_queue(sid)}
        if method == "GET" and path == "/api/pending":
            return {"items": svc.pending_list(sid)}
        if method == "GET" and path == "/api/cabinet":
            return {"cabinets": svc.cabinet_view(sid)}
        if method == "GET" and path == "/api/sales":
            return {"items": svc.sales_list(sid)}
        if method == "GET" and path == "/api/losses":
            return {"items": svc.loss_list(sid)}
        if method == "GET" and path == "/api/audit":
            qs = env.get("QUERY_STRING", "")
            et = None
            m = re.search(r"(?:^|&)event_type=([^&]+)", qs)
            if m:
                et = m.group(1)
            return {"items": svc.audit_list(store_id=sid, event_type=et)}
        if method == "GET" and path == "/api/report":
            return svc.daily_report(sid)
        if method == "POST" and path == "/api/store/close":
            return svc.close_store(sid, actor=data.get("_actor", "manager"))
        raise NotFoundError("接口不存在")

    # ---------- WSGI ----------
    def __call__(self, env, start_response):
        path = env["PATH_INFO"]
        method = env["REQUEST_METHOD"]
        try:
            if path.startswith("/api/"):
                payload = self.api(method, path, env)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                start_response("200 OK", [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-store")])
                return [body]
            return self._serve_static(path, start_response)
        except ConflictError as e:
            return self._error(start_response, 409, "Conflict",
                               "版本冲突", str(e), code="version_conflict")
        except AuthError as e:
            return self._error(start_response, 403, "Forbidden",
                               "权限不足", str(e), code="forbidden")
        except NotFoundError as e:
            return self._error(start_response, 404, "Not Found", "未找到",
                               str(e))
        except (StateError, ValidationError) as e:
            return self._error(start_response, 400, "Bad Request",
                               "操作被拒绝", str(e))
        except (KeyError, ValueError, TypeError) as e:
            return self._error(start_response, 400, "Bad Request",
                               "参数错误", str(e))

    def _error(self, start_response, status, reason, title, message,
               code=None):
        body = json.dumps({"error": title, "message": message,
                           "code": code or title}, ensure_ascii=False) \
            .encode("utf-8")
        start_response(f"{status} {reason}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store")])
        return [body]

    def _serve_static(self, path, start_response):
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = path.lstrip("/")
        # 附件访问
        if rel.startswith("attachments/"):
            return self._serve_attachment(rel, start_response)
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            start_response("404 Not Found",
                           [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Not Found"]
        ctype = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(
                     os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        start_response("200 OK", [("Content-Type", ctype),
                                  ("Cache-Control", "no-store")])
        return [body]

    def _serve_attachment(self, rel, start_response):
        name = os.path.basename(rel)
        full = os.path.join(config.ATTACH_DIR, name)
        if not os.path.isfile(full):
            start_response("404 Not Found",
                           [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Not Found"]
        with open(full, "rb") as f:
            body = f.read()
        start_response("200 OK", [
            ("Content-Type", "image/jpeg"),
            ("Cache-Control", "private, max-age=60")])
        return [body]


def create_server(host="127.0.0.1", port=8000, app=None, clock=None,
                  db_path=None):
    app = app or App(clock=clock, db_path=db_path)
    return make_server(host, port, app, handler_class=QuietHandler), app
