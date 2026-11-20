#!/usr/bin/env python3
"""给 DeepSeek 加"性格"的小中转(OpenAI 兼容接口)。

客户端(TablePlus)请求 -> 本程序:校验访问密码 -> 在对话里塞入性格提示词、限制回答长度 -> 转给 DeepSeek -> 原样(含流式)返回。
- 只监听 127.0.0.1,公网入口由 nginx 的 /llm/ 提供
- DeepSeek 密钥和访问密码都从环境变量读(systemd 的 EnvironmentFile),不写在本文件里
- 性格提示词在 PERSONA_FILE,每次请求都重新读取,改完不用重启
- 不记录任何对话内容(里面可能有你的表结构),只记 时间/状态码/耗时
"""
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("PROXY_PORT", "8766"))
UPSTREAM = os.environ.get("UPSTREAM_URL", "https://api.deepseek.com/chat/completions")
API_KEY = os.environ["DEEPSEEK_API_KEY"]
TOKEN = os.environ["PROXY_TOKEN"]
PERSONA_FILE = os.environ.get("PERSONA_FILE", "/opt/llm-proxy/persona.txt")
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/opt/llm-proxy/settings.json")
_FALLBACK = {"default_model": "deepseek-flash", "max_tokens_cap": 8000,
             "models": {"deepseek-flash": {"upstream": "deepseek-flash", "effort": "high"}}}


def load_settings():
    """每次请求都重新读 settings.json(改完不用重启);读坏了就用保底配置"""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            st = json.load(f)
        assert st["models"][st["default_model"]]
        return st
    except Exception:
        return _FALLBACK
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "30"))          # 每分钟最多请求数
MAX_BODY = 2 * 1024 * 1024
UPSTREAM_TIMEOUT = 180

_lock = threading.Lock()
_hits = []        # 最近一分钟的请求时间
_fails = []       # 最近一分钟的密码错误时间


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


