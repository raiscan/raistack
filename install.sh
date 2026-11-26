#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
force=false
if [ "${1-}" = --force ]; then force=true; shift; fi
[ "$#" -eq 0 ] || { echo 'Usage: install.sh [--force]' >&2; exit 2; }
skills=${AGENT_SKILLS_HOME:-"$HOME/.agents/skills"}
claude_skills=${CLAUDE_SKILLS_HOME:-"$HOME/.claude/skills"}
bin=${RAISTACK_BIN_HOME:-"$HOME/.local/bin"}
cow=${COWTREE_HOME:-"$HOME/.local/share/cowtree"}
mkdir -p "$skills" "$claude_skills" "$bin" "$(dirname "$cow")"
link() {
  src=$1 dest=$2
  if [ -L "$dest" ]; then
    if [ "$(readlink "$dest")" = "$src" ]; then echo "Already linked: $dest"; return; fi
    rm "$dest"
  elif [ -e "$dest" ]; then
    if [ "$force" != true ]; then echo "Refusing to replace $dest (use --force)" >&2; return 1; fi
    rm -rf -- "$dest"
  fi
  ln -s -- "$src" "$dest"
  echo "Linked $dest -> $src"
}
if [ ! -d "$cow/.git" ]; then
  [ ! -e "$cow" ] || { echo "Cowtree destination exists: $cow" >&2; exit 1; }
  git clone https://github.com/raiscan/cowtree.git "$cow"
  echo "Installed cowtree at $cow"
else
  echo "Using cowtree at $cow"
fi
for name in agent-run temporary-agents cowtree cow-git-worktree; do
  link "$root/skills/$name" "$skills/$name"
  link "$root/skills/$name" "$claude_skills/$name"
done
link "$root/tools/agent-run/agent-run" "$bin/agent-run"
link "$cow/bin/git-cowtree" "$bin/git-cowtree"
