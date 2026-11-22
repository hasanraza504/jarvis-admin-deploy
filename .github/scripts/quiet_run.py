#!/usr/bin/env python3
"""Run a build tool with its output kept off the public log.

    quiet_run.py LABEL -- COMMAND [ARGS...]

The workflow logs of this repository are public, and the tools that run the
private source tree (npm, expo prebuild, Gradle, CocoaPods, xcodebuild) print
parts of it: resolved app config, package.json fields, dependency lists, file
paths, code frames. So the default is deny:

* every byte of the command's output goes to a private file,
  $QUIET_RUN_LOG_DIR/LABEL.log (mode 600), which the scrub step deletes and
  which is never uploaded as an artifact or a cache;
* only lines matching LIVE below reach the public log while it runs: step and
  task names, result lines and timings;
* if the command fails, a filtered error block is printed: error-looking lines
  and the last lines of the output, minus anything JSON-shaped, over-long,
  base64-shaped, an environment listing, a code frame or credential-shaped,
  and with paths into the private tree outside node_modules/, ios/ and
  android/ replaced by "<src>/<app file>".

The exit status is the command's. Everything here is generic; nothing in this
file is specific to one app.
"""

import os
import re
import subprocess
import sys
import time

# Lines allowed onto the public log while the command runs. Each pattern must
# match the WHOLE line (after colour codes are stripped).
LIVE = [re.compile(p) for p in (
    # Gradle: the app module's task names (library modules' names would list the
    # app's native dependencies, so those are only counted), and result lines.
    r"> Task :app:[A-Za-z0-9_]+( (UP-TO-DATE|NO-SOURCE|SKIPPED|FROM-CACHE|FAILED))?",
    r"BUILD (SUCCESSFUL|FAILED) in [0-9hms ]+",
    r"[0-9]+ actionable tasks?: [0-9]+ [a-z-]+(, [0-9]+ [a-z-]+)*",
    r"FAILURE: Build failed with an exception\.",
    # npm ci
    r"added [0-9]+ packages?( from [0-9]+ contributors?)?(, (removed|changed) [0-9]+ packages?)* in [0-9.]+m?s",
    # expo prebuild
    r"✔ (Created native directory|Cleared (android|ios) code|Finished prebuild|Updated package\.json( \| no changes)?)",
    # CocoaPods
    r"Pod installation complete! There (is|are) [0-9]+ dependenc(y|ies) from the Podfile and [0-9]+ total pods? installed\.",
    r"Pod install took [0-9.]+ \[s\] to run",
    # xcodebuild
    r"\*\* (ARCHIVE|EXPORT|BUILD|CLEAN) (SUCCEEDED|FAILED) \*\*",
    # Metro, when Gradle or Xcode bundles the JS: entry point is a public package.
    r"(Android|iOS) Bundled [0-9.]+ ?ms node_modules/[A-Za-z0-9@/._-]+ \([0-9]+ modules?\)",
)]

GRADLE_TASK = re.compile(r"> Task :[A-Za-z0-9_.:-]+")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Failure block: which lines are candidates, and which are always dropped.
ERRORISH = re.compile(
    r"(?i)(\berror\b|\bfailed\b|^e: |exception|what went wrong|cannot find|not found|"
    r"\bfatal\b|denied|unable to|could not|\*\* .* FAILED \*\*)")
TAIL = 30
MAX_BLOCK = 150
MAX_LEN = 300
DROP = [re.compile(p) for p in (
    r"^\s*[\[{]",                        # starts like JSON
    r"^\s*[}\]],?\s*$",                  # closes JSON
    r"\"[^\"\s]{1,80}\"\s*:",            # "key": anywhere — a JSON or JS object
    r"(?i)expoConfig|\"scripts\"|manifest\.json|app\.config",
    r"[A-Za-z0-9+/]{60,}={0,2}",         # base64-shaped run
    r"^\s*[A-Z][A-Z0-9_]{2,}=",          # KEY=value environment listing
    r"^\s{2,}[A-Z][A-Z0-9_]{2,}: ",      # "  KEY: value" environment listing
    r"^\s*>?\s*[0-9]+\s*\|",             # code frame:  12 | const x = ...
    r"^\s*\|\s*\^",                      # code frame caret
    r"(?i)passw|secret|token|private[ _-]?key|BEGIN [A-Z ]*KEY",
    r"^(\S+: )?(note|warning|w): ",     # notes and warnings name dependency targets
)]
SAFE_SRC_DIRS = ("node_modules/", "ios/", "android/")


