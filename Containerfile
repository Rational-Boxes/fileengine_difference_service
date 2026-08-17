# difference_service image. Reuses the FileEngine Python client from the sibling
# python_interface/, so build with the *parent* (monorepo) directory as context:
#   podman build -f difference_service/Containerfile -t difference-service ..
#   podman run --rm -p 8100:8100 --env-file difference_service/.env difference-service
# The event worker runs off the same image under a different command (M1).
FROM python:3.12-slim

WORKDIR /app

# Reused gRPC client FIRST (changes rarely -> better layer caching), then this
# service. The .env (credentials) is never copied.
COPY python_interface/ /app/python_interface/
COPY difference_service/pyproject.toml difference_service/README.md /app/difference_service/
COPY difference_service/src/ /app/difference_service/src/

RUN pip install --no-cache-dir /app/python_interface && \
    pip install --no-cache-dir /app/difference_service

# Bind all interfaces INSIDE the container (the host still fronts loopback per the
# monitoring convention).
ENV DIFF_HTTP_HOST=0.0.0.0 \
    DIFF_HTTP_PORT=8100
EXPOSE 8100

CMD ["difference-service"]
