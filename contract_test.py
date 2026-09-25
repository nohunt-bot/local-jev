#!/usr/bin/env python3
"""Contract test for the local System-One stand-in (systemone_local.py +
fake_engine.py) against the pinned, unmodified `@jkudish/jev-mcp@0.8.0`
package run over MCP stdio via `npx`. Proves that jev-mcp's `compatible`
provider (JEV_PROVIDER=compatible) needs no code changes to work against a
local HTTP endpoint, and that jev-mcp's own client-side validators fail
closed on a low-mass/garbage engine — it does not evaluate judgment quality:
fake_engine.py is a scripted fixture, not a real model (no open-weight
model is downloaded or run anywhere in this test).

Three modes (see parse_args()):
  (default, no flags)    spawn fake_engine.py and systemone_local.py on free
                          localhost ports per mode ("confident", then
                          "garbage"), spawn jev-mcp pointed at systemone_local
                          via JEV_PROVIDER=compatible, do the MCP handshake,
                          list tools (expect exactly the 11 jev-mcp tools),
                          call each tool with a small synthetic input and
                          check pass/fail-closed per tool, plus a self-check
                          that a dead ENGINE_URL is correctly NOT mistaken for
                          jev-mcp validators failing closed. Also runs pure
                          unit checks of the readout math with no subprocesses.
  --endpoint URL [--token T]
                          test an existing System-One-compatible endpoint
                          directly: spawns neither fake_engine.py nor
                          systemone_local.py, just jev-mcp pointed at URL.
  --engine-url URL --engine-model NAME
                          spawn systemone_local.py against a real OpenAI-
                          compatible engine (see systemone_local.py's own
                          ENGINE_URL/ENGINE_MODEL docs for per-engine values),
                          then jev-mcp pointed at that systemone_local.
                          TOP_LOGPROBS, MIN_LABEL_MASS, ENGINE_EXTRA_BODY and
                          ENGINE_TIMEOUT_S pass through from the caller's
                          environment when set (e.g. TOP_LOGPROBS=11 for
                          mlx_lm.server, which rejects anything above 11);
                          unset ones keep systemone_local.py's defaults.
Both external modes run only the unit checks plus one reduced round (tools/
list = 11, every tool is_error == False and not invalid_response) -- the
fake-engine-specific checks (the confident ~0.9 probability spot check, the
garbage round, the dead-engine self-check) do not apply to a real endpoint.

Exit 0 only if every check for the selected mode passes.
"""
import argparse
import importlib.util
import json
import math
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# run_unit_checks() execs fresh, throwaway copies of systemone_local.py under
# synthetic module names; without this they'd leave __pycache__/*.pyc next to
# the source on every run.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent

EXPECTED_TOOLS = {
    "jev_verify", "jev_screen", "jev_noul", "jev_find", "jev_classify",
    "jev_decide", "jev_rerank", "jev_compare", "jev_extract", "jev_review", "jev_gate",
}

# Small, valid, synthetic inputs per tool (no personal data, no real URLs),
# built from each tool's inputSchema in jev-mcp 0.8.0.
TOOL_INPUTS = {
    "jev_verify": {
        "claims": ["The release shipped on a Monday."],
        "evidence": "Changelog entry: 'Shipped v1.2.0 on Monday, Jan 5.'",
    },
    "jev_screen": {
        "text": "Quarterly numbers: revenue up 4% quarter over quarter.",
        "purpose": "summarize the document for a status update",
    },
    "jev_noul": {
        "propositions": ["Water boils at 100 degrees Celsius at sea level."],
    },
    "jev_find": {
        "query": "Which note covers the deployment steps?",
        "candidates": [
            {"id": "note1", "text": "Deployment steps: build, test, ship."},
            {"id": "note2", "text": "Notes about lunch plans."},
        ],
    },
    "jev_classify": {
        "items": [{"id": "item1", "text": "The invoice is overdue by 30 days."}],
        "classes": [
            {"id": "billing", "description": "Billing and payment issues."},
            {"id": "technical", "description": "Technical product issues."},
        ],
    },
    "jev_decide": {
        "decision": "Pick a caching strategy for the API.",
        "evidence": "Redis is already deployed; Memcached would need new infra.",
        "priorities": "Minimize new infrastructure.",
        "candidates": [
            {"id": "redis", "description": "Use the existing Redis deployment."},
            {"id": "memcached", "description": "Deploy a new Memcached cluster."},
        ],
    },
    "jev_rerank": {
        "query": "deployment rollback steps",
        "candidates": [
            {"id": "doc1", "text": "How to roll back a deployment."},
            {"id": "doc2", "text": "How to bake a cake."},
        ],
    },
    "jev_compare": {
        "passage_a": "The release shipped on Monday.",
        "passage_b": "The release shipped on Monday.",
    },
    "jev_extract": {
        "document": "Total: $42.00 due on 2026-01-01.",
        "fields": [
            {"id": "total", "pattern": r"\$[0-9]+(?:\.[0-9]{2})?", "description": "The total amount due."}
        ],
    },
    "jev_review": {
        "request": "Add input validation to the login form.",
        "diff": "+ if not email:\n+     raise ValueError('email required')",
    },
    "jev_gate": {
        "request": "Add input validation to the login form.",
        "diff": "+ if not email:\n+     raise ValueError('email required')",
        "claims": ["Input validation was added to the login form."],
        "evidence": "The diff adds a ValueError raised when the email field is empty.",
    },
}

