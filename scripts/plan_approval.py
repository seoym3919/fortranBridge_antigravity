#!/usr/bin/env python3
"""UserPromptSubmit hook: a human approves PLAN.md by typing APPROVE PLAN <code>.

The code is the start of PLAN.md's SHA-256, so an approval covers one exact text and a later edit voids it. This runs on what the
user actually submitted, which the model cannot produce: that is what makes it a human's approval. It records the approval in
.verification/plan_approval.json and in the evidence log.

Limit: those are plain files. This makes approval a human step in normal use; it is not a defence against a process that writes
the file directly. Never write that file by hand.

Hook input (stdin JSON): prompt, cwd. Prints a note for the conversation. Always exits 0 (a hook error must not wedge a session).
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verification_evidence as ve  # noqa: E402

COMMAND = re.compile(r"^\s*APPROVE\s+PLAN\s+([0-9a-f]{8})\s*$", re.IGNORECASE | re.MULTILINE)


def approve(prompt: str, cwd: Path) -> Optional[str]:
    """The note for the conversation, or None if the message is not an approval."""
    match = COMMAND.search(prompt)
    if not match:
        return None
    plan = cwd / "PLAN.md"
    digest = ve.sha256_file(plan)
    if digest is None:
        return "fortranBridge: there is no PLAN.md here, so nothing was approved."
    if not digest.startswith(match.group(1).lower()):
        return f"fortranBridge: that code is not for the current PLAN.md, so nothing was approved. Its code is {digest[:8]}."
    folder = cwd / ".verification"
    folder.mkdir(exist_ok=True)
    (folder / "plan_approval.json").write_text(json.dumps({"sha256": digest, "at": datetime.now(timezone.utc).isoformat()}) + "\n", encoding="utf-8")
    ve.record_run(ve.log_path(str(folder)), tool="plan_approval", plugin="fortranbridge", kind="approval", name="plan_approval",
                  key="plan_approval", passed=True, tolerance={}, subjects=[ve.subject("PLAN.md", cwd)])
    return f"fortranBridge: PLAN.md ({digest[:8]}) is approved by the user. Conversion may begin."


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        is_antigravity = "lastUserInput" in payload or "workspacePaths" in payload
        prompt = str(payload.get("prompt") or payload.get("lastUserInput") or "")
        workspaces = payload.get("workspacePaths")
        cwd_raw = payload.get("cwd") or (workspaces[0] if isinstance(workspaces, list) and workspaces else ".")
        note = approve(prompt, Path(cwd_raw).resolve())
        if note:
            if is_antigravity:
                print(json.dumps({"injectSteps": [{"ephemeralMessage": note}]}))
            else:
                print(note)
        elif is_antigravity:
            print("{}")
    except Exception as exc:  # a broken hook must never wedge the session
        print(f"plan_approval: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
