# jarvis-admin — build harness

**This repository contains no application source code, and never should.**

It exists only to run GitHub Actions builds for a private application
repository (named by the `SOURCE_REPO` variable). GitHub Actions minutes on
standard runners — including macOS — are free for public repositories, so
keeping the harness public and the code private gives unlimited iOS and Android
builds at no cost.

## How a build works

1. A build is started from the **Actions** tab or by the app's buildkit tool
   (builds are manual only).
2. The runner clones the private source repo using a **read-only deploy key**
   held in `SOURCE_REPO_SSH_KEY`. A deploy key is scoped to exactly one
   repository and cannot write, so a leak cannot reach anything else.
3. `expo prebuild` generates the native project, and Gradle (Android) or
   `xcodebuild` (iOS) compiles and signs it. Signing material is written from
   repository secrets into the runner's temporary directory and a throwaway
   keychain, which exist only for the life of the job.
4. The build is pushed to **private R2 storage**, not to GitHub Actions
   artifacts. **Nothing is ever submitted to a store from here** — this harness
   only produces files.

Nothing from the source tree is committed here, and the job only uploads the
artifact paths it names explicitly.

The logs are public, so the source tree's tools (npm, expo prebuild, Gradle,
CocoaPods, xcodebuild) run under `.github/scripts/quiet_run.py`. Their full
output stays in a private file on the runner, deleted at the end of the job;
the log shows only allowlisted progress and result lines, and a filtered error
block on failure. The log does still show the source repository's name, the
commit SHA and subject that was built, the app version and build number, the
signing identity names and fingerprints, and the artifact's name and sha256.
[`SECRETS.md`](SECRETS.md) lists exactly what is kept out and what is not.

## Branches

| Branch | Workflow | Runner | Produces |
|---|---|---|---|
| `android` | Build Android | `ubuntu-latest` | `.aab` or `.apk` |
| `ios` | Build iOS | `macos-26` | `.ipa` |

Both workflows are present on both branches. That is not redundancy: GitHub
only allows `workflow_dispatch` for workflows that exist on the repository's
DEFAULT branch, so an iOS workflow living solely on `ios` would be invisible and
un-runnable. The `ios` branch remains the place to edit iOS build logic.

## Running a build

Actions → *Build Android* / *Build iOS* → **Run workflow**, then pick:

- **format** — `aab` or `apk` (Android only)
- **ref** — the source branch, tag or SHA to build
- **profile** — recorded only; this harness has one release configuration

Each build gets a store build number (Android `versionCode`, iOS
`CFBundleVersion`) of minutes since the Unix epoch, so every upload is
numbered above the one before it.

## Getting the build

Builds land in private R2 storage, not in this repository:

```
app-builds/<app>/<platform>/<app>-<platform>-<IST stamp>-run<N>.<ext>
```

A `.sha256` sits beside each file so you can verify the download. The run's
summary page prints the exact filename and hash.

Nothing is published here on purpose: Actions artifacts on a **public**
repository are downloadable by anyone who can see the run, and a signed binary
embeds the whole JS bundle. Sending builds to private storage removes that
exposure entirely.

An iOS build signed for **App Store distribution** will not install directly
on a device; it goes to TestFlight.

## Configuration

See [`SECRETS.md`](SECRETS.md) for every secret and variable, and how to produce
each value.

## Security rules for this repo

- **Never commit application source, `.env` files, keystores, `.p8`/`.p12` keys
  or provisioning profiles.** Everything sensitive belongs in Actions secrets,
  which are encrypted and are not exposed to forks.
- Builds are `workflow_dispatch` only. Do not add `pull_request` or
  `pull_request_target` triggers — a fork could then run modified workflow code
  against this repository's context.
- Keep `permissions: contents: read`.
- Workflow **logs are public**. Never `echo` a secret; pass values via `env:`
  rather than inline `${{ }}` interpolation inside a shell command.
- Any new step that runs the source tree's tooling must run it under
  `quiet_run.py`. Do not print file contents, the resolved app config, an
  environment listing or `package.json` fields.
- No `curl … | sh` installers. A tool is downloaded at a pinned version,
  checked against a pinned sha256, in a step with no secret in its environment.
