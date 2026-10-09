# Executable bridge tests for Technocore PR #545

Tests-only supplement to [PR #545](https://github.com/flop-labs/technocore-chat/pull/545).
Prepared for review; this document does not claim publication or Linux validation.

## Provenance and scope

Base commit: `c3c3170e2b432e2c410dce80d71937bb29728567`, verified on 2026-10-09.
Target repository/branch: `SparcleAI/technocore-chat:fix-interop-gap-recovery`.
The original patch is byte-identical to the supplement discussed on October 8:

```text
bc56fa48cbdec036d9ae7d1c7ffc8d4fdafe0e977eb51dbf99b241499f365f9a  pr545-checkpoint-tests.patch
```

The patch adds only `tests/unit/test_interop_bridge.py` (181 lines, 13 cases).
The published Python example is compiled from `src/interop.md`; the transport,
delivery function and atomic checkpoint saver are synthetic. No network request,
running service, signing key or identity is required. Existing test bodies,
protocol, dependencies, workflows and server code are unchanged.

The tests and documentation were prepared with AI assistance (OpenAI Codex).
Execution results below are local observations, not a maintainer endorsement.

## Contents and integrity

This review supplement adds five files relative to the base: the test file,
this README, the original patch, a standalone internal-gap diagnostic and
`SHA256SUMS`. The last four live in `docs/review/pr545-checkpoint-tests/`.
The patch and test file deliberately contain the same test addition: the file
supports direct review and execution, while the unchanged patch preserves the
previously reviewed artifact and permits standalone application to the base.
They are alternative adoption paths, not two test suites; do not apply the patch
after adopting the commit. The patch and review documentation can be omitted
when incorporating the test into #545.
From the repository root, verify the four payload hashes before execution:

```bash
sha256sum -c docs/review/pr545-checkpoint-tests/SHA256SUMS
```

The manifest deliberately does not hash itself. It verifies byte integrity;
the public commit and its parent establish reviewable Git provenance.

## Apply the standalone patch

For a clean checkout of the base without this supplement:

```bash
test "$(git rev-parse HEAD)" = c3c3170e2b432e2c410dce80d71937bb29728567
git apply --check /path/to/pr545-checkpoint-tests.patch
git apply /path/to/pr545-checkpoint-tests.patch
git status --short
# Only new file: tests/unit/test_interop_bridge.py
```

If the supplement commit has already been checked out or cherry-picked, do not
apply the patch a second time. The documentation files are optional to adoption.

## Validate on Linux

Run from the repository root with Python 3.12, Bash and uv available:

```bash
uv sync --frozen
uv run pytest tests/unit/test_interop_bridge.py -q
uv run pytest tests/http/test_docs.py -q -k reference_bridge
uv run just check
git diff --check
```

The repository pins Python 3.12 in `.python-version`. `rust-just` is a locked
development dependency; no separate just installation is needed. At this base,
`just check` runs Ruff lint/format, ty, size caps, the complete Python suite and
branch-aware project coverage (96% floor). It does not run every CI job: image
builds, MCP packaging/worker builds and the contract recipe are separate.
No workflow or external runner is triggered by this preparation.

Optional coverage of the **test harness**, not the server or documented loop:

```bash
COVERAGE_FILE=.coverage-bridge uv run coverage run --branch --source=tests/unit \
  -m pytest tests/unit/test_interop_bridge.py -q
COVERAGE_FILE=.coverage-bridge uv run coverage report \
  --include='*/test_interop_bridge.py' --show-missing --fail-under=0
```

## Results and limits

- Rechecked on 2026-10-09: patch application, 13 passing cases, Ruff lint and
  formatting. No existing tracked file is modified.
- Windows checks use an existing environment: Python 3.12.9, pytest 9.1.1,
  coverage 7.16.1 and Ruff 0.16.8. This is not a fresh frozen Linux environment.
- Earlier harness coverage: 101/103 statements and 8/8 branches, combined 98.20%.
  The uncovered guard rejects an unexpected fake export. This is not protocol
  coverage and does not demonstrate all failure modes.
- The native Windows full-suite attempt stops at collection because the store
  imports Unix `fcntl`. Full Linux tests and project coverage remain unverified.
- Restart tests assume atomic checkpoint saving. They demonstrate replay after
  interruption, not exactly-once delivery or filesystem power-loss durability.

The 13 cases cover ordinary delivery, retained-prefix recovery, cold start,
export ahead of the poll, empty reads, three missing-successor exports,
poll/export generation mismatch and three restart outcomes. They complement
the existing documentation assertions and executable generation-change refusal;
they do not replace store/HTTP/concurrency tests.

## Newly reported internal-hole limitation

[yukkie3276's review](https://github.com/flop-labs/technocore-chat/pull/545#issuecomment-6073592111)
identifies a separate case not covered by these 13 tests. The original loop checks
the first sequence but not every consecutive sequence in a page. The standalone
diagnostic executes that exact example using synthetic responses:

```bash
uv run python docs/review/pr545-checkpoint-tests/reproduce-internal-gap.py .
# Expected at c3c3170e: exit 1, indicating the proposed refusal invariant is violated.
```

| Case | Input after saved seq 1 | Observed result at the base |
| --- | --- | --- |
| Read internal hole | Same-generation read [2, 4] | Delivers 2 and 4; saves seq 4; no export |
| Export internal hole | Read [4], recovered export [2, 4] | Delivers 2 and 4; saves seq 4; one export |

Both were reproduced locally on 2026-10-09. This diagnostic is outside pytest's
default test paths; its expected nonzero exit is not a passing regression or a
protocol fix. A useful follow-up is to validate continuity before delivery and
checkpoint advancement, with read and export regressions after agreement on the
recovery/refusal policy. No correction to `src/interop.md` is included here.

A nonobserved sequence is not proof of a lost message: retention, expiry,
corrupted input and response-window truncation have different meanings. These
synthetic cases demonstrate silent checkpoint advancement across an unexplained
hole, not loss on a live service. The current store skips unparseable read records;
the clock-rollback/per-record-expiry scenario discussed in
[PR #940](https://github.com/flop-labs/technocore-chat/pull/940) is proposed work,
not verified deployed behavior. Empty caught-up reads are normal in these tests.

Related work: [#481](https://github.com/flop-labs/technocore-chat/issues/481),
[#800](https://github.com/flop-labs/technocore-chat/pull/800),
[#775](https://github.com/flop-labs/technocore-chat/issues/775).
