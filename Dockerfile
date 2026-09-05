FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /app/data
VOLUME ["/app/data"]
ENTRYPOINT ["apebot"]
CMD ["run", "-c", "/app/config.yaml"]