# Every tool-level input above stays comfortably under the default
# TOP_LOGPROBS=20 letter cap. This one deliberately has 22 classes to go past
# it through real jev-mcp: systemone_local omits a choice with more labels
# than the cap (zero engine calls; the per-label yes/no fallback that used to
# answer it failed open and was removed), so jev_classify must report
# invalid_response in BOTH rounds -- a confident engine does not buy a verdict
# here either. jev_find past 20 candidates and jev_extract with 20 regex
# matches (+ none_of_them = 21 labels) take the same fail-closed path in
# normal use; jev_rerank never does (it asks one noul per candidate).
LARGE_CLASSIFY_INPUT = {
    "items": [{"id": "item1", "text": "The invoice is overdue by 30 days."}],
    "classes": [{"id": f"class{i}", "description": f"Placeholder category {i} for the over-cap check."} for i in range(22)],
}


def _verify_failed(p):
    return any(r.get("status") == "invalid_response" for r in p.get("results", []))


def _screen_failed(p):
    return p.get("status") == "invalid_response"


def _find_failed(p):
    return p.get("status") == "invalid_response"


def _classify_failed(p):
    return any(r.get("status") == "invalid_response" for r in p.get("results", []))


def _classify_all_invalid(p):
    # LARGE_CLASSIFY_INPUT (both rounds): every item must come back
    # invalid_response with no classification -- a verdict for any item means
    # the over-cap path failed open.
    results = p.get("results") or []
    return bool(results) and all(r.get("status") == "invalid_response" and r.get("classification") is None for r in results)


def _decide_failed(p):
    return p.get("recommendation", {}).get("status") == "invalid_response"


def _rerank_failed(p):
    return p.get("status") == "invalid_response"


def _compare_failed(p):
    return p.get("overall", {}).get("status") == "invalid_response"


def _extract_failed_any(p):
    # Used only for confident mode's "nothing at all looks broken" check: our
    # test input's regex always matches, so under a working engine none of
    # these three should ever appear; any one of them is an anomaly worth
    # flagging even though only "invalid_response" is meaningful proof for
    # the garbage-mode direction (see _extract_garbage_ok below).
    return any(r.get("status") in ("invalid_response", "invalid_pattern", "not_found") for r in p.get("results", []))


def _extract_garbage_ok(p):
    # "not_found"/"invalid_pattern" happen with zero engine
    # involvement (no regex match, or a broken pattern) and would let extract
    # "pass" the garbage round vacuously. Our fixed test input's regex does
    # match, so a garbage engine must show specifically invalid_response.
    return any(r.get("status") == "invalid_response" for r in p.get("results", []))


def _gate_any_failed(p):
    # Confident mode: neither half may show a failure signal.
    review_bad = p.get("review", {}).get("status") == "invalid_response"
    verify_bad = p.get("verification", {}).get("summary", {}).get("invalid_response", 0) > 0
    return review_bad or verify_bad


def _gate_all_failed(p):
    # Garbage mode requires BOTH halves to show their own
    # failure signal, not just one (OR let a coincidental partial success
    # count as "failed closed").
    review_bad = p.get("review", {}).get("status") == "invalid_response"
    verify_bad = p.get("verification", {}).get("summary", {}).get("invalid_response", 0) > 0
    return review_bad and verify_bad


# jev_noul/jev_review both use a plain top-level "status" field the same way
# jev_screen/jev_find/jev_rerank do (success: "ok"/absent; failure:
# "invalid_response"), so they share _screen_failed's check. Used for
# confident mode (via negation) and, for the 9 simple tools, garbage mode too
# -- see GARBAGE_FAIL_PREDICATES for the 2 (jev_extract, jev_gate) that need a
# stricter, non-symmetric garbage-mode check.
FAIL_PREDICATES = {
    "jev_verify": _verify_failed,
    "jev_screen": _screen_failed,
    "jev_noul": _screen_failed,
    "jev_find": _find_failed,
    "jev_classify": _classify_failed,
    "jev_decide": _decide_failed,
    "jev_rerank": _rerank_failed,
    "jev_compare": _compare_failed,
    "jev_extract": _extract_failed_any,
    "jev_review": _screen_failed,
    "jev_gate": _gate_any_failed,
}

GARBAGE_FAIL_PREDICATES = {**FAIL_PREDICATES, "jev_extract": _extract_garbage_ok, "jev_gate": _gate_all_failed}