def load_persona():
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def load_extra(name):
    """读某个模式的附加提示词(只允许 /opt/llm-proxy 目录下的文件名,不允许带路径)"""
    if not name or os.path.basename(name) != name:
        return ""
    try:
        with open(os.path.join(os.path.dirname(PERSONA_FILE), name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def strip_tools(req):
    """教学模式:去掉工具调用能力(AI 就没法去查你的库),并清掉历史里的工具调用痕迹"""
    req.pop("tools", None)
    req.pop("tool_choice", None)
    out = []
    for m in req.get("messages", []):
        if m.get("role") == "tool":
            continue
        if m.get("tool_calls"):
            m = {k: v for k, v in m.items() if k != "tool_calls"}
            if not (m.get("content") or "").strip():
                continue
        out.append(m)
    req["messages"] = out


def rate_ok(bucket, limit):
    now = time.time()
    with _lock:
        while bucket and now - bucket[0] > 60:
            bucket.pop(0)
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "llm-proxy"
    sys_version = ""

    def log_request(self, code="-", size="-"):   # 只记 方法/路径/状态码(不记内容、不记查询参数)
        log(f"{self.command} {self.path.split(chr(63),1)[0]} -> {code} ua={self.headers.get(chr(85)+chr(115)+chr(101)+chr(114)+chr(45)+chr(65)+chr(103)+chr(101)+chr(110)+chr(116),chr(45))[:60]}")

    def log_message(self, *a):
        pass

    # ---------- 小工具 ----------
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json(code, {"error": {"message": msg, "type": "proxy_error", "code": code}})

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if got and hmac.compare_digest(got.encode(), TOKEN.encode()):
            return True
        rate_ok(_fails, 10**9)   # 记一次失败
        with _lock:
            now = time.time()
            recent = [t for t in _fails if now - t < 60]
        if len(recent) > 20:      # 一分钟内乱猜超过 20 次,先拖慢
            time.sleep(2)
        self._err(401, "invalid or missing API key")
        return False

    @staticmethod
    def _path(raw):
        p = raw.split("?", 1)[0].rstrip("/")
        return p[3:] if p.startswith("/v1") else p

    # ---------- 路由 ----------
    def do_GET(self):
        p = self._path(self.path)
        if p == "/health":
            return self._json(200, {"ok": True})
        if not self._authed():
            return
        if p == "/models":
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "deepseek"} for m in load_settings()["models"]]})
        self._err(404, "not found")

    def do_POST(self):
        p = self._path(self.path)
        if not self._authed():
            return
        if p != "/chat/completions":
            return self._err(404, "not found")
        if not rate_ok(_hits, RATE_PER_MIN):
            return self._err(429, "too many requests, slow down")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > MAX_BODY:
                return self._err(413, "bad body size")
            req = json.loads(self.rfile.read(n))
        except (ValueError, OSError):
            return self._err(400, "bad json")

        # 1) 模型和思考强度:按 settings.json 里"模型名 -> 真实模型+思考设置"翻译;不认识的名字用默认
        st = load_settings()
        name = st.get("aliases", {}).get(req.get("model"), req.get("model"))
        cfg = st["models"].get(name) or st["models"][st["default_model"]]
        req["model"] = cfg["upstream"]
        req.pop("reasoning_effort", None)
        req.pop("thinking", None)
        if cfg.get("thinking") == "disabled":
            req["thinking"] = {"type": "disabled"}
        else:
            req["thinking"] = {"type": "enabled"}
            req["reasoning_effort"] = cfg.get("effort", "high")
        cap = int(st.get("max_tokens_cap", 8000))
        if cfg.get("strip_tools"):
            strip_tools(req)

        # 2) 性格提示词:放最前面;再在最后补一条提醒(模型更看重靠近结尾的指令)
        persona = load_persona()
        extra = load_extra(cfg.get("persona_extra"))
        if extra:
            persona = persona + "\n\n" + extra
        msgs = req.get("messages")
        if not isinstance(msgs, list):
            return self._err(400, "messages required")
        if persona:
            know = "".join("\n\n" + load_extra(n) for n in st.get("knowledge_files", []) if load_extra(n))
            msgs = [{"role": "system", "content": persona + know}] + msgs
            msgs.append({"role": "system", "content": "回答时始终遵守上面「说话风格」的要求。\n" + persona})
        req["messages"] = msgs

        # 3) 长度上限
        mt = req.get("max_tokens")
        req["max_tokens"] = min(mt, cap) if isinstance(mt, int) and mt > 0 else cap

        data = json.dumps(req, ensure_ascii=False).encode()
        stream = bool(req.get("stream"))
        t0 = time.time()

        def open_upstream():
            up = urllib.request.Request(UPSTREAM, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + API_KEY,
                "Accept": "text/event-stream" if stream else "application/json",
            })
            return urllib.request.urlopen(up, timeout=UPSTREAM_TIMEOUT)

        def upstream_error(e):
            body = e.read()
            log(f"POST chat {e.code} upstream-error {time.time()-t0:.1f}s")
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # DeepSeek 偶尔会只思考不说话(回答是空的)。非流式:发现回答为空就自动重试一次。
        if not stream:
            body = None
            for attempt in (1, 2):
                try:
                    resp = open_upstream()
                except urllib.error.HTTPError as e:
                    return upstream_error(e)
                except Exception as e:
                    log(f"POST chat 502 {type(e).__name__}")
                    return self._err(502, "upstream unreachable")
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json")
                status = resp.status
                resp.close()
                try:
                    m = json.loads(body)["choices"][0]["message"]
                    empty = not (m.get("content") or "").strip() and not m.get("tool_calls")
                except Exception:
                    empty = False
                if not empty:
                    break
                log(f"POST chat 回答为空,第 {attempt} 次" + (",重试" if attempt == 1 else ",放弃重试"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log(f"POST chat {status} {req['model']} think={req.get('reasoning_effort', 'off')} tools={len(req.get('tools') or [])} stream=False {len(body)}B {time.time()-t0:.1f}s")
            return

        # 流式:思考内容照常转发;如果整个流结束了还没出现任何正文/工具调用,就再请求一次,把第二次的内容接着发出去
        sent = 0
        headers_sent = False
        for attempt in (1, 2):
            try:
                resp = open_upstream()
            except urllib.error.HTTPError as e:
                if not headers_sent:
                    return upstream_error(e)
                break
            except Exception as e:
                log(f"POST chat 502 {type(e).__name__}")
                if not headers_sent:
                    return self._err(502, "upstream unreachable")
                break
            if not headers_sent:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
            got_content = False
            buf = b""
            try:
                while True:
                    chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n\n" in buf:
                        ev, buf = buf.split(b"\n\n", 1)
                        line = ev.strip()
                        drop = False
                        if line == b"data: [DONE]":
                            drop = attempt == 1 and not got_content
                        elif line.startswith(b"data:"):
                            try:
                                d = json.loads(line[5:])
                                ch = (d.get("choices") or [{}])[0]
                                dl = ch.get("delta") or {}
                                if (dl.get("content") or "") != "" or dl.get("tool_calls"):
                                    got_content = True
                                # 第一次什么都没说就收尾:把这个"结束"事件吞掉,免得客户端以为已经结束
                                if attempt == 1 and not got_content and ch.get("finish_reason"):
                                    drop = True
                            except ValueError:
                                pass
                        if not drop:
                            self.wfile.write(ev + b"\n\n")
                            self.wfile.flush()
                            sent += len(ev) + 2
                if buf.strip():
                    self.wfile.write(buf)
                    self.wfile.flush()
                    sent += len(buf)
            except (BrokenPipeError, ConnectionResetError):
                resp.close()
                break
            finally:
                resp.close()
            if got_content or attempt == 2:
                break
            log("POST chat(流式)回答为空,重试一次")
        log(f"POST chat 200 {req['model']} think={req.get('reasoning_effort', 'off')} tools={len(req.get('tools') or [])} stream=True {sent}B {time.time()-t0:.1f}s")


if __name__ == "__main__":
    if len(TOKEN) < 24:
        sys.exit("PROXY_TOKEN 太短")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log(f"listening on {HOST}:{PORT}")
    srv.serve_forever()
