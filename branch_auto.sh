#!/usr/bin/env bash
set -e

# ===== 参数 =====
CURRENT_DATE=$1   # 本次迭代起始，如 20251201
NEXT_DATE=$2      # 下一迭代起始，如 20251215

if [[ -z "$CURRENT_DATE" || -z "$NEXT_DATE" ]]; then
  echo "Usage: iteration.sh <CURRENT_DATE> <NEXT_DATE>"
  exit 1
fi

CURRENT_DEV="${CURRENT_DATE}-dev"
RELEASE_BRANCH="release/${CURRENT_DATE}"
NEXT_DEV="${NEXT_DATE}-dev"

echo "=== Iteration Close Start ==="
echo "CURRENT_DEV=$CURRENT_DEV"
echo "RELEASE_BRANCH=$RELEASE_BRANCH"
echo "NEXT_DEV=$NEXT_DEV"

git config user.email "ci@gitlab.com"
git config user.name "GitLab CI"

git fetch origin

# 1. 校验当前迭代分支存在
if ! git show-ref --verify --quiet "refs/remotes/origin/${CURRENT_DEV}"; then
  echo "ERROR: ${CURRENT_DEV} not exists"
  exit 1
fi

# 2. 创建 release 分支（基于当前迭代）
if git show-ref --verify --quiet "refs/remotes/origin/${RELEASE_BRANCH}"; then
  echo "Release branch already exists, skip"
else
  git checkout -B "${RELEASE_BRANCH}" "origin/${CURRENT_DEV}"
  git push origin "${RELEASE_BRANCH}"
fi

# 3. 创建下一迭代分支（基于 dev）
if git show-ref --verify --quiet "refs/remotes/origin/${NEXT_DEV}"; then
  echo "Next dev branch already exists, skip"
else
  git checkout -B "${NEXT_DEV}" "origin/${CURRENT_DEV}"
  git push origin "${NEXT_DEV}"
fi

echo "=== Iteration Close Done ==="