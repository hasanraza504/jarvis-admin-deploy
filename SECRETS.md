# Secrets and variables

Set these under **Settings → Secrets and variables → Actions**.

Secrets are encrypted, are never printed in logs, and are **not** available to
workflows triggered from forks — which is what makes a public harness repo safe.
Variables are plain text and visible to anyone; only non-sensitive values go
there.

Set every secret from a file or a pipe, never as a command-line argument, so the
value stays out of shell history and process listings:

```bash
gh secret set NAME -R <this repo> < file
```

## Variables (Settings → Variables)

| Name | Value for this repo |
|---|---|
| `SOURCE_REPO` | `InstaFarms/jarvis-admin` |
| `EXPECTED_SIGNING_SHA1` | SHA-1 of the Android upload certificate (`keytool -list -v`) |
| `EXPECTED_IOS_APP_ID` | `<team id>.<bundle id>` of the provisioning profile |
| `EXPECTED_IOS_CERT_SERIAL` | Serial number of the distribution certificate |
| `ANDROID_KEY_ALIAS` | Key alias inside the upload keystore (not sensitive, so not a secret: a secret's value is masked everywhere in the log, and a common word like an alias would blank out unrelated log text) |

The three `EXPECTED_*` values are what the build compares the signed binary
against; a build signed with anything else fails instead of being uploaded.

## Secrets (Settings → Secrets)

### Both branches

| Name | What it is |
|---|---|
| `SOURCE_REPO_SSH_KEY` | Private half of a **read-only deploy key** on `InstaFarms/jarvis-admin` |
| `RCLONE_CONFIG_BUILDS` | An rclone config whose `[builds]` remote reaches the R2 `app-builds` bucket, made with an R2 API token **limited to that bucket** (see below). Both workflows check for it in their first step and stop there if it is missing. |

### `android` branch

| Name | How to produce it |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | `base64 -w0 upload.jks` |
| `ANDROID_KEYSTORE_PASSWORD` | Keystore password |
| `ANDROID_KEY_PASSWORD` | Key password (the keystore password, for a PKCS12 keystore) |

### `ios` branch

| Name | How to produce it |
|---|---|
| `IOS_DIST_CERT_P12_BASE64` | `base64 -w0 dist-cert.p12` |
| `IOS_DIST_CERT_PASSWORD` | Password set when exporting the `.p12` |
| `IOS_PROVISIONING_PROFILE_BASE64` | `base64 -w0 profile.mobileprovision` |

## The Android upload key

The upload keystore was generated for this app, not exported from anywhere:

```bash
keytool -genkeypair -alias upload -keyalg RSA -keysize 4096 -validity 9125 \
  -storetype PKCS12 -keystore upload.jks
```

**Once Google Play has seen an upload key, it is the app's identity there.** A
lost keystore means an upload-key reset through Play Console support, so keep a
backup of the keystore and its password outside this repository.

## The iOS certificate and profile

The `.p12` is an existing team distribution certificate with its private key,
and the profile is an App Store profile for the app's bundle id that names that
certificate. The build reads the team, bundle id and profile name from the
profile itself. A profile only grants the capabilities enabled on its bundle id
when the profile was made; after enabling one (for example Associated Domains),
regenerate the profile and set `IOS_PROVISIONING_PROFILE_BASE64` again.

## The R2 remote

`RCLONE_CONFIG_BUILDS` holds a complete rclone config: an `s3` remote for
Cloudflare R2, and a `[builds]` alias remote pointing at the `app-builds`
bucket, so the workflow only ever names `builds:<app>/<platform>/…`.

**The credential in it must be an R2 API token limited to the `app-builds`
bucket.** The alias constrains only the rclone client; the token is what
GitHub hands to every step that has the secret in its environment, so the
token itself must be unable to reach any other bucket. An account-wide R2
key must never be used here.

1. Cloudflare dashboard → R2 → **Manage API tokens** → **Create API token**:
   permission **Object Read & Write**, **Apply to specific buckets only** →
   `app-builds`, no IP filter needed, TTL as you prefer. Keep the page open:
   it shows the access key id and secret access key once.
2. Write the config in a private temporary directory, using an editor so the
   keys never appear in shell history:

   ```bash
   umask 077
   d=$(mktemp -d) && chmod 700 "$d"
   cat > "$d/rclone.conf" <<'EOF'
   [r2]
   type = s3
   provider = Cloudflare
   access_key_id = PASTE_ACCESS_KEY_ID
   secret_access_key = PASTE_SECRET_ACCESS_KEY
   endpoint = https://<account id>.r2.cloudflarestorage.com
   no_check_bucket = true

   [builds]
   type = alias
   remote = r2:app-builds
   EOF
   ${EDITOR:-vi} "$d/rclone.conf"      # replace the two PASTE_ values and the account id
   ```

   `no_check_bucket = true` is required: a bucket-scoped token may not list
   or create buckets.
3. Prove the scope before setting it. The first command must list the
   `app-builds` folders. The second must either fail with an access error or
   list `app-builds` alone; if it lists any other bucket, the token is not
   bucket-scoped, so do not use it:

   ```bash
   rclone --config "$d/rclone.conf" lsf builds: --dirs-only
   rclone --config "$d/rclone.conf" lsd r2:
   ```

4. Set the secret from the file, check it by name, and delete the file:

   ```bash
   gh secret set RCLONE_CONFIG_BUILDS -R <this repo> < "$d/rclone.conf"
   gh secret list -R <this repo>
   rm -rf "$d"
   ```

Rotate by creating a new token, repeating these steps and then deleting the
old token in the Cloudflare dashboard.

rclone itself is installed by its own workflow step, at a pinned version whose
archive must match a pinned sha256, and that step has no secret in its
environment.

## What the public log shows

Workflow logs on a public repository are public. The private tree is never
committed here, and its tools' output is kept out of the log.

**The wrapper.** `npm ci`, `expo prebuild`, `pod install`, the profile helper,
Gradle and `xcodebuild` run under `.github/scripts/quiet_run.py`. Their full
output goes to a mode-600 file in `$RUNNER_TEMP/private-logs/` (mode 700),
which is never uploaded as an artifact or a cache and is deleted by the scrub
step. Everything the wrapper prints sits between `::stop-commands::<token>` and
`::<token>::`, with a random token per invocation, so none of it can act as a
workflow command.

**While a tool runs**, a line reaches the log only if it matches an allowlisted
shape in full: Gradle task names of the `:app` module (other modules' tasks are
only counted), Gradle's result lines, the `npm ci` "added N packages" line, the
four expo prebuild progress lines, the two CocoaPods summary lines, the
xcodebuild `** ARCHIVE SUCCEEDED **`-style result lines, and Metro's
"Bundled" line for a `node_modules/` entry point. Every run ends with the
wrapper's own summary: line counts, how many were withheld, and that the full
output existed only on the runner and was removed.

**When a tool fails**, the log gets the same summary (with the exit status) and
an error block in which each line is a fixed template, never the tool's own
text. A tool line that matches none of these shapes, starts with whitespace, or
is longer than 1,000 characters is withheld:

| Tool line | What the log shows |
|---|---|
| `> Task :app:X FAILED`, `Execution failed for task ':app:X'.` | the same, with a library module's name replaced by `<library module>` |
| `* What went wrong:` and the lines under it | `* What went wrong: <details withheld>` |
| `FAILURE: Build failed with an exception.`, `BUILD FAILED in …` | the same |
| a compiler error, `<path>:<line>[:<col>]: error: …` | `error: <L error in K>`, where L is javac, Kotlin, Swift, Clang or compiler, and K: one of an app source file, the generated android project, the generated ios project, a dependency in node_modules, a file outside the source tree |
| a Kotlin `e: file://<path>…` error | `e: <Kotlin compiler error in K>` |
| an xcodebuild `<project>.xcodeproj: error: …` | for five signing messages (a profile lacking a capability or an entitlement, or not matching an entitlement; no profiles for an app id; no signing certificate of a type), the message with the target suffix dropped. These name only the profile, capability, entitlement and app id, which come from this repo's own secrets. Any other: `xcodebuild: error: <project error withheld>` |
| `** ARCHIVE FAILED **` and similar | the same |
| `The following build commands failed:` and its list | the same heading, `failed build command: <phase>` per entry (for example `CompileSwift`), and `(N failures)` |
| `npm error code X`, `npm error errno X` | the same (the code is an upper-case identifier, such as `ERESOLVE`, `E401`, `ENOTFOUND`) |
| `XError: …` or `x.y.ZException: …` | the class, if it is a standard JavaScript error or under `java.`, `javax.`, `kotlin.`, `org.gradle.`, `com.android.` or `groovy.`, followed by `: <message withheld>`. Any other class is `<error class withheld>` |
| a CocoaPods `[!] …` line | `[!] <CocoaPods error withheld>` |
| `error: …`, `fatal: …`, `ERROR: …`, `error[X]: …` | `error: <message withheld>` (and so on) |

A template that repeats is printed once with `(xN)`, and the block stops at 40
lines. The full text is never in the public log: to see it, rerun the build
privately (on a machine or a private repository you control), not here.

**What a public log can still show from the private tree:**

- the source repository's name, the ref built, the commit SHA, and that
  commit's subject line (all printed by `actions/checkout`);
- the Node version pinned in the tree's `.nvmrc` (printed by `actions/setup-node`);
- the app's version and build number, and on iOS the scheme and workspace name
  of the generated project (printed by the harness, and in the per-step `env:`
  listings GitHub shows);
- the names of Gradle tasks in the `:app` module, and the counts the wrapper
  prints;
- on failure, the error templates above: which task or build phase failed, the
  language and kind of file a compiler error is in, the npm error code, a
  standard error class, and the exit status.

The log also shows signing details that come from this repo's own secrets and
variables, not from the tree: the certificate's name, the profile's name, UUID,
team and app id, the key alias, and the expected and actual signing
fingerprints and serial. The artifact's file name (built from this repo's name,
a timestamp and the run number) and its sha256 appear at the end.

**Caches.** No step saves an Actions cache: `actions/setup-node` runs without
its `cache` input, and `actions/setup-java` and `android-actions/setup-android`
cache nothing unless asked. npm's own debug log, which names the tree's
dependencies and paths, is turned off (`npm_config_logs_max=0` for the whole
job), and the scrub step removes `~/.npm/_logs` as well.

## The deploy key

`SOURCE_REPO_SSH_KEY` holds the private half of an ed25519 deploy key registered
on `InstaFarms/jarvis-admin` with **write access disabled**. This is deliberately preferred
over a personal access token:

- it is bound to one repository and cannot reach any other,
- it is read-only, so it cannot push, tag or delete,
- it belongs to no user, so revoking it locks out nobody.

The private half exists only as this secret: it was set straight from the
generated file, and the file was then deleted. To rotate, generate a new pair,
replace the deploy key and set the secret again.

Revoke or rotate it under `InstaFarms/jarvis-admin` → Settings → Deploy keys.
