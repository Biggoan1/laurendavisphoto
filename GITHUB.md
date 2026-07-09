# GitHub — how this repo mirrors to github.com/Biggoan1/laurendavisphoto

`origin` is the GitHub mirror. The live site runs from this checkout inside the
`laurendavisphoto` LXC (10.100.0.113); GitHub is a backup + collaboration copy.

## Auth
A dedicated **SSH deploy key** in the container (`/root/.ssh/id_ed25519`) is
registered on the repo (Settings → Deploy keys, **Allow write access**). GitHub
is reached over SSH on **port 443** (22 is unreliable on this network) via
`~/.ssh/config`:

    Host github.com
        HostName ssh.github.com
        Port 443
        User git

## Flow
- The AI feature-coder commits approved work to `main` in a worktree, merges it,
  and `git push origin main` mirrors it to GitHub (best-effort; a push failure
  never rolls back the local merge).
- Humans: `git push origin main` after committing.
- The container's checkout is the source of truth for what's deployed.
