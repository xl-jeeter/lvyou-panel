FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    fastapi==0.115.6 uvicorn[standard]==0.34.0 aiohttp==3.11.11 \
    aiosqlite==0.20.0 jinja2==3.1.5 python-multipart==0.0.18

COPY app/ ./app/

VOLUME ["/data"]

EXPOSE 34567

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "34567"]
