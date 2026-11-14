#!/usr/bin/env python3
"""Generic Figwright stdio client. No machine-specific path is embedded.

Set FIGWRIGHT_SERVER to a shell command or JSON argv array, and optionally
FIGWRIGHT_SESSION_ID. Example:
  export FIGWRIGHT_SERVER='node /path/to/@figwright/mcp/dist/index.mjs'
  export FIGWRIGHT_SESSION_ID='your-file-session'

Capability-discovery resilience (tools/list) — observed failure in
iteration-1 live runs: the editor-side tools/list request repeatedly timed
out while tools/call worked fine. tools/list is OPTIONAL metadata: tool
names are fixed by the SOP contract, so discovery must never block the
whole reconstruction.

Optional env configuration:
  FIGWRIGHT_SKIP_TOOLS_LIST=1     never call tools/list; proceed straight to
                                  contract-based tool calls. This is the
                                  explicit skip switch when the editor hangs
                                  on capability discovery.
  FIGWRIGHT_TOOLS_LIST_TIMEOUT=15 per-attempt timeout (seconds).
  FIGWRIGHT_TOOLS_LIST_RETRIES=2 bounded retries before degrading.

After bounded retries tools/list degrades (self.tools == {},
self.tools_list_status == "degraded") instead of raising; only a failed
initialize handshake still aborts.

Session constraint (machine readable, see SESSION_SERIAL below):
  A Figma editor session bound by FIGWRIGHT_SESSION_ID processes
  tools/call serially. Live re-verification against the same session MUST
  be serialized — never run two FW instances / two live verify passes
  concurrently against the same session id.
"""
import json
import os
import select
import shlex
import subprocess
import sys
import time

# Machine-readable record of the Figma plugin session serial limitation.
# Live re-verification must be serial against one session id.
SESSION_SERIAL = {
    "session_mode": "serial",
    "applies_to": "FIGWRIGHT_SESSION_ID bound Figma editor (follower) session",
    "rule": ("tools/call against one session is processed serially; "
             "live re-verification MUST run serially, never two FW "
             "instances concurrently against the same session id"),
}


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _log(msg):
    print("[figwright] " + msg, file=sys.stderr, flush=True)


class FW:
    def __init__(self, session_id=None, client="figma-native-html-reconstruction",
                 server=None, timeout=180):
        server = server or os.environ.get("FIGWRIGHT_SERVER")
        if not server:
            raise RuntimeError("Set FIGWRIGHT_SERVER to the Figwright MCP launch command")
        argv = json.loads(server) if server.lstrip().startswith("[") else shlex.split(server)
        self.timeout = timeout
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self._rid = 0
        # tools/list is optional capability discovery; it must never abort
        # the reconstruction. Status machine-readable values:
        # "not_run" | "skipped" | "degraded" | "ok".
        self.tools_list_status = "not_run"
        self.tools = {}
        try:
            msg = {"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": client, "version": "1"}}}
            self.send(msg)
            self.call_raw(msg)
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            if _env_flag("FIGWRIGHT_SKIP_TOOLS_LIST"):
                _log("tools/list skipped via FIGWRIGHT_SKIP_TOOLS_LIST; "
                     "using SOP contract tool names for calls")
                self.tools_list_status = "skipped"
            else:
                self._discover_tools_bounded()
            _log("session constraint: " + SESSION_SERIAL["rule"])
            session_id = session_id or os.environ.get("FIGWRIGHT_SESSION_ID")
            if session_id:
                self.call("use_file", sessionId=session_id)
        except Exception:
            self.close()
            raise

    def _discover_tools_bounded(self):
        """tools/list with bounded retry; degrades instead of raising."""
        t = _env_float("FIGWRIGHT_TOOLS_LIST_TIMEOUT", 15.0)
        retries = max(1, _env_int("FIGWRIGHT_TOOLS_LIST_RETRIES", 2))
        last = None
        for attempt in range(1, retries + 1):
            try:
                self.tools = self.list_tools(timeout=t)
                self.tools_list_status = "ok"
                return
            except Exception as e:  # timeout / exit / protocol error
                last = e
                _log(f"tools/list attempt {attempt}/{retries} failed: {e}")
                time.sleep(min(2.0, 0.5 * attempt))
        _log(f"WARN tools/list unavailable after {retries} attempt(s): {last}; "
             "capability discovery degraded, proceeding without tool metadata "
             "(calls still use SOP contract tool names)")
        self.tools_list_status = "degraded"
        self.tools = {}

    def _next(self):
        self._rid += 1
        return self._rid

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call_raw(self, msg, timeout=None):
        rid = msg["id"]
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], max(0.1, deadline-time.time()))
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP exited: " + (self.proc.stderr.read() or ""))
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("id") == rid:
                if "error" in data:
                    raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
                return data.get("result")
        raise TimeoutError(f"MCP request {rid} timed out")

    @staticmethod
    def _unwrap(result):
        content = (result or {}).get("content") or []
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        return result

    def list_tools(self, timeout=None):
        rid = self._next()
        msg = {"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}}
        self.send(msg)  # must transmit before call_raw; omitting this was the
                        # iteration-1 live bug where tools/list "timed out"
                        # because the request was never sent
        result = self.call_raw(msg, timeout=timeout)
        return {t["name"]: t for t in (result or {}).get("tools", [])}

    def call(self, tool, timeout=None, **args):
        rid = self._next()
        self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}})
        return self._unwrap(self.call_raw({"jsonrpc": "2.0", "id": rid}, timeout=timeout))

    def get_node(self, node_id):
        return self.call("get_node", nodeId=node_id).get("node")

    def save_screenshots(self, node_ids, out_dir, fmt="PNG", scale=1):
        return self.call("save_screenshots", nodeIds=list(node_ids), outDir=out_dir,
                         format=fmt, scale=scale, timeout=600)

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
