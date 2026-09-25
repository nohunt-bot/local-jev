#!/usr/bin/env python3
"""Deterministic stand-in for an OpenAI-compatible /v1/chat/completions
endpoint with logprobs, used only so contract_test.py can exercise
systemone_local.py's readout logic end to end. No model weights are
downloaded or run here; this is not a benchmark of any real model's
calibration, only a fixture for the plumbing and answer-shape contract
between systemone_local.py and jev-mcp.

Env:
  FAKE_ENGINE_HOST  bind host, default 127.0.0.1
  FAKE_ENGINE_PORT  bind port, default 8080
  FAKE_MODE         "confident" (default) or "garbage" -- see build_top_logprobs

Only POST /v1/chat/completions is served; everything else is 404.
"""
import json
import math
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("FAKE_ENGINE_HOST", "127.0.0.1")
PORT = int(os.environ.get("FAKE_ENGINE_PORT", "8080"))
MODE = os.environ.get("FAKE_MODE", "confident")

OPTION_RE = re.compile(r"(?m)^([A-Z])\) ")


def option_letters(prompt):
    """Letters appearing as lettered options ("A) ...") in the prompt, in order."""
    seen = []
    for m in OPTION_RE.finditer(prompt):
        letter = m.group(1)
        if letter not in seen:
            seen.append(letter)
    return seen


def confident_tokens(letters):
    # 0.90 total mass on option "A", split across two token spellings ("A" and
    # " A") so systemone_local's merge-by-stripped-token logic has something
    # to merge; the remaining 0.10 splits evenly over the other option
    # letters actually present in the prompt.
    tokens = [("A", 0.6), (" A", 0.3)]
    others = [l for l in letters if l != "A"]
    if others:
        share = 0.10 / len(others)
        tokens += [(l, share) for l in others]
    return tokens


def garbage_tokens(letters):
    # An uncalibrated/off-task model: mass sits on ordinary chat filler
    # tokens, never meaningfully on a real option letter. The small leftover
    # spread over the letters (well under MIN_LABEL_MASS's default of 0.5)
    # still exercises the merge/threshold path rather than trivially finding
    # no letters at all.
    tokens = [("The", 0.5), ("I", 0.3), ("Sure", 0.15)]
    if letters:
        share = 0.05 / len(letters)
        tokens += [(l, share) for l in letters]
    return tokens


def build_top_logprobs(prompt, requested_top_logprobs):
    letters = option_letters(prompt)
    pairs = confident_tokens(letters) if MODE == "confident" else garbage_tokens(letters)
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    # Real engines cap how many alternatives they report; mirror that so a
    # low TOP_LOGPROBS on the caller's side is exercised faithfully.
    cap = requested_top_logprobs if isinstance(requested_top_logprobs, int) and requested_top_logprobs > 0 else 20
    pairs = pairs[:cap]
    return [{"token": tok, "logprob": math.log(p), "bytes": None} for tok, p in pairs]


def last_user_content(messages):
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-engine/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(404, {"error": "not found"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            prompt = last_user_content(body.get("messages"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            self._send(400, {"error": f"malformed request: {exc}"})
            return
        top_logprobs = build_top_logprobs(prompt, body.get("top_logprobs"))
        top = top_logprobs[0] if top_logprobs else {"token": "", "logprob": 0.0}
        response = {
            "id": "fake-engine-0",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": top["token"].strip()},
                    "logprobs": {
                        "content": [
                            {
                                "token": top["token"],
                                "logprob": top["logprob"],
                                "bytes": None,
                                "top_logprobs": top_logprobs,
                            }
                        ]
                    },
                    "finish_reason": "length",
                }
            ],
            "usage": {
                "prompt_tokens": len(prompt) // 4,
                "completion_tokens": 1,
                "total_tokens": len(prompt) // 4 + 1,
            },
        }
        self._send(200, response)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[fake_engine] mode={MODE} http://{HOST}:{PORT}/v1/chat/completions", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
