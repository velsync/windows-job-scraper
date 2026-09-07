# Pending CI workflow

`ci.yml` in this directory is the complete GitHub Actions workflow for the
Python 3.12 automated test gate (ubuntu-latest + windows-latest, exact lock
install, full pytest suite, `pip check`, and a Windows pywin32 import proof).

## Why it is not active

The automation credential used for this branch (a GitHub App token) is not
permitted to create or update files under `.github/workflows/`, so the
workflow could not be pushed into place. The repository therefore has no
check runs for this branch yet.

## Activation (one manual step)

A user or credential with `workflows` permission must move this file into
place and push:

    git mv build/ci/ci.yml .github/workflows/ci.yml
    git commit -m "ci: activate Python 3.12 test gate"
    git push

After activation the workflow runs on pushes to `main`, `arena/**` and
`repair/**` branches and on pull requests targeting them.
