#!/usr/bin/env python3
"""Run a build tool with its output kept off the public log.

    quiet_run.py LABEL -- COMMAND [ARGS...]

The workflow logs of this repository are public, and the tools that run the
private source tree (npm, expo prebuild, Gradle, CocoaPods, xcodebuild) print
parts of it: resolved app config, package.json fields, dependency lists, file
paths, quoted source lines. So both paths are default-deny:

* Every byte of the command's output goes to a private file,
  $QUIET_RUN_LOG_DIR/LABEL.log (mode 600, directory 700), which the scrub step
  deletes and which is never uploaded as an artifact or a cache.
* While it runs, a line reaches the public log only if it matches one of the
  LIVE patterns in full: task names, result lines, timings.
* If it fails, a line reaches the public log only if it matches one of the
  FAILURE shapes below, and then only as that shape's fixed template: the
  error code or class, the Gradle task, the xcodebuild phase, the kind of file
  an error is in. Message text, paths, identifiers and quoted source never pass.
  The full text is available only by rerunning privately.
* Everything this script prints is wrapped in ::stop-commands::<random token>
  ... ::<token>::, so nothing it prints can act as a workflow command.

The exit status is the command's. Nothing in this file is specific to one app.
"""

import os
import re
import secrets
import subprocess
import sys
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
MAX_LIVE_LEN = 300
MAX_FAILURE_LEN = 1000
MAX_BLOCK = 40

# ---------------------------------------------------------------- live path
# Each pattern must match a whole segment (colour codes stripped, split at
# CR and LF, trailing spaces removed). Leading whitespace never matches.
LIVE = [re.compile(p) for p in (
    # Gradle: the app module's task names (library modules' names would list the
    # app's native dependencies, so those are only counted), and result lines.
    r"> Task :app:[A-Za-z0-9_]{1,80}( (UP-TO-DATE|NO-SOURCE|SKIPPED|FROM-CACHE|FAILED))?",
    r"BUILD (SUCCESSFUL|FAILED) in [0-9hms ]{1,20}",
    r"[0-9]{1,6} actionable tasks?: [0-9]{1,6} [a-z-]{1,20}(, [0-9]{1,6} [a-z-]{1,20}){0,4}",
    r"FAILURE: Build failed with an exception\.",
    # npm ci
    r"added [0-9]{1,6} packages?( from [0-9]{1,6} contributors?)?(, (removed|changed) [0-9]{1,6} packages?){0,2} in [0-9.]{1,8}m?s",
    # expo prebuild
    r"✔ (Created native directory|Cleared (android|ios) code|Finished prebuild|Updated package\.json( \| no changes)?)",
    # CocoaPods
    r"Pod installation complete! There (is|are) [0-9]{1,5} dependenc(y|ies) from the Podfile and [0-9]{1,5} total pods? installed\.",
    r"Pod install took [0-9.]{1,8} \[s\] to run",
    # xcodebuild
    r"\*\* (ARCHIVE|EXPORT|BUILD|CLEAN) (SUCCEEDED|FAILED) \*\*",
    # Metro, when Gradle or Xcode bundles the JS: the entry point is a public package.
    r"(Android|iOS) Bundled [0-9.]{1,8} ?ms node_modules/[A-Za-z0-9@/._-]{1,120} \([0-9]{1,7} modules?\)",
)]
GRADLE_TASK = re.compile(r"> Task :")

# ------------------------------------------------------------- failure path
# A failure line passes only as the template of the first shape it matches.
# A segment that starts with whitespace matches nothing (source lines, carets,
# JSON or object interiors, stack frames, notes under an error), except inside
# xcodebuild's "The following build commands failed:" list, where only the
# phase word is kept.

