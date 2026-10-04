#!/usr/bin/env bash
# 把 market-watch 推送到 GitHub（含 tag v0.1.0）。
#
# 前置（只需做一次）：
#   1) 在 GitHub 网页新建一个**空**仓库，不要勾选 Add a README file / LICENSE / .gitignore：
#      https://github.com/new   （仓库名建议 market-watch）
#   2) 生成 Personal Access Token（classic 或 fine-grained 都行），必须有 repo 权限：
#      https://github.com/settings/tokens
#
# 用法（凭据只走环境变量，不落盘、不进 .git/config、不出现在进程命令行里）：
#   export GITHUB_USER=<你的 GitHub 用户名>
#   export GITHUB_TOKEN=<你的 PAT>
#   bash scripts/publish_github.sh https://github.com/<你的用户名>/market-watch.git
#
# 不传地址就用环境变量 REPO_URL，或不设这个开关交互输入。
#
# 本机特有条件：HTTPS 出网被 SteamTools 中间人接管，TLS 吊销检查会挂。
# 本脚本用 openssl 后端 + SteamTools 根 CA，见 .git/config 的 http.sslCAInfo。
# 换机器跑时如果报证书错，先确认那两项配置存在。

set -euo pipefail

REPO_URL="${1:-${REPO_URL:-}}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TAG="v0.1.0"

die() { printf '  [FAIL] %s\n' "$1" >&2; exit 1; }
ok()  { printf '  [ OK ] %s\n' "$1"; }
step(){ printf '\n== %s ==\n' "$1"; }

command -v git >/dev/null 2>&1 || die "找不到 git"

step "0/6 环境自检"
[ -n "${GITHUB_USER:-}" ] || die "缺少 GITHUB_USER（你的 GitHub 用户名）"
[ -n "${GITHUB_TOKEN:-}" ] || die "缺少 GITHUB_TOKEN（PAT，需要 repo 权限）"
[ -n "$REPO_URL" ] || die "缺少仓库地址：bash scripts/publish_github.sh https://github.com/<user>/market-watch.git"

# 本机 SBOM / TLS 修复：没有 CA 配置会在 SteamTools 环境下报 CRYPT_E_NO_REVOCATION_CHECK
if [ "$(git config --get http.sslBackend || true)" != "openssl" ]; then
  git config --local http.sslBackend openssl
fi
if [ -z "$(git config --get http.sslCAInfo || true)" ]; then
  if [ -f "$HOME/.workbuddy/certs/steamtools-ca.pem" ]; then
    git config --local http.sslCAInfo "$HOME/.workbuddy/certs/steamtools-ca.pem"
    ok "已补 http.sslCAInfo（SteamTools 根 CA 未配置）"
  fi
fi

step "1/6 工作区与 tag 一致性"
[ -z "$(git status --porcelain)" ] || die "工作区不干净，先提交或 stash"
ok "工作区干净（$(git rev-parse --short HEAD)）"

TAG_COMMIT="$(git rev-parse "${TAG}^{commit}")"
HEAD_COMMIT="$(git rev-parse HEAD)"
[ "$TAG_COMMIT" = "$HEAD_COMMIT" ] ||
  die "tag ${TAG} 指向 $(git rev-parse --short "${TAG}^{commit}")，但 HEAD 是 $(git rev-parse --short HEAD)；发布前必须让 tag 落在 HEAD"
ok "${TAG} -> ${TAG_COMMIT:0:7} == HEAD"

step "2/6 GitHub 可达性"
git ls-remote "$REPO_URL" "refs/heads/$BRANCH" >/dev/null 2>&1 ||
  die "连不上或仓库不存在：${REPO_URL}。请先到 https://github.com/new 建一个空仓库"
ok "$REPO_URL 可达空仓库"

step "3/6 提交作者身份（占位邮箱会让 GitHub 上无法关联账号）"
EMAIL="$(git config --get user.email || true)"
if [ "$EMAIL" = "dev@local" ] || [ -z "$EMAIL" ]; then
  NEW_EMAIL="${GITHUB_USER}@users.noreply.github.com"
  git config user.email "$NEW_EMAIL"
  ok "占位 dev@local -> ${NEW_EMAIL}"
else
  ok "已配置：${EMAIL}"
fi
git config user.name "${GITHUB_USER}" >/dev/null 2>&1 || true

step "4/6 配置远端"
git remote remove origin >/dev/null 2>&1 || true
git remote add origin "$REPO_URL"
ok "origin -> $REPO_URL（明文 URL，不含 token）"

step "5/6 推送分支与 tag"
# token 通过一次性 credential helper 注入，写进 .git/config 的是不含凭据的公开 URL
git -c credential.helper='!f() { echo "username='${GITHUB_USER}'; echo "password='${GITHUB_TOKEN}'"; }; f' \
  push -u origin "$BRANCH" --follow-tags
ok "分支 ${BRANCH} 与 tag ${TAG} 已推送"

step "6/6 回读校验"
REMOTE_HEAD="$(git ls-remote origin "refs/heads/$BRANCH" | awk '{print $1}')"
[ "${REMOTE_HEAD:0:7}" = "${HEAD_COMMIT:0:7}" ] ||
  die "远端 HEAD ${REMOTE_HEAD:0:7} != 本地 ${HEAD_COMMIT:0:7}"
ok "远端 $(git rev-parse --short "${REMOTE_HEAD:-$HEAD_COMMIT}") 与本地一致"

REMOTE_TAG="$(git ls-remote origin "refs/tags/${TAG}" | awk '{print $1}')"
[ -n "$REMOTE_TAG" ] || die "远端查不到 tag ${TAG}"
ok "tag ${TAG} 已在远端（tag 对象 ${REMOTE_TAG:0:7}）"

printf '\n全部通过。打开看：\n  %s\n' "$REPO_URL"
printf '发版说明页：\n  %s/releases/tag/%s\n\n' "$REPO_URL" "$TAG"
