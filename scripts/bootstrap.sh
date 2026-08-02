#!/usr/bin/env bash
# jiucai-helper 首次运行入口：无需预先安装 Python 即可执行环境检查。
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SKILL_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUNTIME_DIR=${JIUCAI_RUNTIME_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/jiucai-helper"}
VENV_DIR="$RUNTIME_DIR/venv"
REQ_FILE="$SCRIPT_DIR/requirements-free.txt"

CHECK_ONLY=0
ASSUME_YES=0
WITH_FUTU=0
LAUNCH=0

usage() {
  printf '%s\n' \
    '用法：bootstrap.sh [--check] [--yes] [--with-futu] [--launch]' \
    '  --check       只检查 Python、免费行情组件与可选 Futu 组件' \
    '  --yes         安装免费行情组件，不再二次确认' \
    '  --with-futu   同时安装 futu-api；仍需用户自行安装并登录 OpenD' \
    '  --launch      使用检测到的运行环境启动本地观测台'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --with-futu) WITH_FUTU=1 ;;
    --launch) LAUNCH=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

find_python() {
  if [ "${JIUCAI_PYTHON+x}" = x ]; then
    if [ -x "$JIUCAI_PYTHON" ]; then
      printf '%s\n' "$JIUCAI_PYTHON"
      return 0
    fi
    return 1
  fi
  for candidate in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

python_missing() {
  printf '%s\n' '未检测到 Python 3.9 或更高版本。jiucai-helper 需要 Python 才能启动本地观测台。' >&2
  case "$(uname -s 2>/dev/null || printf unknown)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        printf '%s\n' 'macOS 可在用户确认后运行：brew install python' >&2
      else
        printf '%s\n' '请从 https://www.python.org/downloads/macos/ 安装 Python 3，安装后重新运行本脚本。' >&2
      fi
      ;;
    Linux)
      printf '%s\n' 'Linux 请使用系统包管理器安装 python3、python3-venv 与 python3-pip，然后重新运行本脚本。' >&2
      ;;
    *)
      printf '%s\n' '请从 https://www.python.org/downloads/ 安装 Python 3，安装后重新运行本脚本。' >&2
      ;;
  esac
  printf '%s\n' '安装 Python 会修改系统环境，必须由用户明确确认后执行。' >&2
  exit 3
}

SYSTEM_PY=$(find_python || true)
[ -n "$SYSTEM_PY" ] || python_missing

if ! "$SYSTEM_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  printf '检测到的 Python 版本过低：%s；需要 Python 3.9 或更高版本。\n' "$SYSTEM_PY" >&2
  exit 3
fi

if [ -x "$VENV_DIR/bin/python" ]; then
  ACTIVE_PY="$VENV_DIR/bin/python"
else
  ACTIVE_PY="$SYSTEM_PY"
fi

has_free_deps() {
  "$ACTIVE_PY" -c 'import akshare, baostock' >/dev/null 2>&1
}

has_futu() {
  "$ACTIVE_PY" -c 'import futu' >/dev/null 2>&1
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  printf 'Python：可用（%s）\n' "$SYSTEM_PY"
  if has_free_deps; then
    printf '免费 A 股行情组件：可用（akshare、baostock）\n'
  else
    printf '免费 A 股行情组件：未安装\n'
  fi
  if has_futu; then
    printf 'Futu Python 组件：已安装；仍需检测 OpenD 连接与行情权限\n'
  else
    printf 'Futu Python 组件：未安装（可选）\n'
  fi
  exit 0
fi

if [ "$LAUNCH" -eq 1 ]; then
  exec "$ACTIVE_PY" "$SKILL_DIR/webapp/server.py"
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  if [ -t 0 ]; then
    printf '%s' '将在 jiucai-helper 的独立虚拟环境中安装免费行情组件，不修改系统 Python。继续吗？[y/N] '
    read -r answer
    case "$answer" in y|Y|yes|YES) ;; *) printf '%s\n' '已取消安装。'; exit 0 ;; esac
  else
    printf '%s\n' '安装需要用户确认。确认后重新运行：bash scripts/bootstrap.sh --yes' >&2
    exit 4
  fi
fi

mkdir -p "$RUNTIME_DIR"
"$SYSTEM_PY" -m venv "$VENV_DIR"
ACTIVE_PY="$VENV_DIR/bin/python"
"$ACTIVE_PY" -m pip install --upgrade pip
"$ACTIVE_PY" -m pip install --requirement "$REQ_FILE"
if [ "$WITH_FUTU" -eq 1 ]; then
  "$ACTIVE_PY" -m pip install futu-api
fi

printf '运行环境已安装：%s\n' "$VENV_DIR"
if "$ACTIVE_PY" "$SCRIPT_DIR/fallback_quote.py" snapshot SH.600519; then
  printf '%s\n' '免费 A 股行情验证完成。可运行 bash scripts/bootstrap.sh --launch 启动观测台。'
else
  printf '%s\n' '依赖已经安装，但免费行情连通测试失败。请检查网络后重试；不得把本次状态宣布为行情可用。' >&2
  exit 5
fi