PASSTHROUGH_EXACT = ("PATH", "HOME", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")

# --engine-url mode: let a caller who already exported one of these (e.g.
# TOP_LOGPROBS=11 for mlx_lm.server, ENGINE_TIMEOUT_S for a slow engine)
# reach the real systemone_local.py they asked us to spawn, instead of
# silently overriding it with our own fake-engine-tuned defaults (the default
# round hardcodes MIN_LABEL_MASS=0.5 on purpose, since it's calibrated to
# fake_engine.py specifically).
EXTERNAL_ENGINE_PASSTHROUGH = ("MIN_LABEL_MASS", "TOP_LOGPROBS", "ENGINE_EXTRA_BODY", "ENGINE_TIMEOUT_S")

# npm_config_* keys whose name contains any of these (case-insensitive) are
# credentials, not registry settings: never forwarded to the jev-mcp subprocess.
NPM_CONFIG_SECRET_MARKERS = ("auth", "token", "password")


def build_jev_env(base_url, api_key="local-test"):
    """Minimal env for the jev-mcp subprocess: only what npx needs to fetch
    from the registry, plus the three JEV_* vars for the compatible provider.
    Deliberately never forwards TYPESAFE_API_KEY/OPENROUTER_API_KEY/any other
    *_API_KEY or token from our own environment -- including an npm_config_*
    one: npm's own private-registry credentials (e.g. an "_authToken" or a
    "_password") would otherwise pass through the blanket npm_config_*
    allowlist below, so any npm_config_* key whose name contains "auth",
    "token" or "password" (case-insensitive) is dropped too.
    """
    env = {k: os.environ[k] for k in PASSTHROUGH_EXACT if k in os.environ}
    for key, value in os.environ.items():
        if key.startswith("npm_config_") and not any(marker in key.lower() for marker in NPM_CONFIG_SECRET_MARKERS):
            env[key] = value
    env["JEV_PROVIDER"] = "compatible"
    env["JEV_API_BASE_URL"] = base_url
    env["JEV_API_KEY"] = api_key
    return env


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host, port, timeout=15.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.1)
    raise TimeoutError(f"nothing listening on {host}:{port} after {timeout}s ({last_err})")


def spawn(cmd, env, **kwargs):
    # start_new_session=True (setsid) puts the process in its own process
    # group so cleanup can signal the whole group, not just the immediate
    # child: npx can hand off to a node grandchild with inherited stdio and
    # then exit itself, which would otherwise orphan a live jev-mcp process
    # still holding the pipes open (observed in manual testing).
    return subprocess.Popen(cmd, env=env, start_new_session=True, **kwargs)


def terminate(proc, name):
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
    print(f"[cleanup] could not confirm {name} (pid {proc.pid}) exited", file=sys.stderr)


