"""Publish exactly the independently validated two-file patch to a NEW fork branch.

Run in a separate job from all repository/test code. Never update existing refs.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

REPOSITORY = "almondous/technocore-chat"
BASE = "e4c4f73f3b28612d7161170b11e08e580b02123a"
TREE = "7b015f4191ecad6ec35de8b65d6e55481bcb27a8"
BRANCH = "fix/http-repeated-accept-headers-20260922"
TEST = "tests/http/test_accept_headers.py"
assert os.environ["GITHUB_REPOSITORY"] == REPOSITORY
root = Path.cwd()
report = json.loads((root.parent / "evidence/report.json").read_text())
assert report["base"] == BASE
assert report["before"]["failed"] == 12 and report["after"]["failed"] == 0
assert set(report["files"]) == {"src/app.py", TEST}
assert not (set(report["fixed_suite"]["failures"]) - set(report["base_suite"]["failures"]))
files = {}
for name, expected in report["files"].items():
    data = (root.parent / "evidence/payload" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected
    files[name] = data.decode("utf-8")
raw = (root / "src/app.py").read_bytes()
assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == "330d2fb7788128a98af5f711e713cc9d865c859a"
old = b'    ranges = _accept_ranges(request.headers.get("accept", ""))\n'
new = (
    b'    # Accept is a list field: preserve every field line and its order (RFC 9110 section 5.2).\n'
    b'    ranges = _accept_ranges(",".join(request.headers.getlist("accept")))\n'
)
assert raw.count(old) == 1
assert files["src/app.py"].encode() == raw.replace(old, new)
authored_test = (Path(__file__).resolve().parent / "test_accept_headers.py").read_text()
assert ast.dump(ast.parse(files[TEST])) == ast.dump(ast.parse(authored_test))


def api(path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.github.com/repos/" + REPOSITORY + path,
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


try:
    api("/git/ref/heads/" + BRANCH)
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
else:
    raise RuntimeError("Contribution branch already exists; refusing to overwrite")
# Creating unreferenced objects cannot change any existing branch or pull request.
tree = api("/git/trees", {"base_tree": TREE, "tree": [
    {"path": name, "mode": "100644", "type": "blob", "content": value}
    for name, value in files.items()
]})
commit = api("/git/commits", {
    "message": "fix(http): honor all Accept header field lines",
    "tree": tree["sha"], "parents": [BASE],
    "author": {"name": "almondous", "email": "38044896+almondous@users.noreply.github.com"},
})
api("/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": commit["sha"]})
print(json.dumps({"branch": BRANCH, "head": commit["sha"], "tree": tree["sha"],
                  "full_check_green": report["full_check_green"]}, indent=2))
