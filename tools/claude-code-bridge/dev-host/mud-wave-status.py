#!/usr/bin/env python3
"""Render the Howl wave-A build as a markdown checklist and publish it to Alice's status site.

Example status generator (modern-mud specific). The pattern any generator follows:
build markdown, pipe it over ssh with the restricted alice_status key; the page name
is the ssh "command" (alice-status-drop on aimax1 validates it and writes <name>.md).
Run it from a systemd user timer (see ../systemd/).

It also pushes each package's commits as `extra/<slug>` (JSON Lines, one commit per
line) so the item's "what happened" modal shows them. slug = the package ID slugified
the way the status site does, which matches the page's bold `**<ID>**` title. A local
hash cache means a package is only re-sent when its commit list changes.
"""
import hashlib, json, os, re, subprocess, time

R = "/home/cronqj/dev/modern-mud"
PKGS = "/home/cronqj/dev/modern-mud-notes/story/build/waveA_packages.json"
CACHE = os.path.expanduser("~/.cache/mud-wave-status/extra-hashes.json")
SSH = ["ssh", "-i", "/home/cronqj/.ssh/alice_status", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
       "-o", "ConnectTimeout=10", "cronqj@10.20.30.177"]
LAYER = {0: "Engine, layer 0", 1: "Engine, layer 1", 2: "Light, chapters 1-4 and room text",
         3: "Chapters 3B, 5A, 6", 4: "Chapters 5B, 7", 5: "Final integration"}


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def slugify(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]


def commit_rows(mine, merge_sha):
    """Commits on the package's branches that aren't on wave-a before its merge, newest first,
    plus the merge commit into howl/wave-a."""
    if not mine:
        return []
    base = f"{merge_sha}^1" if merge_sha else "howl/wave-a"
    fmt = ["--format=%h|%ad|%s", "--date=iso-strict"]
    out = sh("git", "-C", R, "log", "--no-merges", *fmt, *mine, "^" + base)
    if merge_sha:
        out = sh("git", "-C", R, "log", "-1", *fmt, merge_sha) + out
    rows = []
    for line in out.splitlines()[:400]:
        h, at, subj = (line.split("|", 2) + ["", ""])[:3]
        if h:
            rows.append({"type": "commit", "sha": h, "at": at, "subject": subj[:300],
                         **({"merge": True} if h == (merge_sha or "")[:len(h)] else {})})
    return rows


def push_extras(extras):
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}
    changed = False
    for slug, rows in extras.items():
        body = "".join(json.dumps(r) + "\n" for r in rows).encode()
        h = hashlib.sha256(body).hexdigest()
        if not rows or cache.get(slug) == h:
            continue
        r = subprocess.run(SSH + ["extra/" + slug], input=body, capture_output=True, timeout=30)
        if r.returncode == 0:
            cache[slug], changed = h, True
        else:
            print(f"extra/{slug}: push failed: {r.stderr.decode(errors='replace').strip()}")
    if changed:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        json.dump(cache, open(tmp, "w"))
        os.replace(tmp, CACHE)


def main():
    pk = json.load(open(PKGS))["packages"]
    branches = sh("git", "-C", R, "branch", "--format=%(refname:short)|%(committerdate:format:%H:%M)").split()
    bdate = dict(b.split("|") for b in branches if "|" in b)
    merged = set(sh("git", "-C", R, "branch", "--merged", "howl/wave-a", "--format=%(refname:short)").split())
    log = sh("git", "-C", R, "log", "howl/wave-a", "--format=%H|%ad|%s", "--date=format:%H:%M")
    rows, done, extras = [], 0, {}
    for p in pk:
        i = p["id"]
        pat = re.compile(rf"^howl/[A-Z]-{re.escape(i)}(-r(\d+))?$")
        mine = [b for b in bdate if pat.match(b)]
        rnd = max([int(pat.match(b).group(2) or 1) for b in mine] or [0])
        mline = next((l for l in log.splitlines() if re.search(rf"Merge howl/[A-Z]-{re.escape(i)}\b", l.split("|", 2)[-1])), None)
        is_done = bool(mline)  # only an explicit merge commit counts; a fresh branch with no commits is trivially "merged"
        extras[slugify(i)] = commit_rows(mine, mline.split("|")[0] if mline else None)
        title = re.sub(r"^[A-Z0-9-]+ ", "", p.get("title", i)).split(":")[0][:70]
        if is_done:
            done += 1
            note = f"merged {mline.split('|')[1]}" if mline else "merged"
            if rnd > 1:
                note += f", {rnd} review rounds"
            rows.append((p["layer"], f"- [x] **{i}** {title} — *{note}*"))
        elif mine:
            last = max(bdate[b] for b in mine)
            rows.append((p["layer"], f"- [ ] ⏳ **{i}** {title} — *round {rnd} of 4, last commit {last}*"))
        else:
            rows.append((p["layer"], f"- [ ] **{i}** {title} — *not started*"))
    wf = sh("tmux", "capture-pane", "-p", "-t", "claude-first")
    runs = [re.sub(r"\s+", " ", l.strip(" ◯◉")) for l in wf.splitlines() if "howl-wave" in l and "▰" in l]
    runs = [re.sub(r"[▰▱]+", "", r) for r in runs]
    out = [f"# Howl Wave A build", "",
           f"<progress value=\"{done}\" max=\"{len(pk)}\"></progress>", "",
           f"**{done} of {len(pk)} packages merged** · branch `howl/wave-a` on Bazzite", ""]
    if runs:
        out += ["> Workflows running: " + " · ".join(r.split("·")[0].strip() + " (" + "·".join(r.split("·")[1:3]).strip() + ")" for r in runs), ""]
    for L in sorted(set(l for l, _ in rows)):
        out += [f"## {LAYER.get(L, f'Layer {L}')}", ""] + [r for l, r in rows if l == L] + [""]
    out += ["## After the build", "", "- [ ] Agents play chapters 1-7 through the walkthrough; fixes", "- [ ] Open the PR to main",
            "- [ ] Wave B design (chapters 8-12)", "", f"*Generated {time.strftime('%Y-%m-%d %H:%M')} on bazzite, every 3 minutes.*"]
    subprocess.run(SSH + ["wave-a"], input="\n".join(out).encode(), timeout=30)
    push_extras(extras)


main()
