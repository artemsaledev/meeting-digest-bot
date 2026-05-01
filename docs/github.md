# GitHub Repository Setup

Recommended repository strategy: keep `MeetingDigestBot` as a separate repository.

Reason:

- it is a standalone runtime service
- it has its own deployment, env, systemd units, and state DB
- it integrates with `AIcallorder`, but should not be hidden as an AIcallorder branch
- it can evolve independently and be deployed/rolled back independently

Suggested repository name:

```text
meeting-digest-bot
```

## Create Remote With GitHub CLI

Install and authenticate GitHub CLI:

```powershell
winget install GitHub.cli
gh auth login
```

Create a private repo and push:

```powershell
cd "C:\Users\artem\Downloads\dev-scripts\6. Task Manager"
gh repo create meeting-digest-bot --private --source . --remote origin --push
```

## Or Create Repo Manually

1. Create a private repo on GitHub named `meeting-digest-bot`.
2. Add it as remote:

```powershell
git remote add origin https://github.com/artemsaledev/meeting-digest-bot.git
git branch -M main
git push -u origin main
```

Do not commit `.env`, `ssh.txt`, SQLite DB files, logs, or release ZIP archives.

