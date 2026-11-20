#!/bin/bash
# 用法: SSH_HOST=你的服务器别名或 user@host bash scripts/set_llm_key.sh
: "${SSH_HOST:?请先设置 SSH_HOST,例如 SSH_HOST=my-vps}"
# 把 DeepSeek 密钥安全存到服务器(/etc/llm-proxy/env,权限 600),并生成"访问密码"
# 访问密码会自动复制到你 Mac 的剪贴板(不在屏幕上显示),等一下直接粘贴到 TablePlus 的 API Key 一栏
# DeepSeek 密钥:输入时不显示字符,不写进任何脚本或命令行
if ssh "$SSH_HOST" 'test -e /etc/llm-proxy/env'; then
  echo "服务器上已经有 /etc/llm-proxy/env,为避免覆盖已取消。请先手动检查再决定是否覆盖。"; exit 1
fi
echo "请粘贴你的 DeepSeek API 密钥(sk- 开头;输入时不显示字符),然后回车"
read -r -s -p "密钥: " KEY; echo
case "$KEY" in sk-*) ;; *) echo "密钥应该以 sk- 开头,已取消,什么都没改"; exit 1;; esac
case "$KEY" in *\'*|*\"*|*\\*|*" "*|*\$*|*\#*) echo "密钥里含有特殊字符,已取消"; exit 1;; esac

# 密钥通过标准输入传给服务器;访问密码在服务器上随机生成
TOKEN=$(printf '%s\n' "$KEY" | ssh "$SSH_HOST" '
  set -e
  umask 077
  mkdir -p /etc/llm-proxy
  read -r K
  T=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d " \n")
  printf "DEEPSEEK_API_KEY=%s\nPROXY_TOKEN=%s\n" "$K" "$T" > /etc/llm-proxy/env
  chmod 600 /etc/llm-proxy/env
  printf "%s" "$T"
')
unset KEY
if [ -z "$TOKEN" ]; then echo "写入失败,请告诉 Claude"; exit 1; fi
printf '%s' "$TOKEN" | pbcopy
unset TOKEN
echo "---- 检查 ----"
ssh "$SSH_HOST" 'ls -l /etc/llm-proxy/env | awk "{print \$1, \$3, \$NF}"; grep -c "^DEEPSEEK_API_KEY=sk-" /etc/llm-proxy/env'
echo "完成。访问密码已复制到剪贴板(现在 Cmd+V 就是它)。先别复制别的东西,等 Claude 说可以再粘贴到 TablePlus。"
