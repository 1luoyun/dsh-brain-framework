#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""embed_server.py — bge 向量常驻服务（给 brain.py 的语义层用）。

为什么要它：embed.py 每次 --query 都要重新 import torch + 加载模型，
2026-11-03 实测 6.95 秒 —— recall 一走向量层就是这个代价。常驻后一次加载、
后续每次约 30~60ms。brain.py 必须零依赖，所以它只能通过 HTTP 调这里。

用法（**必须用 comfyui venv 的 python 跑**）：
    python embed_server.py --start     # 后台起（已在跑就复用）
    python embed_server.py --status
    python embed_server.py --stop
    python embed_server.py --serve     # 前台跑（调试）

接口：
    GET /q?text=<urlencoded>&top=20  ->  {"ok":true,"hits":[{"id":..,"sim":..}]}
    GET /health                      ->  {"ok":true,"indexed":N}
"""
import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "brain.db")
PORT = int(os.environ.get("BRAIN_EMBED_PORT") or 7799)
CREATE_NO_WINDOW = 0x08000000

# brain 目录只放源码与数据：import embed 会在同目录生成 __pycache__，
# health 的「目录干净」当场就会红（2026-11-03 实际踩过）。这份服务是常驻的，
# import 只发生一次，关掉写入没有任何代价。
sys.dont_write_bytecode = True

import numpy as np  # noqa: E402
from embed import Encoder  # noqa: E402

_enc = None
_SRV = None
_index = {"m": None, "ids": [], "n": 0}
_LOCK = threading.Lock()


def _load_index():
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    try:
        rows = list(con.execute("SELECT id, v FROM event_emb ORDER BY id"))
    finally:
        con.close()
    if not rows:
        return None, []
    dim = len(rows[0][1]) // 4
    m = np.frombuffer(b"".join(r[1] for r in rows), dtype="float32").reshape(-1, dim)
    return m, [r[0] for r in rows]


def refresh(force=False):
    """索引变了就重载。每 60 秒最多查一次计数。"""
    with _LOCK:
        if not force and _index["m"] is not None:
            con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
            try:
                cur = con.execute("SELECT count(*) FROM event_emb").fetchone()[0]
            except Exception:
                cur = -1
            finally:
                con.close()
            if cur == _index["n"]:
                return
        m, ids = _load_index()
        _index["m"] = m
        _index["ids"] = ids
        _index["n"] = len(ids)


def encode(text):
    global _enc
    with _LOCK:
        if _enc is None:
            _enc = Encoder()
        return _enc.encode([text])[0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path == "/shutdown":
            # 不能在本线程直接 shutdown（serve_forever 正等这个请求返回）。
            if _SRV is not None:
                threading.Thread(target=_SRV.shutdown, daemon=True).start()
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            try:
                refresh()
                return self._json({"ok": True, "indexed": _index["n"]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if u.path == "/sim":
            # 一批文本与 query 的相似度。给 brain.brief 挑「哪套步骤贴题」用 ——
            # 词面匹配分不清「删一个目录」和「删东西前的三步预检」（只有「删」
            # 一个共享字），语义能分清。
            try:
                qs = parse_qs(u.query)
                text = (qs.get("q") or [""])[0]
                texts = json.loads((qs.get("texts") or ["[]"])[0])
                if not text or not texts:
                    return self._json({"ok": False, "error": "empty"}, 400)
                qv = encode(text)
                tv = _enc.encode(list(texts))
                sims = (tv @ qv).tolist()
                return self._json({"ok": True,
                                   "sims": [round(float(s), 4) for s in sims]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if u.path != "/q":
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            qs = parse_qs(u.query)
            text = (qs.get("text") or [""])[0]
            top = int((qs.get("top") or ["20"])[0])
            if not text:
                return self._json({"ok": False, "error": "empty text"}, 400)
            refresh()
            if _index["m"] is None:
                return self._json({"ok": False, "error": "empty index"}, 200)
            qv = encode(text)
            sims = _index["m"] @ qv
            order = np.argsort(-sims)[:top]
            hits = [{"id": _index["ids"][int(k)], "sim": round(float(sims[k]), 4)}
                    for k in order]
            return self._json({"ok": True, "indexed": _index["n"], "hits": hits})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)


def port_open(port=PORT, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def cli(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--serve", action="store_true")
    a = ap.parse_args(argv)

    if a.status:
        print(json.dumps({"ok": True, "running": port_open(), "port": PORT}))
        return 0
    if a.stop:
        if not port_open():
            print(json.dumps({"ok": True, "running": False}))
            return 0
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/shutdown" % PORT,
                                         method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        for _ in range(40):
            if not port_open():
                print(json.dumps({"ok": True, "stopped": True}))
                return 0
            time.sleep(0.25)
        print(json.dumps({"ok": False, "error": "still listening"}))
        return 1
    if a.start:
        if port_open():
            print(json.dumps({"ok": True, "already": True, "port": PORT}))
            return 0
        py = sys.executable
        p = subprocess.Popen([py, os.path.abspath(__file__), "--serve"],
                             cwd=HERE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        for _ in range(120):
            if port_open():
                print(json.dumps({"ok": True, "pid": p.pid, "port": PORT}))
                return 0
            time.sleep(0.25)
        print(json.dumps({"ok": False, "error": "not ready after 30s"}))
        return 1
    if a.serve:
        # 启动即加载模型 + 建索引：别让第一个用户等那 1 秒。
        global _enc, _SRV
        try:
            _enc = Encoder()
            refresh(True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": "warmup: %s" % e}))
            return 1
        try:
            _SRV = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        print(json.dumps({"ok": True, "serving": PORT}), flush=True)
        _SRV.serve_forever()
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
