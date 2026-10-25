#!/usr/bin/env python3
"""EMV2 shadow for the wallet bridge (em_bridge.py): inserts emv2_block.py once, just before the `if __name__ == "__main__":`
anchor, so it wraps Handler.api after every other layer (flowmap, account). Apply it last in the bridge build chain.
Without emv2-bridge.json next to the patched file every route stays on its current path (see the block's header).
usage: apply_bridge_emv2.py <em_bridge.py> [--check]"""
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANCHOR = '\n\nif __name__ == "__main__":'
MARK = "# EMV2 shadow (emv2/bridge/apply_bridge_emv2.py"
END = "Handler.api = _emv2_api"


def main():
    target = Path(sys.argv[1])
    s = target.read_text()
    block = (HERE / "emv2_block.py").read_text().rstrip()
    if MARK in s:
        # an earlier version of the block: replace it whole (from its rule line to its last line), else nothing to do
        start = s.rindex("\n# ----", 0, s.index(MARK)) + 1
        end = s.index(END, start) + len(END)
        if s[start:end] == block:
            print("already patched")
            return
        out = s[:start] + block + s[end:]
        compile(out, str(target), "exec")
        print("check ok: earlier EMV2 block replaced, compiles")
        if "--check" not in sys.argv:
            target.write_text(out)
            print(" ", hashlib.sha256(out.encode()).hexdigest()[:16], target.name)
        return
    if s.count(ANCHOR) != 1 or "class Handler(BaseHTTPRequestHandler):" not in s or "    def api(self, path, query, qs):" not in s:
        raise SystemExit("anchor not found exactly once (if __name__ / Handler.api)")
    out = s.replace(ANCHOR, "\n\n" + block + "\n" + ANCHOR)
    compile(out, str(target), "exec")
    print("check ok: 1 anchor, compiles")
    if "--check" in sys.argv:
        return
    target.write_text(out)
    print(" ", hashlib.sha256(out.encode()).hexdigest()[:16], target.name)


if __name__ == "__main__":
    main()
