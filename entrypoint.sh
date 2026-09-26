#!/bin/sh
# gygo 容器启动脚本：把 /data/hosts 里的自定义域名映射，套用到容器自己的 /etc/hosts。
#
# 用途：不少环境直连 TMDB（api.themoviedb.org / image.tmdb.org）很慢或连不上，
# 需要自己钉 hosts 才能用。/data 这个目录本来就是 docker-compose 里挂载到宿主机
# 的那个持久化目录（跟 monitors.json 同级），所以这个 hosts 文件也在宿主机上，
# 直接在宿主机编辑、`docker compose restart` 一下就生效，不用进容器、不用重新
# 构建镜像。

DATA_DIR="${GYGO_DATA_DIR:-/data}"
HOSTS_FILE="$DATA_DIR/hosts"
MARK_START="# --- gygo custom hosts start（本段由容器启动脚本自动维护，别手改这两行标记）---"
MARK_END="# --- gygo custom hosts end ---"

mkdir -p "$DATA_DIR"

if [ ! -f "$HOSTS_FILE" ]; then
  cat > "$HOSTS_FILE" <<'EOF'
# gygo 自定义 hosts 映射文件。
# 每行一条：<IP地址> <域名>，跟系统 /etc/hosts 格式一样；# 开头的整行是注释，会被跳过。
#
# 最常见的用途是 TMDB 连不上（国内直连经常很慢/连不上），需要自己钉一下 IP：
#   1) 找一个当前可用的 IP（可以用 https://www.itdog.cn/ping/ 之类的工具，
#      查 api.themoviedb.org 和 image.tmdb.org 从你的网络环境连哪个 IP 比较通畅）
#   2) 把下面两行前面的 # 去掉，IP 换成你查到的
#   3) 保存这个文件，重启容器（docker compose restart gygo）就生效，不用重新构建镜像
#
# 104.244.42.65 api.themoviedb.org
# 104.244.42.65 image.tmdb.org
EOF
  echo "[entrypoint] 已生成 hosts 模板：$HOSTS_FILE（默认全部注释，没有实际生效的映射，按文件里的说明改）"
fi

# 幂等：每次启动先删掉上次注入到 /etc/hosts 里的旧内容，再按当前文件重新注入一遍，
# 这样反复重启、或者你编辑了 hosts 文件后重启，都不会在 /etc/hosts 里越堆越多。
if [ -f /etc/hosts ]; then
  awk -v s="$MARK_START" -v e="$MARK_END" '
    $0==s {skip=1; next}
    $0==e {skip=0; next}
    !skip {print}
  ' /etc/hosts > /etc/hosts.new 2>/dev/null && mv /etc/hosts.new /etc/hosts
fi

{
  echo "$MARK_START"
  grep -Ev '^[[:space:]]*(#|$)' "$HOSTS_FILE" 2>/dev/null
  echo "$MARK_END"
} >> /etc/hosts

APPLIED=$(grep -Ev '^[[:space:]]*(#|$)' "$HOSTS_FILE" 2>/dev/null | wc -l | tr -d ' ')
echo "[entrypoint] 已应用自定义 hosts：$HOSTS_FILE 里生效的映射 $APPLIED 条"

exec "$@"
