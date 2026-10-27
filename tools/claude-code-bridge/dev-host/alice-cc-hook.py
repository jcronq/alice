#!/usr/bin/env python3
"""Tell Alice when Claude Code on this host finishes long work.

Modes:
  (default)            Claude Code Stop hook: reads hook JSON on stdin. Surfaces to Alice when the
                       turn ran >= LONG_TURN_S, or the turn was a background-workflow completion.
  --progress REPO BRANCH SESSION_DIR PID
                       Mid-run updates: batched commits on BRANCH (after 8 min quiet) and stalls
                       (no file activity under SESSION_DIR for 30 min). Exits when PID dies.
  --watch TRANSCRIPT PID
                       For sessions started before the hook was installed: polls the transcript and
                       surfaces each workflow completion once the agent has replied. Exits when PID dies.

Transport: ssh with a restricted key to aimax1, whose forced command drops the doc in
Alice's inner/surface/ inbox.
"""
import json, os, re, socket, subprocess, sys, time

LONG_TURN_S = 600
ALICE = os.environ.get("ALICE_SSH", "cronqj@10.20.30.177")
KEY = os.path.expanduser(os.environ.get("ALICE_SURFACE_KEY", "~/.ssh/alice_surface"))
STATE = os.path.expanduser("~/.cache/alice-cc-hook/seen.json")
WF_RE = re.compile(r"<task-notification>.*?<status>(\w+)</status>.*?<summary>(.*?)</summary>", re.S)


def ts(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def is_prompt(row):
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    c = (row.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return False
    return bool(text_of(c).strip())


def last_turn(path):
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    start = None
    for i in range(len(rows) - 1, -1, -1):
        if is_prompt(rows[i]):
            start = i
            break
    if start is None:
        return None
    prompt = rows[start]
    reply = ""
    for r in rows[start + 1:]:
        if r.get("type") == "assistant":
            t = text_of((r.get("message") or {}).get("content"))
            if t.strip():
                reply = t
    return {"prompt": text_of(prompt["message"]["content"]), "start_ts": prompt.get("timestamp"),
            "reply": reply, "n_rows": len(rows)}


def seen(key):
    try:
        s = json.load(open(STATE))
    except (OSError, ValueError):
        s = []
    if key in s:
        return True
    s = (s + [key])[-200:]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"))
    return False


def surface(session_id, transcript, cwd, turn, trigger):
    project = os.path.basename(cwd.rstrip("/")) or "unknown"
    dur = time.time() - ts(turn["start_ts"])
    prompt = turn["prompt"].strip()
    if len(prompt) > 600:
        prompt = prompt[:600] + " [...]"
    reply = turn["reply"].strip() or "(no text reply)"
    if len(reply) > 3000:
        reply = reply[:3000] + " [...]"
    doc = f"""---
priority: info
type: claude-code-turn-done
reply_expected: false
created: {time.strftime('%Y-%m-%d')}
detail: {re.sub(r'[^A-Za-z0-9._-]', '-', project)[:30]}-{session_id[:8]}
host: {socket.gethostname()}
session_id: {session_id}
transcript: {transcript}
---

# Claude Code on {socket.gethostname()} finished: {project} ({dur/60:.0f} min turn)

Trigger: {trigger}

What it was asked:
{prompt}

Its last message:
{reply}

---
For Speaking: Jason asked (2026-10-27) to be told when his Claude Code sessions finish long work. If this is a real milestone (workflow done, PR opened, blocked on him), send him 1-2 plain sentences. Routine chatter: let it pass. More context: ssh cronqj@10.20.30.199, read the transcript above, or tmux capture-pane.
"""
    subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                    "-o", "ConnectTimeout=10", ALICE], input=doc.encode(), timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def classify(turn):
    m = WF_RE.search(turn["prompt"])
    if m and re.match(r'\s*(Dynamic |Background )?workflow "', m.group(2), re.I):
        return f"background workflow {m.group(1)}: {m.group(2).strip()[:200]}"
    dur = time.time() - ts(turn["start_ts"])
    if dur >= LONG_TURN_S:
        return f"long turn ({dur/60:.0f} min)"
    return None


def hook():
    ev = json.load(sys.stdin)
    if ev.get("stop_hook_active"):
        return
    tp = ev.get("transcript_path")
    turn = last_turn(tp) if tp and os.path.exists(tp) else None
    if not turn or not turn["start_ts"]:
        return
    trigger = classify(turn)
    if not trigger or seen(f"{ev.get('session_id')}:{turn['start_ts']}"):
        return
    # Detach so the hook never slows Claude Code down.
    if os.fork() == 0:
        os.setsid()
        try:
            surface(ev.get("session_id", "?"), tp, ev.get("cwd", os.getcwd()), turn, trigger)
        finally:
            os._exit(0)


def watch(transcript, pid):
    session_id = os.path.basename(transcript).removesuffix(".jsonl")
    cwd = os.environ.get("ALICE_WATCH_CWD", os.getcwd())
    baseline = last_turn(transcript)
    if baseline:
        seen(f"{session_id}:{baseline['start_ts']}")
    while os.path.exists(f"/proc/{pid}"):
        time.sleep(60)
        try:
            quiet = time.time() - os.path.getmtime(transcript)
            turn = last_turn(transcript)
        except OSError:
            continue
        if not turn or not turn["start_ts"] or quiet < 120 or not turn["reply"]:
            continue
        trigger = classify(turn)
        if trigger and "workflow" in trigger and not seen(f"{session_id}:{turn['start_ts']}"):
            surface(session_id, transcript, cwd, turn, trigger)


def send_doc(kind, slug, title, body):
    doc = f"""---
priority: info
type: {kind}
reply_expected: false
created: {time.strftime('%Y-%m-%d')}
detail: {slug}
host: {socket.gethostname()}
---

# {title}

{body}

---
For Speaking: Jason asked (2026-10-27) for a status update on his Claude Code builds whenever something meaningful happens; you decide what's meaningful (see memory feedback_cc_meaningful_updates). 1-3 plain sentences, or let it pass.
"""
    subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                    "-o", "ConnectTimeout=10", ALICE], input=doc.encode(), timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout.strip()