STANDARD_JS_ERRORS = {
    "Error", "TypeError", "SyntaxError", "ReferenceError", "RangeError", "EvalError",
    "URIError", "AggregateError", "SystemError", "CommandError", "ConfigError",
}
STANDARD_JVM_PREFIXES = ("java.", "javax.", "kotlin.", "org.gradle.", "com.android.", "groovy.")
LANGUAGE = {
    ".java": "javac", ".kt": "Kotlin", ".kts": "Kotlin", ".swift": "Swift",
    ".m": "Clang", ".mm": "Clang", ".c": "Clang", ".cc": "Clang", ".cpp": "Clang",
    ".h": "Clang", ".hpp": "Clang",
}
# (shape, template). The profile name, capability, entitlement and bundle id
# come from this harness's own signing secrets, not from the source tree.
XCODE_KNOWN = [(re.compile(p), t) for p, t in (
    (r"Provisioning profile (\"[^\"]{1,64}\") doesn't include the ([A-Za-z ]{1,48}) capability\.",
     r"Provisioning profile \1 doesn't include the \2 capability."),
    (r"Provisioning profile (\"[^\"]{1,64}\") doesn't include the ([a-z0-9.-]{1,80}) entitlement\.",
     r"Provisioning profile \1 doesn't include the \2 entitlement."),
    (r"Provisioning profile (\"[^\"]{1,64}\") doesn't match the entitlements file's value for the ([a-z0-9.-]{1,80}) entitlement\.",
     r"Provisioning profile \1 doesn't match the entitlements file's value for the \2 entitlement."),
    (r"No profiles for ('[A-Za-z0-9.-]{1,100}') were found(: .{0,300})?",
     r"No profiles for \1 were found"),
    (r"No signing certificate (\"[A-Za-z ]{1,40}\") found(: .{0,300})?",
     r"No signing certificate \1 found"),
)]
XCODE_TARGET_SUFFIX = re.compile(r" \(in target '[^']*' from project '[^']*'\)$")


def file_kind(path, src_roots):
    """Which kind of file an error is in. Never the path itself."""
    if path.startswith("file://"):
        path = path[len("file://"):]
    for root in src_roots:
        if path.startswith(root + "/"):
            rest = path[len(root) + 1:]
            if rest.startswith("node_modules/"):
                return "a dependency in node_modules"
            if rest.startswith("android/"):
                return "the generated android project"
            if rest.startswith("ios/"):
                return "the generated ios project"
            return "an app source file"
    if "/node_modules/" in path:
        return "a dependency in node_modules"
    return "a file outside the source tree"


def gradle_task(path):
    parts = path.strip(":").split(":")
    if not parts or not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", p) for p in parts):
        return None
    task = parts[-1]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", task):
        return None
    if parts[:-1] == ["app"]:
        return ":app:" + task
    if not parts[:-1]:
        return ":" + task
    return ":<library module>:" + task


def error_class(name, has_message):
    if name in STANDARD_JS_ERRORS or name.startswith(STANDARD_JVM_PREFIXES):
        shown = name
    else:
        shown = "<error class withheld>"
    return shown + (": <message withheld>" if has_message else "")


class FailureShapes:
    """Reduces a failed command's output to fixed templates."""

    def __init__(self, src_roots):
        self.src_roots = src_roots
        self.in_failed_commands = False

    def reduce(self, seg):
        if len(seg) > MAX_FAILURE_LEN:
            return None
        if self.in_failed_commands:
            m = re.fullmatch(r"\(([0-9]{1,4}) failures?\)", seg)
            if m:
                self.in_failed_commands = False
                return seg
            m = re.fullmatch(r"\s+([A-Z][A-Za-z]{1,40})(\s.*)?", seg)
            if m:
                return "failed build command: " + m.group(1)
            self.in_failed_commands = False
        if seg[:1].isspace():
            return None

        m = re.fullmatch(r"> Task (:\S{1,200}) FAILED", seg)
        if m:
            t = gradle_task(m.group(1))
            return "> Task %s FAILED" % t if t else None
        m = re.fullmatch(r"Execution failed for task '(:[^'\s]{1,200})'\.", seg)
        if m:
            t = gradle_task(m.group(1))
            return "Execution failed for task '%s'." % t if t else None
        if seg == "FAILURE: Build failed with an exception." or re.fullmatch(r"BUILD FAILED in [0-9hms ]{1,20}", seg):
            return seg
        if seg == "* What went wrong:":
            return "* What went wrong: <details withheld>"

        m = re.fullmatch(r"e: (?:file://)?(/[^:\s]{1,400}\.kts?)(?::\d+:\d+|: \(\d+, \d+\):)? .*", seg)
        if m:
            return "e: <Kotlin compiler error in %s>" % file_kind(m.group(1), self.src_roots)
        m = re.fullmatch(r"(/[^:\s]{1,400}?(\.[A-Za-z]{1,5})):\d{1,6}(?::\d{1,6})?: (?:fatal )?error: .*", seg)
        if m:
            lang = LANGUAGE.get(m.group(2), "compiler")
            return "error: <%s error in %s>" % (lang, file_kind(m.group(1), self.src_roots))
        m = re.fullmatch(r"(/[^:\s]{1,400}\.(?:xcodeproj|xcworkspace)): error: (.*)", seg)
        if m:
            message = XCODE_TARGET_SUFFIX.sub("", m.group(2))
            for shape, template in XCODE_KNOWN:
                known = shape.fullmatch(message)
                if known:
                    return "xcodebuild: error: " + known.expand(template)
            return "xcodebuild: error: <project error withheld>"
        if re.fullmatch(r"\*\* (ARCHIVE|EXPORT|BUILD|CLEAN|TEST) FAILED \*\*", seg):
            return seg
        if seg == "The following build commands failed:":
            self.in_failed_commands = True
            return seg

        m = re.fullmatch(r"npm (?:error|ERR!) code ([A-Z][A-Z0-9_]{1,23})", seg)
        if m:
            return "npm error code " + m.group(1)
        m = re.fullmatch(r"npm (?:error|ERR!) errno (-?[0-9]{1,6}|[A-Z][A-Z0-9_]{1,23})", seg)
        if m:
            return "npm error errno " + m.group(1)

        m = re.fullmatch(r"((?:[A-Za-z_$][\w$.]{0,80})?(?:Error|Exception))(:\s.*)?", seg)
        if m:
            return error_class(m.group(1), bool(m.group(2)))
        if seg.startswith("[!] "):
            return "[!] <CocoaPods error withheld>"
        m = re.fullmatch(r"(error|fatal|ERROR|FATAL)(\[[A-Za-z0-9_-]{1,20}\])?: .*", seg)
        if m:
            return "%s%s: <message withheld>" % (m.group(1), m.group(2) or "")
        return None


