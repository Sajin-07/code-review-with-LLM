#!/bin/bash
set -e
cd "$(dirname "$0")/.."

REPOS_DIR="repos"
mkdir -p "$REPOS_DIR"

echo "→ Cloning spring-petclinic-rest..."
if [ -d "$REPOS_DIR/spring-petclinic-rest" ]; then
  echo "  Already exists, pulling latest..."
  cd "$REPOS_DIR/spring-petclinic-rest" && git pull && cd ../..
else
  git clone --depth 1 https://github.com/spring-petclinic/spring-petclinic-rest.git "$REPOS_DIR/spring-petclinic-rest"
fi

echo "→ Cloning spring-petclinic-angular..."
if [ -d "$REPOS_DIR/spring-petclinic-angular" ]; then
  echo "  Already exists, pulling latest..."
  cd "$REPOS_DIR/spring-petclinic-angular" && git pull && cd ../..
else
  git clone --depth 1 https://github.com/spring-petclinic/spring-petclinic-angular.git "$REPOS_DIR/spring-petclinic-angular"
fi

echo "✓ Repos ready in $REPOS_DIR/"
