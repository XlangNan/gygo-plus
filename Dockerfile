FROM python:3.11-alpine

LABEL org.opencontainers.image.title="gygo"
LABEL org.opencontainers.image.description="光鸭云盘分享链接追更监控：丢一个分享链接进去，出新集自动转存"

WORKDIR /app

# 只拷贝运行需要的文件，主流程零第三方依赖，无需 pip install。
# 唯一的例外是"订阅追更"的文件名识别（guessit / anitopy），见下面的 pip install。
COPY guangya.py share_gy.py monitor.py monitor_store.py gygo_log.py app.py \
     dingtalk.py smartstrm.py subscription.py tmdb.py episode_parse.py \
     selftest.py index.html ./
COPY tests/ ./tests/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# guessit / anitopy：从分享链接里那些命名很乱的文件名（尤其是动漫/字幕组压制）
# 猜集数，比自己写正则准得多。都是纯 Python、没有 C 扩展，alpine 装起来很快；
# 装不上也不影响整体运行 —— episode_parse.py 会自动退回到内置的正则规则。
RUN pip install --no-cache-dir guessit anitopy || true

ENV GYGO_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

VOLUME ["/data"]
EXPOSE 5099

# 容器自带健康检查：WEB 服务挂了 docker ps 上能直接看到 unhealthy
HEALTHCHECK --interval=60s --timeout=8s --start-period=15s --retries=3 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+(os.environ.get('PORT') or '5099')+'/api/health',timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "app.py"]
