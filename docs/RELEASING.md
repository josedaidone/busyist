# Releasing buZyist

Releases are built and published by GitHub Actions (`.github/workflows/release.yml`). Pushing a
tag like `v1.0.1` starts the build; nothing is published until a tag is pushed.

## Make a release

1. Bump the version in `busyist.py`. Keep the exact format, since `build.ps1`, the PyInstaller spec,
   the Inno script and CI all read it by regex:

   ```python
   __version__ = "1.0.1"
   ```

2. Commit and push:

   ```powershell
   git commit -am "buZyist 1.0.1"
   git push
   ```

3. Tag the same version and push the tag:

   ```powershell
   git tag v1.0.1
   git push origin v1.0.1
   ```

   The build fails on purpose if the tag and `__version__` differ.

4. Watch the run on the [Actions tab](https://github.com/josedaidone/buzyist/actions)
   ("Build and release", about 5–10 minutes). It checks the version, installs Inno Setup, runs
   `build.ps1` and publishes the release.

5. When it's green, the [latest release](https://github.com/josedaidone/buzyist/releases/latest)
   has `Buzyist-Setup-x.y.z.exe`, the portable zip, `SHA256SUMS.txt` and (once signing is set up,
   see below) `SHA256SUMS.txt.minisig`, with notes generated from
   the commits. Edit the notes on the release page if you want.

## Test a build without publishing

On the Actions tab, open **Build and release** and click **Run workflow**. The installer and zip are
uploaded as a downloadable artifact of that run, and no release is created.

## If it fails

- Open the failed step and read its log.
- **Tag doesn't match the version**: delete the tag, fix `__version__`, commit, then re-tag:

  ```powershell
  git tag -d v1.0.1
  git push origin :refs/tags/v1.0.1
  ```

- **Release step fails with a permissions error**: go to **Settings → Actions → General → Workflow
  permissions**, choose **Read and write permissions**, then re-run the job.

## Signing updates

The in-app updater downloads `SHA256SUMS.txt` and the installer from the same GitHub release. The
checksum alone only proves the download isn't damaged: anyone who can publish a release (a leaked CI
token, the GitHub account, a compromised action) could replace both files, and every installed copy
would install that in silence. So `SHA256SUMS.txt` is signed with [minisign](https://jedisct1.github.io/minisign/)
(Ed25519), and the signature ships as the release asset `SHA256SUMS.txt.minisig`.

### What the app does

`UPDATE_PUBKEYS` in `busyist.py` lists the public keys an update may be signed with.

- **`UPDATE_PUBKEYS` is empty (how the code ships today):** nothing is checked. The app logs
  `this build has no update signing key; trusting SHA256SUMS.txt without a signature` and installs
  any release whose installer matches `SHA256SUMS.txt`, exactly as before signing existed.
- **`UPDATE_PUBKEYS` has a key:** before it trusts `SHA256SUMS.txt`, the app downloads
  `SHA256SUMS.txt.minisig` and checks it against those keys (prehashed `ED` mode, key id, and the
  signature over the trusted comment, like `minisign -V`). If the `.minisig` is missing or doesn't
  verify, the update is **refused, with no fallback** to the checksum alone: the log says why and
  Settings shows "The update isn't signed by buZyist; not installing it." The installer isn't even
  downloaded.
- Either way, the installer, `SHA256SUMS.txt` and the `.minisig` must come from
  `https://github.com/josedaidone/buzyist/releases/download/...`, or the update is refused.

A copy only starts checking once it runs a version with a key in `UPDATE_PUBKEYS`: the update *to*
that version is still checked the old way, every update after it is checked against the key.

### Make the key (once, offline)

On a PC you trust, with either the real `minisign` or the stdlib-only tool in this repo (both read
and write the same files):

```powershell
minisign -G -p buzyist.pub -s buzyist.key
# or
python tools\minisign.py generate -p buzyist.pub -s buzyist.key
```

Use a strong password. `buzyist.key` is the secret key: keep it, and a backup of it on a second
offline medium (a USB stick in a drawer, a password manager), out of the repo (`*.key` is
gitignored). **If the key is lost, installed copies can't update themselves any more** (people will
have to download the next version by hand), so keep that backup.

Then put the second line of `buzyist.pub` (it starts with `RW`) in `busyist.py`:

```python
UPDATE_PUBKEYS: list[str] = ["RW...your key..."]
```

Make a second key pair the same way and add it after the first one: that is the next key, for a
rotation (see below). Keep its secret key offline too.

### Give the key to CI

The workflow's "Sign the checksums" step signs `dist\SHA256SUMS.txt` with
`python tools\minisign.py sign ... --app busyist.py --optional`. Add two repository secrets
(**Settings → Secrets and variables → Actions**):

- `MINISIGN_SECRET_KEY`: the whole content of `buzyist.key` (both lines);
- `MINISIGN_PASSWORD`: its password.

Safer: put them in a GitHub **Environment** named `release` with a required reviewer, and give the
build job `environment: release`, so a release can't be signed without someone approving it. Safest:
don't give CI the key at all; let CI build, download the artifact, sign `SHA256SUMS.txt` on your PC
and upload `SHA256SUMS.txt.minisig` to the release before publishing it (start it as a draft).

Without the secrets the step only warns and the release ships unsigned, **as long as
`UPDATE_PUBKEYS` is empty**. Once it holds a key, the step fails instead, because every copy would
refuse an unsigned release. It also refuses to sign with a key that isn't in `UPDATE_PUBKEYS`.

### Sign by hand

```powershell
# while building
powershell -ExecutionPolicy Bypass -File build.ps1 -SigningKey C:\keysuzyist.key

# or afterwards
python tools\minisign.py sign -s C:\keysuzyist.key -m dist\SHA256SUMS.txt --app busyist.py
# or with the real minisign (prehashed is its default)
minisign -S -s C:\keysuzyist.key -m dist\SHA256SUMS.txt
```

Check a signature: `minisign -V -p buzyist.pub -m dist\SHA256SUMS.txt`.

### Rotate the key

1. Ship a version whose `UPDATE_PUBKEYS` has the current key and the next one.
2. Once enough people have updated past it, sign releases with the next key, and in a later version
   drop the old key and add a new "next" one.

A copy accepts a signature by any key in its list, so it keeps updating across the switch.

### The test key

`tests/data/test-minisign.key` / `.pub` is a throwaway pair for the tests, unencrypted on purpose.
It must never be in `UPDATE_PUBKEYS` (a test checks).

## Before you tag

`config.json`, `session.json` and `history.json` hold your Todoist token, bar PIN and history. They are
gitignored and `build.ps1` refuses to ship them, but check `git status` before committing anyway.
