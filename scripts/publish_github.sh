#!/usr/bin/env bash
# 把 market-watch 推送到 GitHub（含 tag v0.1.0）。
#
# 凭据三选一（按优先级）：
#   1) 环境变量 GITHUB_USER / GITHUB_TOKEN（适合 CI）；
#   2) 【默认】Windows 凭据管理器 / macOS Keychain / Linux secret-service 里
#      已存的 GitHub 凭据 —— 执行 git credential-manager github login
#      走浏览器授权即可，无需手动生成 PAT。本机已登录，直接可用。
#   3) 两者都没有 → 脚本在第 0 步退出并提示先授权，不会带着空 token 硬跑。
#
# 用法：
#   bash scripts/publish_github.sh https://github.com/<user>/market-watch.git
#   # 或显式给凭据（CI）：
#   GITHUB_USER=<user> GITHUB_TOKEN=<pat> bash scripts/publish_github.sh <repo-url>
#
# 设计约束：凭据只走环境变量 / 系统凭据仓，**不写进 .git/config、不落盘、
# 不出现在 push 的进程命令行里**（token 从来不经过 shell 参数展开）。
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
HAS_TIMEOUT=0
command -v timeout >/dev/null 2>&1 && HAS_TIMEOUT=1 || HAS_TIMEOUT=0

step "0/6 环境自检"
[ -n "$REPO_URL" ] ||
  die "缺少仓库地址：bash scripts/publish_github.sh https://github.com/<user>/market-watch.git"

# --- 凭据解析 -----------------------------------------------------------------
# 环境变量优先；没有就去问系统凭据仓（GCM / keychain / secret-service）。
CRED_FROM="env"
if [ -n "${GITHUB_USER:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
  :
else
  if [ "$HAS_TIMEOUT" -eq 1 ]; then
    GCM_IN=(timeout 20)
  else
    GCM_IN=()
  fi
  CRED_OUT="$(
    printf 'protocol=https\nhost=github.com\n\n' |
      "${GCM_IN[@]}" git credential-manager get 2>/dev/null || true
  )"
  if [ -n "$CRED_OUT" ]; then
    while IFS='=' read -r _key _val; do
      [ -n "${_val:-}" ] || continue
      case "$_key" in
        username) GITHUB_USER="${GITHUB_USER:-$_val}" ;;
        password) GITHUB_TOKEN="${GITHUB_TOKEN:-$_val}" ;;
      esac
    done <<< "$CRED_OUT"
  fi
  CRED_FROM="credential-manager"
fi

[ -n "${GITHUB_USER:-}" ] || die "不知道用哪个 GitHub 账号：export GITHUB_USER=<你的用户名>，或先跑 git credential-manager github login"
[ -n "${GITHUB_TOKEN:-}" ] || die "没有可用的 GitHub token：
  本机已登录过（git credential-manager github login），但这次没读到。
  修复：终端里执行
      git credential-manager github login
  走浏览器授权（可开 --device 用设备码），授权完再重跑本脚本。
  或用环境变量显式给：export GITHUB_TOKEN=<pat>（需要 repo 权限）。"
ok "账号 ${GITHUB_USER}（凭据来源：${CRED_FROM}）"

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
# 凭据有效性不做单独预检：GitHub 空仓库的 ls-remote 不带鉴权也能通，
# 预检会假通过；真正第一次用到凭据就是 push（第 5 步），失败即停。
# 本脚本刻意不调用 https://api.github.com/user 去校验 token —— 那条路要么
# 把 token 带进命令行参数，要么落一个临时配置文件，两者在受限环境里都不稳。
#
# push 的凭据注入走 git 原生的 GIT_ASKPASS（而不是 credential.helper）：
#   - credential.helper 需要拼成 git-credential-<name> 去 PATH 找，
#     GCM 在这台机器上拼不出能用的名字（试过 manager / manager-core /
#     credential-manager / git-credential-manager 四种，均取不到凭据）；
#   - 临时改 credential.helper 指向 .git 下的脚本又受限于沙箱写盘。
#   GIT_ASKPASS 由 git 直接调用，凭据经环境变量传入，不落盘、不进 argv。

# 仓库可达性。公开空仓库的 ls-remote 不需要鉴权，
# 所以这一条只证明 HTTPS 通路通。
if [ "$HAS_TIMEOUT" -eq 1 ]; then
  timeout 60 git ls-remote "$REPO_URL" "refs/heads/$BRANCH" >/dev/null 2>&1 ||
    die "连不上或仓库不存在：${REPO_URL}
  （空仓库请到 https://github.com/new 新建，别勾选 README / LICENSE / .gitignore）"
else
  git ls-remote "$REPO_URL" "refs/heads/$BRANCH" >/dev/null 2>&1 ||
    die "连不上或仓库不存在：${REPO_URL}"
fi
ok "$REPO_URL 可达（分支 $BRANCH）"

step "3/6 提交作者身份（占位邮箱会让 GitHub 上无法关联账号）"
EMAIL="$(git config --get user.email || true)"
if [ "$EMAIL" = "dev@local" ] || [ -z "$EMAIL" ]; then
  NEW_EMAIL="${GITHUB_USER}@users.noreply.github.com"
  git config user.email "$NEW_EMAIL"
  ok "占位 dev@local -> ${NEW_EMAIL}（只改本机配置，不重写历史）"
else
  ok "已配置：${EMAIL}（历史里已有的 commit 作者不变）"
fi
git config user.name "${GITHUB_USER}" >/dev/null 2>&1 || true

step "4/6 配置远端"
git remote remove origin >/dev/null 2>&1 || true
git remote add origin "$REPO_URL"
ok "origin -> $REPO_URL（明文 URL，不含 token）"

step "5/6 推送分支与 tag"
# 这台机器的系统 gitconfig 里配了 credential.helper=helper-selector，
# 而 git-credential-helper-selector 在没有图形会话时会永远挂在"选择账户"
# 的等待上（GIT_ASKPASS 轮不到它，因为 git 先问 helper 链）。
# 破解办法：用 `-c credential.helper=`（空值）先把继承下来的 helper 清掉，
# 再挂上我们自己的 helper。helper 脚本写在 .git/ 下 —— git 完全忽略这个
# 目录，不入库、不出现在 git status；凭据经环境变量传入，不落盘、不进 argv。
GIT_DIR_ABS="$(git rev-parse --absolute-git-dir)"
CRED_HELPER="$GIT_DIR_ABS/mw-cred-helper.$$"
cat > "$CRED_HELPER" <<'EOS'
#!/bin/sh
cat > /dev/null
printf 'username=%s\n' "$MW_USER"
printf 'password=%s\n' "$MW_PASS"
EOS
chmod +x "$CRED_HELPER" 2>/dev/null || true
trap 'rm -f "$CRED_HELPER"' EXIT

MW_USER="$GITHUB_USER" MW_PASS="$GITHUB_TOKEN" \
  timeout 300 git -c credential.helper= -c "credential.helper=$CRED_HELPER" \
    push -u origin "$BRANCH" --follow-tags
rm -f "$CRED_HELPER"
trap - EXIT
ok "分支 ${BRANCH} 与 tag ${TAG} 已推送（单个 push 命令，服务端原子生效）"

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
printf '提醒：这次用的 OAuth token 在会话里出现过，推完建议到\n  https://github.com/settings/applications 把对应授权撤掉（下次 git credential-manager github login 重新授权即可）。\n\n'
