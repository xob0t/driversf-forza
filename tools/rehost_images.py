"""Move research/img links in PR and issue text to GitHub attachment hosting.

PR and issue bodies, comments, reviews and review comments link images in
the repo (`.../blob/<sha>/research/img/<file>?raw=true`). This rewrites each
link to a `https://github.com/user-attachments/assets/...` URL so the images
survive once research/img leaves the repo's history.

GitHub's attachment upload has no public API, so uploads go through
`gh issue edit --attach` / `gh pr edit --attach` on a carrier: the item's own
issue or PR (for a comment, its parent). The carrier's body gets a block of
`![rehost-<key>](./<file>)` references appended, gh rewrites them to asset
URLs and the script reads them back. The final text is then written with
the REST API: the original with only the URLs swapped (a borrowed parent
body gets its own text back). Texts written by someone other than the repo
owner, or on an issue someone else opened, are listed and left alone.
Plain-text paths (`research/img/x.png` in backticks) are not links and stay.

    uv run python tools/rehost_images.py collect      # all texts -> out/rehost/items.json
    uv run python tools/rehost_images.py plan -v      # links, fetch images, report
    uv run python tools/rehost_images.py run [--only pr-12,issue-3,comment-<id>] [--limit N]
    uv run python tools/rehost_images.py verify       # re-read -> items-after.json, check

State lives in out/rehost/: items.json (the texts before any edit), backup/
(every original text, written before the first edit and never
overwritten), images/ (the files, named by git blob sha), manifest.json (old
URL -> new URL, image key -> asset URL, texts done), log.txt. A rerun skips
what is already rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OWNER, NAME = "xob0t", "driversf-forza"
SLUG = f"{OWNER}/{NAME}"
OUT = REPO / "out" / "rehost"
BACKUP = OUT / "backup"
IMAGES = OUT / "images"
ITEMS = OUT / "items.json"  # the texts before any edit
AFTER = OUT / "items-after.json"  # re-read by verify
MANIFEST = OUT / "manifest.json"
LOG = OUT / "log.txt"
PAUSE = 3.0  # seconds between edits
MARK = "<!-- rehost-upload -->"
ASSET_RE = re.compile(r"https://github\.com/user-attachments/assets/[0-9a-fA-F-]+")

# A link to an image under research/img. The texts only use absolute blob
# URLs (`?raw=true`, a full or short sha); a plain-text path such as
# `research/img/x.png` in backticks is a mention, not a link, and stays.
LINK_RE = re.compile(
    r"https://github\.com/" + re.escape(SLUG) + r"/blob/(?P<ref>[0-9a-f]{7,40})/"
    r"(?P<path>research/img/[^\s)\"'<>?#]+)(?:\?raw=true)?"
)


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: list[str], *, input: str | None = None, check: bool = True) -> str:
    p = subprocess.run(cmd, check=False, input=input, capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}): {p.stderr.strip()}")
    return p.stdout


def gh_api(path: str, method: str = "GET", fields: dict | None = None) -> dict:
    cmd = ["gh", "api", "-X", method, path]
    payload = None
    if fields is not None:
        cmd += ["--input", "-"]
        payload = json.dumps(fields)
    return json.loads(run(cmd, input=payload) or "null")


def graphql(query: str, **variables) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    out = json.loads(run(["gh", "api", "graphql", "--input", "-"], input=payload))
    if out.get("errors"):
        raise RuntimeError(out["errors"])
    return out["data"]


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --- collect -----------------------------------------------------------------

AUTHOR = "author { login }"
PAGE = "pageInfo { hasNextPage endCursor }"
COMMENTS = f"comments(first: 100) {{ totalCount nodes {{ id databaseId {AUTHOR} body url }} }}"
PR_QUERY = f"""
query($cursor: String) {{ repository(owner: "{OWNER}", name: "{NAME}") {{
  pullRequests(first: 25, after: $cursor) {{ {PAGE} nodes {{
    id number {AUTHOR} body url {COMMENTS}
    reviews(first: 100) {{ totalCount nodes {{ id databaseId {AUTHOR} body url
      comments(first: 100) {{ totalCount nodes {{ id databaseId {AUTHOR} body url }} }} }} }}
  }} }} }} }}"""
ISSUE_QUERY = f"""
query($cursor: String) {{ repository(owner: "{OWNER}", name: "{NAME}") {{
  issues(first: 50, after: $cursor) {{ {PAGE} nodes {{
    id number {AUTHOR} body url {COMMENTS}
  }} }} }} }}"""


def login(node) -> str:
    return (node.get("author") or {}).get("login") or "ghost"


def collect(_args, dest: Path = ITEMS) -> None:
    items = []
    for kind, query, key in (("pr", PR_QUERY, "pullRequests"), ("issue", ISSUE_QUERY, "issues")):
        cursor = None
        while True:
            conn = graphql(query, cursor=cursor)["repository"][key]
            for n in conn["nodes"]:
                owner = login(n)
                base = {"kind": kind, "number": n["number"], "owner": owner}
                items.append(
                    base
                    | {"sub": "body", "id": f"{kind}-{n['number']}", "node": n["id"]}
                    | {"author": owner, "body": n["body"], "url": n["url"]}
                )
                if n["comments"]["totalCount"] > 100:
                    sys.exit(f"{kind} {n['number']}: more than 100 comments, page them")
                for c in n["comments"]["nodes"]:
                    items.append(
                        base
                        | {"sub": "comment", "id": f"comment-{c['databaseId']}"}
                        | {"db": c["databaseId"], "author": login(c), "body": c["body"]}
                        | {"url": c["url"]}
                    )
                for r in (n.get("reviews") or {}).get("nodes", []):
                    if r["body"]:
                        items.append(
                            base
                            | {"sub": "review", "id": f"review-{r['databaseId']}"}
                            | {"db": r["databaseId"], "author": login(r), "body": r["body"]}
                            | {"url": r["url"]}
                        )
                    for c in r["comments"]["nodes"]:
                        items.append(
                            base
                            | {"sub": "review_comment", "id": f"rcomment-{c['databaseId']}"}
                            | {"db": c["databaseId"], "author": login(c), "body": c["body"]}
                            | {"url": c["url"]}
                        )
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
    if dest == ITEMS and ITEMS.exists():
        sys.exit(f"{ITEMS} exists: it is the snapshot before the edits; move it away first")
    save_json(dest, items)
    hits = [i for i in items if "research/img" in i["body"]]
    print(f"{len(items)} texts, {len(hits)} mention research/img -> {dest}")


# --- images ------------------------------------------------------------------


def blob_key(data: bytes) -> str:
    """Git's blob sha of the bytes: the same picture at two commits is one key."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fetch_image(ref: str, path: str) -> tuple[str, Path]:
    """The image at `ref` from local git (else the raw URL), stored as images/<key>/<name>."""
    p = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:{path}"], check=False, capture_output=True)
    if p.returncode == 0:
        data = p.stdout
    else:
        url = f"https://raw.githubusercontent.com/{SLUG}/{ref}/{path}"
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
    key = blob_key(data)
    dest = IMAGES / key / Path(path).name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return key, dest


