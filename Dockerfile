# 精成 · 演示服务
# Zeabur 的 arbitrary-git 源不跑 zbpack 自动探测,必须显式给 Dockerfile。
# zbpack.json / Procfile 保留,是给"从 GitHub 授权导入"那条路用的。
FROM python:3.11-slim

WORKDIR /app

# 依赖先装,单独一层,改代码不用重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# server.py 里 WEB_DIR 指向仓库根(index.html 所在处),静态站与 /api 同源同端口
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn server.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