def clean(raw):
    return ANSI.sub("", raw).rstrip("\r\n").rstrip()


def live_ok(line):
    return any(p.fullmatch(line) for p in LIVE)


def scrub_paths(line, src_roots):
    for root in src_roots:
        line = line.replace("file://" + root, "<src>").replace(root, "<src>")

    def app_file(m):
        rest = m.group(1)
        return m.group(0) if rest.startswith(SAFE_SRC_DIRS) else "<src>/<app file>"
    return re.sub(r"<src>/([^\s:'\"),]*)", app_file, line)


def failure_block(lines, src_roots):
    picked = []
    in_gradle_block = False
    for i, line in enumerate(lines):
        if line.startswith("* What went wrong:"):
            in_gradle_block = True
        elif line.startswith(("* Try:", "* Exception is:", "* Get more help")):
            in_gradle_block = False
        if in_gradle_block or ERRORISH.search(line) or i >= len(lines) - TAIL:
            picked.append(line)
    out, seen, dropped = [], set(), 0
    for raw in picked:
        # Indentation matters to some patterns (an indented "KEY: value" is an
        # environment listing), so test the line before and after stripping.
        unstripped = scrub_paths(raw, src_roots)
        line = unstripped.strip()
        if not line or line in seen:
            continue
        if len(line) > MAX_LEN or any(p.search(unstripped) or p.search(line) for p in DROP):
            dropped += 1
            continue
        seen.add(line)
        out.append(line)
    if len(out) > MAX_BLOCK:
        dropped += len(out) - MAX_BLOCK
        out = out[-MAX_BLOCK:]
    return out, dropped


def emit(line):
    # A tool's line must never become a workflow command (::add-mask:: etc.).
    if line.startswith("::"):
        line = " " + line
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv):
    if len(argv) < 4 or argv[2] != "--" or not re.fullmatch(r"[a-z0-9-]+", argv[1]):
        sys.stderr.write("usage: quiet_run.py LABEL -- COMMAND [ARGS...]  (LABEL: a-z, 0-9, -)\n")
        return 2
    label, cmd = argv[1], argv[3:]
    log_dir = os.environ.get("QUIET_RUN_LOG_DIR") or os.path.join(
        os.environ.get("RUNNER_TEMP", "/tmp"), "private-logs")
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    os.chmod(log_dir, 0o700)
    log_path = os.path.join(log_dir, label + ".log")
    src = os.environ.get("QUIET_RUN_SRC") or os.path.join(os.environ.get("GITHUB_WORKSPACE", os.getcwd()), "src")
    src_roots = sorted({src.rstrip("/"), os.path.realpath(src).rstrip("/")}, key=len, reverse=True)

    started = time.time()
    total = shown = tasks = 0
    lines = []
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as log:
        try:
            child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            emit(f"{label}: could not start {os.path.basename(cmd[0])}: {exc.strerror}")
            return 127
        for raw in child.stdout:
            log.write(raw)
            total += 1
            line = clean(raw.decode("utf-8", "replace"))
            lines.append(line)
            if GRADLE_TASK.match(line):
                tasks += 1
            if live_ok(line):
                shown += 1
                emit(line)
        code = child.wait()
    if code < 0:
        code = 128 - code
    took = time.time() - started
    summary = f"{label}: {total} output lines kept in the runner's private log, {shown} shown"
    if tasks:
        summary += f", {tasks} Gradle tasks"
    if code == 0:
        emit(f"{summary}; ok in {took:.0f}s")
        return 0
    block, dropped = failure_block(lines, src_roots)
    emit(f"{summary}; FAILED with exit {code} after {took:.0f}s")
    emit(f"--- {label}: filtered error output ({len(block)} lines, {dropped} more withheld by the filter) ---")
    for line in block:
        emit(line)
    emit(f"--- end of {label} error output; the full log is deleted with the runner ---")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
