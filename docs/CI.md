# Continuous integration (CI)

CI means a machine runs the project's checks for you every time you push, on computers that are not yours. Ours is GitHub Actions. It is
configured by one file, `.github/workflows/tests.yml`. This guide explains what that file does, how to read a run, what a green run does and
does not tell you, what it costs, and the rules for changing it. **Every change to CI is appended to the change log at the end of this
guide**, so the guide stays true.

## What runs, and when

| | |
| --- | --- |
| Trigger | Every push (to any branch, and also when a tag is pushed) and every pull request |
| Where | Two clean machines at the same time: `windows-latest` and `ubuntu-latest` |
| What | The whole test suite, `python -m pytest -q`, with `RUN_MCP_TESTS=1` |
| Python | 3.12 |
| Limits | 25 minutes per job; a newer push to the same branch cancels the older run; read-only permissions on the repository |
| Failure handling | `fail-fast: false`: if Windows fails, Linux still finishes, and the reverse, so you always see both results |

Why two systems: the team works on Windows, the container will run Linux, and the code was first written on Linux. Some behaviours differ
(line endings, text encodings, a test that signals its own process), and only a real run on each system shows it. This is how the skill-file
line-ending problem and the Windows-only test failure were found.

## What each step does

| # | Step | What it does |
| --- | --- | --- |
| 1 | `actions/checkout@v4` | Copies the pushed commit onto the machine, the same files a colleague would get from a clone |
| 2 | `actions/setup-python@v5` | Installs Python 3.12 and caches pip downloads, keyed on `requirements-dev.txt`, so later runs are faster |
| 3 | `astral-sh/setup-uv@v5` | Installs `uv`, which launches the MCP (`uv run`) |
| 4 | Install the packages | `python -m pip install -r requirements-dev.txt -c constraints.txt` installs exactly the pinned versions. It is the command the Docker image will use, so a pin that cannot be installed on Linux shows up here, before deployment |
| 5 | Run the tests | `python -m pytest -q` with `RUN_MCP_TESTS=1`: the offline tests, plus the tests that start the real MCP bundle and the real engine against a fake pod and a fake key |

`RUN_MCP_TESTS=1` matters: without it, 5 tests are skipped (that is the "5 skipped" you see in a normal local run).

## Reading a run

1. Open the repository on GitHub, then the **Actions** tab. Every push is a row; click it.
2. You see two jobs, `tests (windows-latest)` and `tests (ubuntu-latest)`. A green tick is a pass, a red cross a failure.
3. Click a job to see its steps. A failure shows as the first red step. Open it and read the last lines: a failing test is named
   (`FAILED tests/test_x.py::test_name`). That name is what to send when asking for help.
4. **Re-run failed jobs** (top right of the run) repeats a run without a new push. A job that fails once and passes on re-run points to a
   timing problem on the runner, not to the code.
5. Each commit in the commit list also carries the tick or cross.
6. Failure emails go to the person who pushed, if notifications for Actions are on: GitHub account, Settings, Notifications, Actions.

If one system is red and the other green, that difference is the information: it is a behaviour that depends on the operating system.

## What a green run means, and what it does not

It means: a clean checkout installs at the pinned versions on both systems, and every test passes, including starting the real MCP and
the real engine.

It does **not** mean the bot answers correctly against the pod. CI has no API key and no pod credentials, on purpose. The live checks run
on a person's machine, by hand:

| Layer | Runs where | Needs secrets | Command |
| --- | --- | --- | --- |
| 1. Offline unit tests | CI and your machine | No | `python -m pytest -q` |
| 2. Real MCP and engine start-up | CI and your machine | No (fake key) | `RUN_MCP_TESTS=1 python -m pytest -q` |
| 3. Live scenarios and answer checks | Your machine only | Yes | `python devtools/run_all.py`, `python devtools/verify_answers.py` |
| 4. Pilot review | People | n/a | the weekly review in `docs/ACCEPTANCE.md` |

The rule for where a new check goes: anything that needs the API key or the pod stays out of CI, and anything deterministic belongs in CI.

## Running the same checks on your machine

PowerShell: `$env:RUN_MCP_TESTS="1"; python -m pytest -q`
Git Bash: `RUN_MCP_TESTS=1 python -m pytest -q`
One file: `python -m pytest -q tests/test_x.py`

## Cost

Checked against GitHub's billing documentation on 2026-11-08; figures can change, so read the account's billing page for the truth.
Standard runners are free for public repositories. For private repositories, the GitHub Free plan includes 2,000 minutes a month (other
plans include more). Windows jobs use minutes at twice the Linux rate, and every job is rounded up to whole minutes. Each push runs two
jobs, so one push costs `Linux minutes + 2 x Windows minutes`. A pushed tag counts as a push. Measured on run #8 (2026-11-08): Windows 1m35s, which rounds to 2 minutes and counts double, so 4; Linux 47s, which rounds to 1
minute, so 1. That is about **5 billable minutes per push**, or about 400 pushes a month on the 2,000-minute allowance. The figure
will change as tests are added: measure again after any large change and note it in the change log.

## Changing CI: the rules of this project

Every change to the workflow or to the test strategy comes with these six things, before it is applied:
1. What changes, in plain language, and why it is needed now.
2. A line-by-line explanation of every workflow line added or changed.
3. What to look for in the Actions tab, and how to tell the new check apart.
4. The cost: extra runs and billable minutes.
5. A proof that a new check can fail: it is broken on purpose and shown to go red.
6. How to undo it: `git revert <commit>`.

The change is then added to the change log below. Real secrets are never put in the workflow or in GitHub's secrets: the live parts stay
out of CI.

## Adding a test, adding a step

- **A new test needs no workflow change.** Put `test_*.py` in `tests/`; pytest finds it and CI runs it on the next push.
- **A new step** goes under `steps:` in `.github/workflows/tests.yml`. Example, not active: a secret scan on every push would be
  `- name: Scan the files for secrets` followed by `run: python devtools/secret_scan.py`. It would be tested on a branch first.
- Test a workflow change on a branch: push the branch, watch the run, and only then merge.
- `tests/test_ci_doc.py` fails if the workflow changes and this guide does not, which is the reminder to update the guide and the log.

## Ideas, not built

| Idea | What it would give | Cost or risk |
| --- | --- | --- |
| Secret scan on every push | Catches a committed key before it spreads | One short step |
| Limit the trigger to `main` and pull requests | Fewer runs, fewer minutes | Branch pushes would not be tested until a pull request |
| Build the Docker image in CI (after Day 3) | Proves the image builds on Linux | A longer job |
| Pin the actions by commit instead of by version tag | Protects against a changed third-party action | Tags move more easily; commits need manual updates |
| A nightly run | Catches a package or pod change with no push | Minutes every day |
| Chart tests (when analytics arrives) | Tests the chart data and structure, not pixels, because fonts and Arabic rendering differ between systems | Depends on the chosen technology |

## Change log

| Date | Change | Why | Explained by |
| --- | --- | --- | --- |
| 2026-11-03 | The workflow file was added: the test suite on Windows and Linux for every push and pull request | Windows-only and Linux-only behaviours need a real run on each | Assistant, with the Windows portability work |
| 2026-11-08 | This guide and its consistency test were added; no change to the workflow | The team asked to be informed of every CI change | Assistant |
| 2026-11-08 | The measured job durations of run #8 were recorded in the cost section; no change to the workflow | The cost estimate became a measurement | Assistant |
