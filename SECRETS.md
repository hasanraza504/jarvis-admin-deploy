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

The three `EXPECTED_*` values are what the build compares the signed binary
against; a build signed with anything else fails instead of being uploaded.

## Secrets (Settings → Secrets)

### Both branches

| Name | What it is |
|---|---|
| `SOURCE_REPO_SSH_KEY` | Private half of a **read-only deploy key** on `InstaFarms/jarvis-admin` |
| `RCLONE_CONFIG_BUILDS` | An rclone config whose `[builds]` remote is pinned to the R2 `app-builds` bucket |

### `android` branch

| Name | How to produce it |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | `base64 -w0 upload.jks` |
| `ANDROID_KEYSTORE_PASSWORD` | Keystore password |
| `ANDROID_KEY_ALIAS` | Key alias inside the keystore |
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

Be aware of what that does and does not buy you: the alias constrains **this
client**, not the credential. Unless the R2 API token itself is scoped to the
`app-builds` bucket, anyone holding it could point a different config at other
buckets. It is defence in depth, not a permission boundary — prefer a
bucket-scoped token, keep the secret tight and rotate it if in doubt.

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
