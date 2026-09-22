"""One-off isolated validation; never included in the upstream contribution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

BASE = "e4c4f73f3b28612d7161170b11e08e580b02123a"
BASE_APP_BLOB = "330d2fb7788128a98af5f711e713cc9d865c859a"
HERE = Path(__file__).resolve().parent
REPO = Path.cwd()
OUT = REPO.parent / "evidence"
OUT.mkdir(exist_ok=True)
REPORT = {"base": BASE, "checks": {}}
TEST = "tests/http/test_accept_headers.py"


def run(label: str, argv: list[str], *, allow_failure=False, extra_env=None):
    print(f"\n=== {label}: {' '.join(argv)} ===", flush=True)
    start = time.monotonic()
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    output = result.stdout.decode("utf-8", errors="replace")
    (OUT / f"{label}.log").write_text(output, encoding="utf-8")
    print(output, flush=True)
    REPORT["checks"][label] = {"exit_code": result.returncode, "seconds": round(time.monotonic() - start, 2)}
    (OUT / "report.json").write_text(json.dumps(REPORT, indent=2) + "\n")
    if result.returncode and not allow_failure:
        raise RuntimeError(f"{label} failed, exit {result.returncode}")
    return result.returncode


def junit(name: str):
    root = ET.parse(OUT / name).getroot()
    cases = list(root.iter("testcase"))
    failures = sorted(f"{c.get('classname')}::{c.get('name')}" for c in cases if c.find("failure") is not None)
    errors = [c for c in cases if c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    assert not errors, f"collection/runtime error in {name}, not a regression assertion"
    return {"total": len(cases), "failed": len(failures), "skipped": len(skipped), "failures": failures}


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == BASE
raw = (REPO / "src/app.py").read_bytes()
assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == BASE_APP_BLOB
assert not (REPO / TEST).exists()
# Establish any pre-existing failures instead of hiding or repairing them in this PR.
run("base-suite", ["uv", "run", "pytest", "tests", "-q", f"--junitxml={OUT / 'base-suite.xml'}"], allow_failure=True)
base_suite = junit("base-suite.xml")
REPORT["base_suite"] = base_suite
shutil.copyfile(HERE / "test_accept_headers.py", REPO / TEST)
run("format-new-test", ["uv", "run", "ruff", "format", TEST])
red = run("regression-before", ["uv", "run", "pytest", TEST, "-q", f"--junitxml={OUT / 'before.xml'}"], allow_failure=True)
before = junit("before.xml")
assert red == 1 and before["failed"] == 12 and before["total"] == 30, before
REPORT["before"] = before
old = b'    ranges = _accept_ranges(request.headers.get("accept", ""))\n'
new = (
    b'    # Accept is a list field: preserve every field line and its order (RFC 9110 section 5.2).\n'
    b'    ranges = _accept_ranges(",".join(request.headers.getlist("accept")))\n'
)
assert raw.count(old) == 1
(REPO / "src/app.py").write_bytes(raw.replace(old, new))
run("regression-after", ["uv", "run", "pytest", TEST, "-q", f"--junitxml={OUT / 'after.xml'}"])
after = junit("after.xml")
assert after["failed"] == 0 and after["total"] == 30, after
REPORT["after"] = after
run("ruff", ["uv", "run", "ruff", "check", "."])
run("format", ["uv", "run", "ruff", "format", "--check", "."])
run("types", ["uv", "run", "ty", "check"])
run("caps", ["uv", "run", "sz.py", "--caps"])
check = run("just-check", ["uv", "run", "just", "check"], allow_failure=True,
            extra_env={"PYTEST_ADDOPTS": f"--junitxml={OUT / 'fixed-suite.xml'}"})
fixed_suite = junit("fixed-suite.xml")
REPORT["fixed_suite"] = fixed_suite
assert not (set(fixed_suite["failures"]) - set(base_suite["failures"])), fixed_suite
assert fixed_suite["total"] >= base_suite["total"] + 30, fixed_suite
REPORT["full_check_green"] = check == 0
if check:
    assert fixed_suite["failed"] > 0, "just check failed for a non-pytest reason"
    print("BASELINE FAILURES REMAIN; NOT CLAIMING ALL CHECKS GREEN", flush=True)
run("coverage", ["uv", "run", "coverage", "report"])
run("ratchet", ["uv", "run", "sz.py", "--check"])
run("example", ["bash", "examples/beautiful_chat.sh"])
run("mcp-build", ["uv", "build", "--project", "mcp", "--out-dir", "dist/mcp"])
packages = sorted(str(p) for p in (REPO / "dist/mcp").iterdir() if p.suffix == ".whl" or p.name.endswith(".tar.gz"))
assert len(packages) == 2
run("mcp-dist", ["uv", "run", "python", "tests/verify_mcp_dist.py", *packages])
run("diff-check", ["git", "diff", "--check"])
changed = set(subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines())
assert changed == {"src/app.py"}, changed
untracked = set(subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], text=True).splitlines())
assert untracked == {TEST}, untracked
# Only these two files cross into the separate publication job.
REPORT["files"] = {}
for name in ("src/app.py", TEST):
    destination = OUT / "payload" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / name, destination)
    REPORT["files"][name] = hashlib.sha256(destination.read_bytes()).hexdigest()
(OUT / "report.json").write_text(json.dumps(REPORT, indent=2) + "\n")
print(json.dumps(REPORT, indent=2), flush=True)
