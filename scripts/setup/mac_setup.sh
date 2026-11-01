#!/bin/zsh
# mac_setup.sh - Automated macOS dev environment setup (idempotent, zsh-safe)
# Usage, from the repository root: ./scripts/setup/mac_setup.sh
set -euo pipefail

ARCH="$(uname -m)"

echo "==> Installing Xcode Command Line Tools (if needed)..."
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install
else
  echo "Xcode Command Line Tools already installed."
fi

if [[ "$ARCH" == "arm64" ]]; then
  echo "==> Ensuring Rosetta is installed for Apple Silicon..."
  if ! /usr/bin/pgrep oahd >/dev/null 2>&1; then
    softwareupdate --install-rosetta --agree-to-license || true
  else
    echo "Rosetta already installed."
  fi
fi

echo "==> Installing Homebrew (if needed)..."
if ! command -v brew >/dev/null 2>&1; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # nosemgrep: bash.curl.security.curl-pipe-bash.curl-pipe-bash - Official Homebrew installation script for development environment only
else
  echo "Homebrew already installed."
fi

eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"

echo "==> Installing Visual Studio Code (if needed)..."
if [ -d "/Applications/Visual Studio Code.app" ]; then
  echo "VS Code already installed at /Applications."
else
  brew install --cask visual-studio-code
fi

echo "==> Installing pyenv (if needed)..."
if ! command -v pyenv >/dev/null 2>&1; then
  brew install pyenv
else
  echo "pyenv already installed."
fi

if ! grep -q 'pyenv init' ~/.zshrc 2>/dev/null; then
  {
    echo ""
    echo "# pyenv setup"
    echo 'export PYENV_ROOT="$HOME/.pyenv"'
    echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"'
    echo 'eval "$(pyenv init -)"'
  } >> ~/.zshrc
fi
eval "$(pyenv init -)"

PYVER="3.12.5"
if ! pyenv versions --bare | grep -q "^${PYVER}$"; then
  pyenv install -s "${PYVER}"
fi
pyenv global "${PYVER}"
python -m pip install --upgrade pip
python -m pip install virtualenv

echo "==> Installing AWS CLI v2 (if needed)..."
if ! command -v aws >/dev/null 2>&1; then
  brew install awscli
else
  echo "AWS CLI already installed."
fi

echo "==> Installing AWS SAM CLI (if needed)..."
if ! command -v sam >/dev/null 2>&1; then
  brew tap aws/tap
  brew install aws-sam-cli
else
  echo "SAM CLI already installed."
fi

echo "==> Installing nvm and Node 18 (if needed)..."
if [ ! -d "$HOME/.nvm" ]; then
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash  # nosemgrep: bash.curl.security.curl-pipe-bash.curl-pipe-bash - Official NVM installation script for development environment only
fi
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
if ! nvm ls 18 >/dev/null 2>&1; then
  nvm install 18
  nvm alias default 18
else
  echo "Node 18 already installed."
fi

echo "==> Installing Docker Desktop (if needed)..."
if [ -d "/Applications/Docker.app" ]; then
  echo "Docker Desktop already installed at /Applications."
else
  brew install --cask docker || true
  echo "Please open Docker.app manually after setup."
fi

echo "==> Installing Open Interpreter (if needed)..."
if ! python -m pip show open-interpreter >/dev/null 2>&1; then
  python -m pip install --upgrade open-interpreter || true
else
  echo "open-interpreter already installed."
fi

echo "==> Configuring zsh quality-of-life settings..."
mkdir -p "$HOME/.zshrc.d"
if ! grep -q 'for f in ~/.zshrc.d/*.zsh' ~/.zshrc 2>/dev/null; then
  cat <<'EOF' >> ~/.zshrc

# Load custom zsh snippets
if [ -d "$HOME/.zshrc.d" ]; then
  for f in $HOME/.zshrc.d/*.zsh; do
    [ -r "$f" ] && source "$f"
  done
fi
EOF
fi

cat <<'EOF' > "$HOME/.zshrc.d/00-preferences.zsh"
# History settings
setopt APPEND_HISTORY
HISTSIZE=100000
SAVEHIST=100000

# Simple prompt
PROMPT='%F{green}%n@%m%f:%F{blue}%~%f$(git_prompt_info) $ '

# Aliases
alias python=python3
alias pip=pip3

# nvm autoload
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
EOF

echo "==> Done. Open a new Terminal window to load updated shell settings."
