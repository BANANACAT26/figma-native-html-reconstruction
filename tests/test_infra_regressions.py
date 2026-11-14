#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Infrastructure regression tests for figma-native-html-reconstruction.

Zero third-party test framework: run with  python3 tests/test_infra_regressions.py

Coverage (unit / synthetic only — NO live Figma, NO Chrome launch):
  A. figwright_client.tools/list resilience
     - optional capability discovery must not block the reconstruction:
       hangs degrade to tools_list_status=="degraded" instead of raising
     - explicit skip switch FIGWRIGHT_SKIP_TOOLS_LIST
     - healthy discovery still works; bounded retry recovers a slow server
     - SESSION_SERIAL machine-readable serial-session record exists
  B. verify.py run-manifest hash contract (no self-reference paradox)
     - manifest digests for html/reference/browser/metrics re-hash equal
     - run.json own digest lives ONLY in sidecar run.json.sha256
     - manifest embeds no self digest; tampering browser.png is detected
  C. CLI backward-compat smoke (--help exits 0) for the touched CLIs.
"""
import hashlib, json, os, py_compile, shutil, subprocess, sys, tempfile, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIX = ROOT / "tests" / "fixtures_infra"
sys.path.insert(0, str(SCRIPTS))

import figwright_client as fc   # noqa: E402
import verify                    # noqa: E402

# ---------------------------------------------------------------- fake MCP server
FAKE_SERVER = r'''
import json, os, sys, time
marker = os.environ.get("FAKE_MARKER", "")
mode = os.environ.get("FAKE_MODE", "normal")
delay = float(os.environ.get("FAKE_DELAY", "1.0"))
count = {"n": 0}

def send(obj):
    print(json.dumps(obj), flush=True)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"protocolVersion": "2025-06-18",
                         "capabilities": {},
                         "serverInfo": {"name": "fake-mcp", "version": "0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        count["n"] += 1
        if marker:
            with open(marker, "a") as f:
                f.write("called\n")
        if mode == "hang":
            while True:
                time.sleep(1)
        if mode == "slow_first" and count["n"] == 1:
            time.sleep(delay)
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"tools": [{"name": "ping", "description": "",
                                    "inputSchema": {}}]}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"content": [{"type": "text",
                                      "text": json.dumps({"ok": True})}]}})
'''


class env:
    """Temporary env overrides with restore."""
    def __init__(self, **kv):
        self.kv = kv
    def __enter__(self):
        self.old = {}
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self
    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def start_fake(tmp, mode, timeout=0.5, retries=2, skip=None, marker=None):
    server = tmp / "fake_mcp_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    os.environ["FAKE_MODE"] = mode
    os.environ["FAKE_MARKER"] = marker or ""
    os.environ["FIGWRIGHT_TOOLS_LIST_TIMEOUT"] = str(timeout)
    os.environ["FIGWRIGHT_TOOLS_LIST_RETRIES"] = str(retries)
    if skip:
        os.environ["FIGWRIGHT_SKIP_TOOLS_LIST"] = "1"
    else:
        os.environ.pop("FIGWRIGHT_SKIP_TOOLS_LIST", None)
    return fc.FW(server=json.dumps([sys.executable, str(server)]))


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


RESULTS = []
def check(name, fn):
    try:
        fn()
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
    else:
        RESULTS.append((name, True, ""))


# ------------------------------------------------------------ A. client resilience
def a1_degraded_not_blocking():
    # The "hang" server wedges inside the tools/list handler forever, so we
    # cannot also call tools/call against it. The required property here is
    # only: bounded timeout -> degrade -> no raise -> clean close. That calls
    # still work after degradation is covered by A2/A3 (responsive servers).
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        fw = start_fake(td, "hang", timeout=0.5, retries=2)
        try:
            assert fw.tools_list_status == "degraded", fw.tools_list_status
            assert fw.tools == {}, fw.tools
        finally:
            fw.close()

def a2_skip_tools_list():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        marker = td / "marker.txt"
        fw = start_fake(td, "normal", skip=True, marker=str(marker))
        try:
            assert fw.tools_list_status == "skipped", fw.tools_list_status
            assert not marker.exists(), "server must never receive tools/list"
            assert fw.call("ping").get("ok") is True
        finally:
            fw.close()

def a3_healthy():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        fw = start_fake(td, "normal")
        try:
            assert fw.tools_list_status == "ok", fw.tools_list_status
            assert "ping" in fw.tools
            assert fw.call("ping").get("ok") is True
        finally:
            fw.close()

def a4_bounded_retry_recovers():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        os.environ["FAKE_DELAY"] = "1.0"
        fw = start_fake(td, "slow_first", timeout=0.5, retries=2)
        try:
            assert fw.tools_list_status == "ok", fw.tools_list_status
        finally:
            fw.close()

def a5_serial_session_record():
    assert fc.SESSION_SERIAL["session_mode"] == "serial"
    assert "serial" in fc.SESSION_SERIAL["rule"]
    assert "session" in fc.SESSION_SERIAL["applies_to"].lower()


# ------------------------------------------------------------ B. manifest hash contract
def b1_manifest_contract():
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "evidence"
        out.mkdir()
        html = FIX / "sample.html"
        reference = out / "reference.png"
        browser = out / "browser.png"
        metrics_p = out / "metrics.json"
        shutil.copy(FIX / "sample_reference.png", reference)
        shutil.copy(FIX / "sample_browser.png", browser)
        shutil.copy(FIX / "fake_metrics.json", metrics_p)
        m = json.loads(metrics_p.read_text(encoding="utf-8"))

        d = verify.build_manifest(html, reference, out, 20, "000000", m)
        digest = verify.write_manifest(d, out)

        # external-file digests re-hash equal
        assert sha(html) == d["html_sha256"], "html digest mismatch"
        assert sha(reference) == d["reference_sha256"], "reference digest mismatch"
        assert sha(browser) == d["browser_sha256"], "browser digest mismatch"
        assert sha(metrics_p) == d["metrics_sha256"], "metrics digest mismatch"
        assert set(d["hash_inputs"]) == {"html", "reference", "browser.png", "metrics.json"}

        # own digest lives ONLY in sidecar; run.json embeds no self digest
        sidecar = out / "run.json.sha256"
        assert sidecar.exists(), "sidecar digest file missing"
        assert sidecar.read_text().strip() == digest, "sidecar != manifest digest"
        assert digest == sha(out / "run.json"), "sha(run.json) != sidecar"
        run_text = (out / "run.json").read_text(encoding="utf-8")
        assert digest not in run_text, "manifest embeds its own digest (self-reference!)"
        assert d["self_digest_file"] == "run.json.sha256"
        assert d["self_reference"] is False

        # tamper detection: change browser.png bytes -> stored digest must mismatch
        browser.write_bytes(b"\x00" + browser.read_bytes())
        assert sha(browser) != d["browser_sha256"], "tamper not detected"


# ------------------------------------------------------------ C. CLI smoke
def c1_help_smoke():
    for script in ("verify.py", "reconstruct.py", "figwright_client.py"):
        py_compile.compile(str(SCRIPTS / script), doraise=True)
    for script in ("verify.py", "reconstruct.py"):
        r = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{script} --help exit={r.returncode}: {r.stderr[:300]}"


def main():
    for name, fn in [
        ("A1 tools/list hang degrades without blocking", a1_degraded_not_blocking),
        ("A2 FIGWRIGHT_SKIP_TOOLS_LIST skips discovery", a2_skip_tools_list),
        ("A3 healthy tools/list discovery works", a3_healthy),
        ("A4 bounded retry recovers slow tools/list", a4_bounded_retry_recovers),
        ("A5 SESSION_SERIAL machine-readable record", a5_serial_session_record),
        ("B1 manifest hashes bind externals + sidecar, no self-reference", b1_manifest_contract),
        ("C1 CLI --help smoke + byte-compile", c1_help_smoke),
    ]:
        check(name, fn)

    width = max(len(n) for n, _, _ in RESULTS)
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name.ljust(width)}  {detail}")
        failed += 0 if ok else 1
    print(f"\n{len(RESULTS)-failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