def links(body: str) -> list[re.Match]:
    return list(LINK_RE.finditer(body))


def backup_path(item: dict) -> Path:
    name = item["id"] if item["sub"] == "body" else f"{item['kind']}-{item['number']}-{item['id']}"
    return BACKUP / f"{name}.md"


def backup(item: dict) -> None:
    """Write the original text once; a rerun never overwrites it."""
    path = backup_path(item)
    if not path.exists():
        BACKUP.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item["body"].encode("utf-8"))


def original(item: dict) -> str:
    return backup_path(item).read_bytes().decode("utf-8")


def plan(args) -> list[dict]:
    """Items with links, their images fetched and keyed; prints a summary."""
    manifest = load_json(MANIFEST, {"urls": {}, "assets": {}})
    todo, mentions = [], []
    for item in load_json(ITEMS, []):
        if "research/img" not in item["body"]:
            continue
        found = links(item["body"])
        rest = LINK_RE.sub("", item["body"])
        if "research/img" in rest:
            mentions.append((item["id"], rest.count("research/img")))
        if found:
            todo.append(item)
    keys = {}
    for item in todo:
        for m in links(item["body"]):
            if m.group(0) not in keys:
                keys[m.group(0)] = fetch_image(m["ref"], m["path"])[0]
    blobs = set(keys.values())
    done = sum(1 for k in blobs if k in manifest["assets"])
    by = {}
    for item in todo:
        by.setdefault((item["kind"], item["sub"], item["author"] == item["owner"] == OWNER), []).append(item)
    for (kind, sub, ours), group in sorted(by.items()):
        n = sum(len(links(i["body"])) for i in group)
        print(f"{kind:5} {sub:14} ours={ours!s:5} {len(group):4} texts {n:4} links")
    print(f"{len(keys)} distinct link URLs, {len(blobs)} distinct images, {done} already uploaded")
    print(f"plain-text mentions left as they are: {len(mentions)} texts, {sum(n for _, n in mentions)} mentions")
    if getattr(args, "verbose", False):
        for ident, n in mentions:
            print(f"  {ident}: {n}")
    args.keys = keys
    return todo


