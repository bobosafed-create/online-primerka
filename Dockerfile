FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Prefer binary wheels for compiled packages. YooKassa is distributed as a
# pure-Python source package, so it can still install without a C compiler.
# Avoid apt-get so a Debian mirror DNS outage cannot block the application build.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --prefer-binary -r requirements.lock

COPY . ./

# The application writes transient data only under /tmp and does not need root.
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).read()" || exit 1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
