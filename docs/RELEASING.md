# Releasing Busyist

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
   git commit -am "Busyist 1.0.1"
   git push
   ```

3. Tag the same version and push the tag:

   ```powershell
   git tag v1.0.1
   git push origin v1.0.1
   ```

   The build fails on purpose if the tag and `__version__` differ.

4. Watch the run on the [Actions tab](https://github.com/josedaidone/busyist/actions)
   ("Build and release", about 5–10 minutes). It checks the version, installs Inno Setup, runs
   `build.ps1` and publishes the release.

5. When it's green, the [latest release](https://github.com/josedaidone/busyist/releases/latest)
   has `Busyist-Setup-x.y.z.exe`, the portable zip and `SHA256SUMS.txt`, with notes generated from
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

## Before you tag

`config.json`, `session.json` and `history.json` hold your Todoist token, bar PIN and history. They are
gitignored and `build.ps1` refuses to ship them, but check `git status` before committing anyway.