def newest_mtime(d):
    m = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                m = max(m, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return m


def progress(repo, branch, session_dir, pid):
    """Mid-run updates: batched integration commits on BRANCH, and stalls (no agent activity 30 min)."""
    last = git(repo, "rev-parse", branch)
    pending, last_commit_at, stalled = [], 0, False
    while os.path.exists(f"/proc/{pid}"):
        time.sleep(120)
        head = git(repo, "rev-parse", branch)
        if head and head != last:
            log = git(repo, "log", "--format=%ad %s", "--date=format:%H:%M", f"{last}..{head}")
            pending += [l for l in log.splitlines() if l][::-1]
            last, last_commit_at = head, time.time()
        if pending and time.time() - last_commit_at > 480:
            body = f"New commits on {branch} ({len(pending)}):\n" + "\n".join(pending[-30:])
            send_doc("claude-code-progress", f"{os.path.basename(repo)}-integration",
                     f"Build progress: {len(pending)} commits landed on {branch}", body)
            pending = []
        idle = time.time() - newest_mtime(session_dir)
        if idle > 1800 and not stalled:
            stalled = True
            send_doc("claude-code-progress", f"{os.path.basename(repo)}-stall",
                     f"Build may be stalled: no agent activity for {idle/60:.0f} min",
                     f"Session dir {session_dir} untouched for {idle/60:.0f} min while the session is alive. "
                     f"Check tmux (claude-first) for a prompt waiting on input or an error.")
        elif idle < 300:
            stalled = False


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--progress":
        progress(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif len(sys.argv) == 4 and sys.argv[1] == "--watch":
        watch(sys.argv[2], int(sys.argv[3]))
    else:
        try:
            hook()
        except Exception:
            pass