def segments(raw):
    """Colour codes stripped; CR and LF both end a segment, as a terminal shows."""
    text = ANSI.sub("", raw.decode("utf-8", "replace"))
    return [s.rstrip() for s in re.split(r"\r\n|\r|\n", text) if s.strip()]


def live_ok(seg):
    return len(seg) <= MAX_LIVE_LEN and any(p.fullmatch(seg) for p in LIVE)


def failure_block(segs, src_roots):
    shapes = FailureShapes(src_roots)
    order, counts = [], {}
    for seg in segs:
        out = shapes.reduce(seg)
        if out is None:
            continue
        if out not in counts:
            order.append(out)
            counts[out] = 0
        counts[out] += 1
    lines = [o + (" (x%d)" % counts[o] if counts[o] > 1 else "") for o in order]
    if len(lines) > MAX_BLOCK:
        lines = lines[:MAX_BLOCK] + ["<%d more error lines withheld>" % (len(lines) - MAX_BLOCK)]
    return lines


def main(argv):
    if len(argv) < 4 or argv[2] != "--" or not re.fullmatch(r"[a-z0-9-]{1,40}", argv[1]):
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

    token = secrets.token_hex(16)
    out = sys.stdout

    def emit(line):
        out.write(line + "\n")
        out.flush()

    emit("::stop-commands::" + token)
    try:
        started = time.time()
        total = shown = tasks = 0
        segs = []
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as log:
            try:
                child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError:
                emit("%s: could not start the command" % label)
                return 127
            for raw in child.stdout:
                log.write(raw)
                for seg in segments(raw):
                    total += 1
                    segs.append(seg)
                    if GRADLE_TASK.match(seg):
                        tasks += 1
                    if live_ok(seg):
                        shown += 1
                        emit(seg)
            code = child.wait()
        if code < 0:
            code = 128 - code
        took = time.time() - started
        counted = "%d output lines, %d shown" % (total, shown) + (", %d Gradle tasks" % tasks if tasks else "")
        kept = ("the full output existed only on this runner, in a private file the scrub step removes; "
                "rerun privately for the full text")
        if code == 0:
            emit("%s: ok in %.0fs; %s, %d withheld; %s" % (label, took, counted, total - shown, kept))
            return 0
        block = failure_block(segs, src_roots)
        emit("--- %s: error shapes (message text, paths and source withheld) ---" % label)
        for line in block:
            emit(line)
        emit("%s: FAILED with exit %d after %.0fs; %s, %d error shapes, %d lines withheld; %s"
             % (label, code, took, counted, len(block), total - shown, kept))
        return code
    finally:
        emit("::%s::" % token)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
