#!/usr/bin/env bash
# Steps 3-11 of the team "Raspberry Pi Setup" guide, for one user on the shared Pi.
# Usage: bash scripts/setup_pi_user.sh <username> "Your Name" <github-email>
# Safe to re-run. Still manual: passwd, and adding the printed SSH key on GitHub.
set -euo pipefail
user=$1 name=$2 email=$3
repo=/opt/path/PATH-Imaging worktree=/opt/path/worktrees/$1

git config --global user.name "$name"
git config --global user.email "$email"
# The shared repo is owned by another user; git refuses it until trusted
git config --global --get-all safe.directory | grep -qx "$repo" || git config --global --add safe.directory "$repo"

[[ -f ~/.ssh/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "$email"

grep -qx 'umask 002' ~/.bashrc || echo 'umask 002' >> ~/.bashrc
grep -qx 'export UV_CACHE_DIR=/opt/path/uv-cache' ~/.bashrc || echo 'export UV_CACHE_DIR=/opt/path/uv-cache' >> ~/.bashrc
umask 002
export UV_CACHE_DIR=/opt/path/uv-cache

[[ -d $worktree ]] || git -C "$repo" worktree add "$worktree" -b "$user-dev"
cd "$worktree"
uv venv --system-site-packages --allow-existing -q  # camera_API.py's window needs the system GTK/GStreamer
uv sync || echo "uv sync failed. If it says 'Permission denied' in /opt/path/uv-cache, run: sudo chmod -R g+w /opt/path/uv-cache"

echo
echo "Add this key on GitHub (Settings -> SSH and GPG keys -> New SSH key), then test with: ssh -T git@github.com"
cat ~/.ssh/id_ed25519.pub
echo "Worktree: $worktree (branch $user-dev). First push: git push -u origin $user-dev"
