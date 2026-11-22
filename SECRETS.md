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
committed here, and its tools' output is kept out of the log:

- `npm ci`, `expo prebuild`, `pod install`, the profile helper, Gradle and
  `xcodebuild` run under `.github/scripts/quiet_run.py`. Their full output
  goes to a mode-600 file under `$RUNNER_TEMP/private-logs/`, which is never
  uploaded as an artifact or cache and is deleted by the scrub step.
- The log shows only allowlisted lines: Gradle `:app:` task names, the
  `npm ci`, prebuild, CocoaPods and xcodebuild result lines, and timings.
- On failure it shows a filtered error block: error lines and the last lines
  of output, minus anything JSON-shaped, longer than 300 characters,
  base64-shaped, an environment listing, a code frame, a note or warning, or
  credential-shaped. Paths into the private tree outside `node_modules/`,
  `ios/` and `android/` are replaced with `<src>/<app file>`.

What the log does still show: the source repository's name, the ref and
commit SHA built, and that commit's subject line (printed by
`actions/checkout`); the app's version and build number; the signing
certificate's name and the profile's name and app id; the expected and actual
signing fingerprints; and the artifact's file name and sha256. Nothing else
from the private tree is printed by the harness's own steps.

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