# --- upload and rewrite -------------------------------------------------------


def text_endpoint(item: dict) -> str:
    sub = item["sub"]
    if sub == "body":
        return f"repos/{SLUG}/issues/{item['number']}"
    if sub == "comment":
        return f"repos/{SLUG}/issues/comments/{item['db']}"
    if sub == "review_comment":
        return f"repos/{SLUG}/pulls/comments/{item['db']}"
    return f"repos/{SLUG}/pulls/{item['number']}/reviews/{item['db']}"


def get_text(item: dict) -> str:
    return gh_api(text_endpoint(item))["body"] or ""


def put_text(item: dict, body: str) -> None:
    method = "PUT" if item["sub"] == "review" else "PATCH"
    got = gh_api(text_endpoint(item), method, {"body": body})["body"] or ""
    if got != body:
        raise RuntimeError(f"{item['id']}: GitHub stored a different text than sent")
    time.sleep(PAUSE)


def carrier_of(item: dict) -> dict:
    """The issue or PR body the uploads go through: the item itself, or its parent."""
    return {"kind": item["kind"], "number": item["number"], "sub": "body", "id": f"{item['kind']}-{item['number']}"}


def strip_upload_block(body: str) -> str:
    """Remove the block of upload references a crashed run left at the end."""
    i = body.find(MARK)
    return body if i < 0 else body[:i].removesuffix("\n\n")


def upload(carrier: dict, files: dict[str, Path], carrier_body: str) -> dict[str, str]:
    """Upload `files` (key -> path) through an edit of the carrier's body.

    Leaves the carrier holding `carrier_body` plus the uploaded references;
    the caller writes the carrier's final text next.
    """
    lines = [f"![rehost-{k}](./{p.relative_to(IMAGES).as_posix()})" for k, p in files.items()]
    text = carrier_body + "\n\n" + MARK + "\n" + "\n".join(lines) + "\n"
    body_file = IMAGES / f"_carrier-{carrier['id']}.md"
    body_file.write_bytes(text.encode("utf-8"))
    cmd = ["gh", carrier["kind"], "edit", str(carrier["number"]), "-R", SLUG, "--body-file", body_file.name]
    for p in files.values():
        cmd += ["--attach", f"./{p.relative_to(IMAGES).as_posix()}"]
    p = subprocess.run(cmd, check=False, cwd=IMAGES, capture_output=True, text=True, encoding="utf-8")
    body_file.unlink()
    time.sleep(PAUSE)
    stored = get_text(carrier)
    got = dict(re.findall(r"!\[rehost-([0-9a-f]{40})\]\((" + ASSET_RE.pattern + r")\)", stored))
    if p.returncode != 0 or set(got) != set(files):
        raise RuntimeError(
            f"upload via {carrier['id']}: gh exit {p.returncode} {p.stderr.strip()!r}; "
            f"got {len(got)}/{len(files)} assets; carrier body left with the upload block"
        )
    return got


def rewrite(item: dict, manifest: dict, keys: dict[str, str]) -> str:
    body = original(item)

    def swap(m: re.Match) -> str:
        url = manifest["assets"][keys[m.group(0)]]
        manifest["urls"][m.group(0)] = url
        return url

    return LINK_RE.sub(swap, body)