def load_module(name, path, env_overrides=None):
    """Exec a fresh copy of a script as a module (server startup is guarded
    by __main__, so this never binds a port); env_overrides are applied only
    for the duration of the exec so module-level `os.environ.get(...)` reads
    see them.
    """
    saved = {}
    try:
        for key, value in (env_overrides or {}).items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_unit_checks():
    print("--- unit checks (readout math, no subprocesses) ---")
    s1 = load_module("systemone_local_unit", HERE / "systemone_local.py")
    checks = []

    merged = s1.merge_tokens([
        {"token": "A", "logprob": math.log(0.6)},
        {"token": " A", "logprob": math.log(0.3)},
        {"token": "B", "logprob": math.log(0.1)},
    ])
    checks.append(("merge 'A' + ' A' -> 0.9", abs(merged["A"] - 0.9) < 1e-9))

    # Raw-tokenizer engines emit space markers instead of a
    # literal leading space -- mlx_lm.server's convert_ids_to_tokens gives
    # "ĠA" (U+0120), SentencePiece models give "▁A" (U+2581) -- both must
    # merge with a plain " A"/"A" too.
    merged_markers = s1.merge_tokens([
        {"token": "ĠA", "logprob": math.log(0.5)},
        {"token": "▁A", "logprob": math.log(0.3)},
        {"token": " A", "logprob": math.log(0.1)},
    ])
    checks.append(("merge tokenizer space markers (Ġ/▁) with plain space", abs(merged_markers["A"] - 0.9) < 1e-9))

    normalized = s1.normalize({"A": 0.6, "B": 0.3})
    checks.append(("renormalize sums to 1", abs(sum(normalized.values()) - 1.0) < 1e-9))
    checks.append(("renormalize keeps ratio", abs(normalized["A"] - (0.6 / 0.9)) < 1e-9))

    checks.append(("confidence uniform -> 0", abs(s1.entropy_confidence([0.5, 0.5]) - 0.0) < 1e-9))
    checks.append(("confidence one-hot -> 1", abs(s1.entropy_confidence([1.0, 0.0]) - 1.0) < 1e-9))

    expected = s1.expected_score({"0": 0.2, "1": 0.3, "2": 0.5})
    checks.append(("score expected value 1.3", abs(expected - 1.3) < 1e-9))

    # The legend must carry the raw criteria values
    # (not describe()'s stringified prompt text) per typesafe-sdk's ScoreResponse.
    s1_legend = load_module("systemone_local_legend", HERE / "systemone_local.py")
    s1_legend.call_engine = lambda prompt: ([{"token": "A", "logprob": 0.0}], 5, 1)
    levels = ["Clearly wrong", {"detail": "structured desc"}, "Looks correct"]
    score_answer = s1_legend.answer_score(None, {"type": "score", "instructions": "how correct", "criteria": levels}, {"input_tokens": 0, "output_tokens": 0})
    expected_legend = {"0": "Clearly wrong", "1": {"detail": "structured desc"}, "2": "Looks correct"}
    checks.append(("score legend carries raw criteria values", score_answer is not None and score_answer.get("legend") == expected_legend))

    # (b): a score (or noul) question with more options than MAX_DIRECT_LABELS
    # must be omitted, not read from a truncated set of letters. Assert both
    # the None result AND that no (truncated) engine call was even attempted.
    s1_cap2 = load_module("systemone_local_cap2", HERE / "systemone_local.py", {"TOP_LOGPROBS": "2"})

    def call_engine_must_not_be_called(prompt):
        raise AssertionError("call_engine must not run for a question that exceeds MAX_DIRECT_LABELS")

    s1_cap2.call_engine = call_engine_must_not_be_called
    try:
        score_over_cap = s1_cap2.answer_score(None, {"type": "score", "instructions": "q", "criteria": ["a", "b", "c"]}, {"input_tokens": 0, "output_tokens": 0})
        score_over_cap_ok = score_over_cap is None
    except AssertionError:
        score_over_cap_ok = False
    checks.append(("TOP_LOGPROBS=2 + 3-level score -> omitted, fail closed", score_over_cap_ok))

    # A choice past the cap follows the same rule. The per-label yes/no
    # fallback that used to answer it normalised independent P(yes) values,
    # which can fail open, so it was removed. With TOP_LOGPROBS=2 and 6
    # labels the answer must be omitted without a single engine call.
    try:
        choice_over_cap = s1_cap2.answer_choice(None, {"type": "choice", "instructions": "q", "criteria": {f"opt{i}": f"option {i}" for i in range(6)}}, {"input_tokens": 0, "output_tokens": 0})
        choice_over_cap_ok = choice_over_cap is None
    except AssertionError:
        choice_over_cap_ok = False
    checks.append(("TOP_LOGPROBS=2 + 6-label choice -> omitted, zero engine calls", choice_over_cap_ok))

    # TOP_LOGPROBS=2 leaves exactly enough labels for noul's 2 options, so this
    # is a normal direct read (a fresh working stub, not the "must not be
    # called" one above); the boundary case (TOP_LOGPROBS<2, where even noul
    # cannot be answered) is covered by the startup-refusal checks below
    # instead, since a running server can never actually have MAX_DIRECT_LABELS < 2.
    at_cap_calls = []

    def counting_call_engine(prompt):
        at_cap_calls.append(prompt)
        return [{"token": "A", "logprob": 0.0}], 1, 1

    s1_cap2.call_engine = counting_call_engine
    noul_at_cap = s1_cap2.answer_noul(None, {"type": "noul", "instructions": "q", "criteria": {"true": "t", "false": "f"}}, {"input_tokens": 0, "output_tokens": 0})
    checks.append(("noul still answers at the TOP_LOGPROBS=2 boundary", noul_at_cap is not None))

    # A choice with exactly TOP_LOGPROBS labels is still one direct readout
    # (guards an off-by-one in the cap check above).
    at_cap_calls.clear()
    choice_at_cap = s1_cap2.answer_choice(None, {"type": "choice", "instructions": "q", "criteria": {"x": "x", "y": "y"}}, {"input_tokens": 0, "output_tokens": 0})
    checks.append(("TOP_LOGPROBS=2 + 2-label choice -> answered, one direct engine call", choice_at_cap is not None and len(at_cap_calls) == 1))

    # (c): ENGINE_EXTRA_BODY's keys must reach the actual engine request body,
    # and ENGINE_TIMEOUT_S the actual urlopen call. Capture at the network
    # boundary (call_engine is the function under test here), saving/restoring
    # the real urlopen since it's one shared module object across every loaded
    # copy of systemone_local plus this file.
    extra = {"chat_template_kwargs": {"enable_thinking": False}}
    s1_extra = load_module("systemone_local_extra_body", HERE / "systemone_local.py", {"ENGINE_EXTRA_BODY": json.dumps(extra), "ENGINE_TIMEOUT_S": "7.5"})
    captured = {}

    class _FakeResp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return self._payload

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResp({
            "choices": [{"logprobs": {"content": [{"top_logprobs": [{"token": "A", "logprob": 0.0}]}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        s1_extra.call_engine("dummy prompt")
    finally:
        urllib.request.urlopen = original_urlopen
    checks.append(("ENGINE_EXTRA_BODY keys merged into engine request body", captured.get("body", {}).get("chat_template_kwargs") == {"enable_thinking": False}))
    checks.append(("ENGINE_TIMEOUT_S=7.5 reaches the engine call's urlopen timeout", captured.get("timeout") == 7.5))

    # (b)/(c): every startup refusal actually refuses to start (non-zero exit
    # AND main()'s own "refusing to start" message, so an import-time crash
    # cannot pass for a refusal), run as real subprocesses since main()'s
    # sys.exit(1) must not tear down this test process.
    def expect_refusal(env_overrides, label):
        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMONE_PORT": str(free_port()), **env_overrides}
        try:
            proc = subprocess.run([sys.executable, str(HERE / "systemone_local.py")], env=env, capture_output=True, text=True, timeout=10)
            return (label, proc.returncode != 0 and "refusing to start" in proc.stderr)
        except subprocess.TimeoutExpired:
            return (label, False)  # hung instead of refusing -> failure

    checks.append(expect_refusal({"TOP_LOGPROBS": "1"}, "refuses to start: TOP_LOGPROBS=1"))
    checks.append(expect_refusal({"ENGINE_EXTRA_BODY": "{not json"}, "refuses to start: ENGINE_EXTRA_BODY invalid JSON"))
    checks.append(expect_refusal({"ENGINE_EXTRA_BODY": "[1, 2, 3]"}, "refuses to start: ENGINE_EXTRA_BODY not an object"))
    checks.append(expect_refusal({"MIN_LABEL_MASS": "0"}, "refuses to start: MIN_LABEL_MASS=0 (no floor)"))
    checks.append(expect_refusal({"ENGINE_TIMEOUT_S": "0"}, "refuses to start: ENGINE_TIMEOUT_S=0"))
    checks.append(expect_refusal({"ENGINE_TIMEOUT_S": "soon"}, "refuses to start: ENGINE_TIMEOUT_S not a number"))

    ok = True
    for name, passed in checks:
        print(f"[unit] {name}: {'PASS' if passed else 'FAIL'}")
        ok = ok and passed
    return ok


def run_http_smoke_checks(base_url, token):
    results = []

    req = urllib.request.Request(base_url + "/nope", method="GET")
    try:
        urllib.request.urlopen(req, timeout=5)
        results.append(("404 on unknown path", False))
    except urllib.error.HTTPError as e:
        results.append(("404 on unknown path", e.code == 404))

    req = urllib.request.Request(
        base_url + "/v1/systemone", data=b"{not json",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        results.append(("400 on malformed body", False))
    except urllib.error.HTTPError as e:
        results.append(("400 on malformed body", e.code == 400))

    req = urllib.request.Request(
        base_url + "/v1/systemone",
        data=json.dumps({"state": {}, "questions": {"q": {"type": "noul"}}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        results.append(("401 on missing token", False))
    except urllib.error.HTTPError as e:
        results.append(("401 on missing token", e.code == 401))

    return results


class LineReader:
    """Background line reader so stdio reads from the jev-mcp subprocess can
    have a real wall-clock timeout (plain file.readline() cannot)."""

    def __init__(self, stream):
        self._q = queue.Queue()
        self._t = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self._t.start()

    def _run(self, stream):
        try:
            for line in stream:
                self._q.put(line)
        except Exception:
            pass
        finally:
            self._q.put(None)  # EOF sentinel

    def readline(self, timeout):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("timed out waiting for a line from jev-mcp")


class McpClient:
    """Newline-delimited JSON-RPC client over an MCP stdio server's stdin/stdout."""

    def __init__(self, proc):
        self._proc = proc
        self._reader = LineReader(proc.stdout)
        self._next_id = 1

    def _write(self, message):
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def request(self, method, params=None, timeout=60.0):
        msg_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for a response to {method} (id={msg_id})")
            line = self._reader.readline(remaining)
            if line is None:
                raise EOFError(f"jev-mcp closed stdout while waiting for {method}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # ignore stray non-JSON-RPC output on stdout
            if msg.get("id") == msg_id:
                return msg

    def notify(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})


def parse_tool_result(reply):
    """(payload_dict_or_None, is_error, raw_text) from a tools/call reply."""
    if "error" in reply:
        return None, True, json.dumps(reply["error"])
    result = reply.get("result") or {}
    is_error = bool(result.get("isError"))
    text_parts = [c.get("text", "") for c in (result.get("content") or []) if c.get("type") == "text"]
    raw_text = "\n".join(text_parts)
    try:
        payload = json.loads(raw_text) if raw_text else None
    except json.JSONDecodeError:
        payload = None
    return payload, is_error, raw_text


def evaluate(mode, name, payload, is_error):
    """True if `name`'s response is what `mode` should produce. An
    MCP-level transport/protocol error (isError) is never a
    legitimate pass in EITHER mode -- our whole design has systemone_local
    always answer with a normal 200 envelope (some/all answers omitted) so
    that jev-mcp's own client-side validators are what does the failing
    closed; an isError instead means something else broke (a dead engine, a
    malformed response, a real bug), which garbage mode must not accept as
    proof of anything. See GARBAGE_FAIL_PREDICATES for jev_extract/jev_gate's
    stricter, non-symmetric garbage-mode checks.
    """
    if is_error:
        return False
    if payload is None:
        return False  # unparsable content is a hard failure in either mode
    try:
        if mode == "garbage":
            return GARBAGE_FAIL_PREDICATES[name](payload)
        return not FAIL_PREDICATES[name](payload)
    except Exception:
        return False  # couldn't even evaluate the shape -> hard failure


def drain_to_list(stream, sink, cap=500):
    try:
        for line in stream:
            sink.append(line.rstrip("\n"))
            if len(sink) > cap:
                sink.pop(0)
    except Exception:
        pass


def mcp_handshake(jev_proc):
    """initialize + notifications/initialized + tools/list, shared by every
    round (default, external, dead-engine self-check). Returns
    (client, missing_tools); raises RuntimeError on a JSON-RPC error.
    """
    client = McpClient(jev_proc)
    init_reply = client.request(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "local-jev-contract-test", "version": "0.1.0"}},
        timeout=180.0,  # first run may need to fetch the package over npx
    )
    if "error" in init_reply:
        raise RuntimeError(f"initialize failed: {init_reply['error']}")
    client.notify("notifications/initialized")

    tools_reply = client.request("tools/list", {}, timeout=60.0)
    if "error" in tools_reply:
        raise RuntimeError(f"tools/list failed: {tools_reply['error']}")
    tool_names = {t["name"] for t in tools_reply["result"]["tools"]}
    missing = EXPECTED_TOOLS - tool_names
    print(f"[handshake] tools/list -> {len(tool_names)} tools; missing={sorted(missing) or 'none'}")
    return client, missing


def run_round(mode):
    print(f"\n=== round: {mode} ===")
    fake_port = free_port()
    sys1_port = free_port()
    base_env = {"PATH": os.environ.get("PATH", "")}
    fake_env = {**base_env, "FAKE_MODE": mode, "FAKE_ENGINE_HOST": "127.0.0.1", "FAKE_ENGINE_PORT": str(fake_port)}
    sys1_env = {
        **base_env,
        "SYSTEMONE_HOST": "127.0.0.1",
        "SYSTEMONE_PORT": str(sys1_port),
        "SYSTEMONE_TOKEN": "local-test",
        "ENGINE_URL": f"http://127.0.0.1:{fake_port}/v1/chat/completions",
        "ENGINE_MODEL": "local-fake",
        "MIN_LABEL_MASS": "0.5",
    }

    fake_proc = spawn([sys.executable, str(HERE / "fake_engine.py")], fake_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    sys1_proc = spawn([sys.executable, str(HERE / "systemone_local.py")], sys1_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    jev_proc = None
    fake_err, sys1_err, jev_err = [], [], []
    threading.Thread(target=drain_to_list, args=(fake_proc.stderr, fake_err), daemon=True).start()
    threading.Thread(target=drain_to_list, args=(sys1_proc.stderr, sys1_err), daemon=True).start()

    try:
        wait_for_port("127.0.0.1", fake_port)
        wait_for_port("127.0.0.1", sys1_port)

        base_url = f"http://127.0.0.1:{sys1_port}"
        http_oks = []
        if mode == "confident":
            for check_name, ok in run_http_smoke_checks(base_url, "local-test"):
                print(f"[http] {check_name}: {'PASS' if ok else 'FAIL'}")
                http_oks.append(ok)

        jev_env = build_jev_env(f"{base_url}/v1/systemone")
        jev_proc = spawn(
            ["npx", "-y", "@jkudish/jev-mcp@0.8.0"], jev_env, cwd=str(HERE),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        threading.Thread(target=drain_to_list, args=(jev_proc.stderr, jev_err), daemon=True).start()

        client, missing = mcp_handshake(jev_proc)

        tool_oks = {}
        for name in sorted(EXPECTED_TOOLS):
            reply = client.request("tools/call", {"name": name, "arguments": TOOL_INPUTS[name]}, timeout=60.0)
            payload, is_error, raw_text = parse_tool_result(reply)
            ok = evaluate(mode, name, payload, is_error)
            tool_oks[name] = ok
            print(f"[tool] {mode} {name}: {'PASS' if ok else 'FAIL'}" + ("" if ok else f"  <-- {raw_text[:200]}"))

        passed = sum(1 for ok in tool_oks.values() if ok)
        label = "passing" if mode == "confident" else "failing-closed (as expected)"
        print(f"[round:{mode}] tools {passed}/{len(tool_oks)} {label}")

        extra_ok = True
        if mode == "confident":
            classify_payload, _, _ = parse_tool_result(
                client.request("tools/call", {"name": "jev_classify", "arguments": TOOL_INPUTS["jev_classify"]}, timeout=60.0)
            )
            top_p = classify_payload["results"][0]["top_probability"]
            # 1e-6, not +-0.05 -- a broken "A"/" A" merge would
            # give 0.6/0.7 ~= 0.857, which the old +-0.05 tolerance let pass.
            near_90 = abs(top_p - 0.9) < 1e-6
            print(f"[check] confident jev_classify top_probability ~= 0.9: {top_p:.6f} {'PASS' if near_90 else 'FAIL'}")
            extra_ok = near_90 and all(http_oks)

        # Past the letter cap through real jev-mcp, not just the direct-function
        # unit check above: 22 classes > the default TOP_LOGPROBS=20, so the
        # choice is omitted and jev_classify must fail closed with
        # invalid_response in BOTH rounds (never a verdict, even from the
        # confident engine). An MCP-level error is not failing closed either.
        large_reply = client.request("tools/call", {"name": "jev_classify", "arguments": LARGE_CLASSIFY_INPUT}, timeout=60.0)
        large_payload, large_is_error, large_raw = parse_tool_result(large_reply)
        try:
            large_ok = not large_is_error and large_payload is not None and _classify_all_invalid(large_payload)
        except Exception:
            large_ok = False  # couldn't even evaluate the shape -> hard failure
        print(f"[check] {mode} jev_classify (22 classes > TOP_LOGPROBS=20) fails closed with invalid_response: {'PASS' if large_ok else 'FAIL'}" + ("" if large_ok else f"  <-- {large_raw[:200]}"))
        extra_ok = extra_ok and large_ok

        return bool(missing) is False and all(tool_oks.values()) and extra_ok
    finally:
        if fake_err:
            print(f"[fake_engine stderr tail] {fake_err[-5:]}")
        if sys1_err:
            print(f"[systemone_local stderr tail] {sys1_err[-5:]}")
        if jev_err:
            print(f"[jev-mcp stderr tail] {jev_err[-5:]}")
        terminate(jev_proc, "jev-mcp")
        terminate(sys1_proc, "systemone_local")
        terminate(fake_proc, "fake_engine")


def run_reduced_round(client, label="external"):
    """The check both external modes run instead of the fake-engine
    dual round -- tools/list = 11, every tool is_error == False and not
    invalid_response (FAIL_PREDICATES, the confident-direction/"any part
    broken" detector). Stops at the first failing tool so an unreachable real
    endpoint fails fast with a clear message instead of grinding through all
    11.
    """
    for name in sorted(EXPECTED_TOOLS):
        reply = client.request("tools/call", {"name": name, "arguments": TOOL_INPUTS[name]}, timeout=60.0)
        payload, is_error, raw_text = parse_tool_result(reply)
        if is_error:
            print(f"[{label}] {name}: MCP error: {raw_text[:300]}")
            return False
        if payload is None:
            print(f"[{label}] {name}: unparsable response content: {raw_text[:300]}")
            return False
        try:
            broken = FAIL_PREDICATES[name](payload)
        except Exception as exc:
            print(f"[{label}] {name}: could not evaluate response shape: {exc}")
            return False
        if broken:
            print(f"[{label}] {name}: returned invalid_response (or equivalent) against a real endpoint: {raw_text[:300]}")
            return False
        print(f"[{label}] {name}: PASS")
    return True


def run_external_round(args):
    """--endpoint or --engine-url+--engine-model: no fake_engine.py, and
    (for --endpoint) no systemone_local.py either.
    """
    procs = []
    sys1_err = []
    jev_err = []
    try:
        if args.endpoint:
            base_url = args.endpoint
            token = args.token
            print(f"[external] testing existing endpoint directly: {base_url}")
        else:
            sys1_port = free_port()
            sys1_env = {"PATH": os.environ.get("PATH", ""), "SYSTEMONE_HOST": "127.0.0.1", "SYSTEMONE_PORT": str(sys1_port),
                        "SYSTEMONE_TOKEN": "external-test", "ENGINE_URL": args.engine_url, "ENGINE_MODEL": args.engine_model}
            for key in EXTERNAL_ENGINE_PASSTHROUGH:
                if key in os.environ:
                    sys1_env[key] = os.environ[key]
            sys1_proc = spawn([sys.executable, str(HERE / "systemone_local.py")], sys1_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            procs.append(sys1_proc)
            threading.Thread(target=drain_to_list, args=(sys1_proc.stderr, sys1_err), daemon=True).start()
            wait_for_port("127.0.0.1", sys1_port, timeout=15.0)
            base_url = f"http://127.0.0.1:{sys1_port}/v1/systemone"
            token = "external-test"
            print(f"[external] systemone_local -> engine {args.engine_url} (model={args.engine_model})")

        jev_env = build_jev_env(base_url, token)
        jev_proc = spawn(
            ["npx", "-y", "@jkudish/jev-mcp@0.8.0"], jev_env, cwd=str(HERE),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        procs.append(jev_proc)
        threading.Thread(target=drain_to_list, args=(jev_proc.stderr, jev_err), daemon=True).start()

        client, missing = mcp_handshake(jev_proc)
        if missing:
            print(f"[external] tools/list missing: {sorted(missing)}")
            return False
        return run_reduced_round(client)
    finally:
        if sys1_err:
            print(f"[systemone_local stderr tail] {sys1_err[-5:]}")
        if jev_err:
            print(f"[jev-mcp stderr tail] {jev_err[-5:]}")
        for p in reversed(procs):
            terminate(p, "external-round-process")


def run_dead_engine_self_check():
    """Prove the strict garbage-mode evaluation actually
    detects an infrastructure failure -- a dead ENGINE_URL -- rather than
    mistaking jev-mcp's own transport-level MCP error for its client-side
    fail-closed validation (both leave the tool without a verdict, so a
    loose check would count a dead engine as a pass). One tool call is
    enough: the failure mechanism (dead engine -> our 502 -> jev-mcp retries
    -> MCP isError) is identical across all 11.
    """
    print("\n=== self-check: a dead ENGINE_URL must not look like a pass ===")
    sys1_port = free_port()
    base_env = {"PATH": os.environ.get("PATH", "")}
    sys1_env = {
        **base_env, "SYSTEMONE_HOST": "127.0.0.1", "SYSTEMONE_PORT": str(sys1_port),
        "SYSTEMONE_TOKEN": "local-test", "ENGINE_URL": "http://127.0.0.1:1/v1/chat/completions",  # nothing listens on port 1
        "ENGINE_MODEL": "local-fake",
    }
    sys1_proc = spawn([sys.executable, str(HERE / "systemone_local.py")], sys1_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    jev_proc = None
    try:
        wait_for_port("127.0.0.1", sys1_port)
        jev_env = build_jev_env(f"http://127.0.0.1:{sys1_port}/v1/systemone", "local-test")
        jev_proc = spawn(
            ["npx", "-y", "@jkudish/jev-mcp@0.8.0"], jev_env, cwd=str(HERE),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        client, missing = mcp_handshake(jev_proc)
        if missing:
            print(f"[self-check] tools/list missing: {sorted(missing)}")
            return False
        reply = client.request("tools/call", {"name": "jev_verify", "arguments": TOOL_INPUTS["jev_verify"]}, timeout=60.0)
        payload, is_error, raw_text = parse_tool_result(reply)
        looks_like_a_pass = evaluate("garbage", "jev_verify", payload, is_error)
        detected_bad = not looks_like_a_pass
        print(f"[self-check] dead engine -> is_error={is_error}, strict garbage-evaluate reports pass={looks_like_a_pass} (must be False)")
        print(f"[self-check] strict garbage check correctly reports failure for a dead engine: {'PASS' if detected_bad else 'FAIL'}")
        return detected_bad
    finally:
        terminate(jev_proc, "jev-mcp (self-check)")
        terminate(sys1_proc, "systemone_local (self-check)")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", help="Test an existing System-One-compatible endpoint directly (no fake_engine.py or systemone_local.py spawned).")
    parser.add_argument("--token", default="external-test", help="Bearer token for --endpoint (default: a placeholder, since jev-mcp's compatible provider requires a non-empty one either way).")
    parser.add_argument("--engine-url", help="Spawn systemone_local.py against this real OpenAI-compatible chat/completions engine URL.")
    parser.add_argument("--engine-model", help="ENGINE_MODEL for --engine-url (must match the engine's served model name for vLLM/Ollama, and mlx_lm.server's --model value).")
    args = parser.parse_args()
    if bool(args.engine_url) != bool(args.engine_model):
        parser.error("--engine-url and --engine-model must be given together")
    if args.endpoint and (args.engine_url or args.engine_model):
        parser.error("--endpoint is mutually exclusive with --engine-url/--engine-model")
    return args


def main():
    args = parse_args()
    print("[contract_test] pinned server: @jkudish/jev-mcp@0.8.0 via npx, JEV_PROVIDER=compatible")
    all_ok = run_unit_checks()

    if args.endpoint or args.engine_url:
        try:
            round_ok = run_external_round(args)
        except Exception:
            print("[round:external] ERROR", file=sys.stderr)
            traceback.print_exc()
            round_ok = False
        print(f"=== external round: {'PASS' if round_ok else 'FAIL'} ===")
        all_ok = all_ok and round_ok
        print(f"\n=== overall: {'PASS' if all_ok else 'FAIL'} ===")
        sys.exit(0 if all_ok else 1)

    for mode in ("confident", "garbage"):
        try:
            round_ok = run_round(mode)
        except Exception:
            print(f"[round:{mode}] ERROR", file=sys.stderr)
            traceback.print_exc()
            round_ok = False
        print(f"=== round {mode}: {'PASS' if round_ok else 'FAIL'} ===")
        all_ok = all_ok and round_ok

    try:
        dead_engine_ok = run_dead_engine_self_check()
    except Exception:
        print("[self-check] ERROR", file=sys.stderr)
        traceback.print_exc()
        dead_engine_ok = False
    all_ok = all_ok and dead_engine_ok

    print(f"\n=== overall: {'PASS' if all_ok else 'FAIL'} ===")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