def run_all(args) -> None:
    todo = plan(args)
    keys = args.keys
    if args.only:
        wanted = set(args.only.split(","))
        todo = [i for i in todo if i["id"] in wanted]
    skipped = [i for i in todo if not (i["author"] == i["owner"] == OWNER)]
    for i in skipped:
        log(f"skip {i['id']} ({i['kind']} {i['number']}): written by {i['author']}, {i['kind']} opened by {i['owner']}")
    todo = [i for i in todo if i not in skipped]
    # Bodies first: a comment's uploads then mostly reuse its parent's.
    todo.sort(key=lambda i: (i["sub"] != "body", i["kind"], i["number"]))
    manifest = load_json(MANIFEST, {"urls": {}, "assets": {}, "items": {}})
    manifest.setdefault("items", {})
    edited = 0
    for item in todo:
        if args.limit and edited >= args.limit:
            break
        backup(item)
        orig = original(item)
        need = {keys[m.group(0)] for m in links(orig)}
        current = get_text(item)
        if need <= manifest["assets"].keys() and current == rewrite(item, manifest, keys):
            log(f"ok   {item['id']}: already rewritten")
            continue
        if item["sub"] == "body":
            current = strip_upload_block(current)
        if current != orig:
            log(f"STOP {item['id']}: text changed since the backup, not touching it")
            continue
        carrier = carrier_of(item)
        files = {}
        for m in links(orig):
            k, path = fetch_image(m["ref"], m["path"])
            if k not in manifest["assets"]:
                files[k] = path
        if files:
            if item["sub"] == "body":
                carrier_body = orig
            else:
                # The parent's body is only borrowed: back it up, put it back.
                parent = next(i for i in load_json(ITEMS, []) if i["id"] == carrier["id"])
                backup(parent)
                carrier_body = strip_upload_block(get_text(carrier))
            keys_left = list(files)
            while keys_left:
                chunk = {k: files[k] for k in keys_left[:50]}
                keys_left = keys_left[50:]
                got = upload(carrier, chunk, carrier_body)
                manifest["assets"].update(got)
                save_json(MANIFEST, manifest)
                log(f"up   {item['id']}: {len(got)} images via {carrier['id']}")
            if item["sub"] != "body":
                put_text(carrier, carrier_body)
                log(f"back {carrier['id']}: body restored")
        put_text(item, rewrite(item, manifest, keys))
        manifest["items"][item["id"]] = {"url": item["url"], "links": len(links(orig))}
        save_json(MANIFEST, manifest)
        edited += 1
        log(f"done {item['id']}: {len(links(orig))} links rewritten ({item['url']})")
    log(f"run finished: {edited} texts edited")


# --- verify ------------------------------------------------------------------


def fetch_url(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "rehost-verify"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read()


def verify(args) -> None:
    """Re-read every text from GitHub and check the rewrite.

    No research/img link is left, no upload block is left on a carrier, every
    edited text is its backup with only the link URLs swapped, every other
    text is as collected, and every asset serves the bytes of its file.
    """
    if not args.no_collect:
        collect(args, AFTER)
    manifest = load_json(MANIFEST, {"urls": {}, "assets": {}, "items": {}})
    before = {i["id"]: i for i in load_json(ITEMS, [])}
    after = load_json(AFTER, [])
    problems = 0
    for item in after:
        ident = item["id"]
        if links(item["body"]):
            print(f"LINK LEFT {ident} ({item['author']}): {len(links(item['body']))} {item['url']}")
            problems += 1
        if MARK in item["body"]:
            print(f"UPLOAD BLOCK LEFT {ident} {item['url']}")
            problems += 1
        if ident in manifest["items"]:
            want = LINK_RE.sub(lambda m: manifest["urls"][m.group(0)], original(item))
            if item["body"] != want:
                print(f"CHANGED {ident}: differs from its backup beyond the URLs")
                problems += 1
        elif ident in before and item["body"] != before[ident]["body"]:
            print(f"note {ident}: changed since collect, not by this tool ({item['url']})")
        rest = item["body"]
        if "research/img" in rest:
            print(f"mention {ident} ({item['author']}): {rest.count('research/img')} plain-text")
    print(f"{len(manifest['items'])} texts rewritten, {problems} problems")
    used = {u for i in after for u in ASSET_RE.findall(i["body"])}
    bad = 0
    for key, url in sorted(manifest["assets"].items()):
        if url not in used:
            print(f"unused asset {key} {url}")
        local = next((IMAGES / key).iterdir())
        try:
            _status, data = fetch_url(url)
        except Exception as e:
            print(f"FAIL {url}: {e}")
            bad += 1
            continue
        if data != local.read_bytes():
            print(f"DIFF {url}: {len(data)} bytes, {local.name} has {local.stat().st_size}")
            bad += 1
        time.sleep(0.2)
    print(f"{len(manifest['assets'])} assets fetched anonymously, {bad} bad")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("collect").set_defaults(func=collect)
    p = sp.add_parser("plan")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=plan)
    p = sp.add_parser("run")
    p.add_argument("--only", help="comma-separated item ids (pr-12, issue-3, comment-<id>)")
    p.add_argument("--limit", type=int, default=0, help="stop after N edited texts")
    p.set_defaults(func=run_all)
    p = sp.add_parser("verify")
    p.add_argument("--no-collect", action="store_true", help="use items.json as it is")
    p.set_defaults(func=verify)
    args = ap.parse_args()
    args.func(args)
