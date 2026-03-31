FROM python:3.11-slim AS base

# Prevent Python from writing bytecode and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies (tini for PID 1, pandoc pinned to >=3.1.6 for CVE-2023-38745)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tini \
    && curl -L -o /tmp/pandoc.deb \
        https://github.com/jgm/pandoc/releases/download/3.6.4/pandoc-3.6.4-1-amd64.deb \
    && dpkg -i /tmp/pandoc.deb \
    && rm -f /tmp/pandoc.deb \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Copy pyproject.toml and README.md first so pip can resolve dependency
# metadata early, improving Docker layer caching for dependency downloads.
COPY pyproject.toml README.md ./

# Copy full project source BEFORE pip install so hatchling can find the
# source tree during the editable/wheel build.
COPY . .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[ui]"

# Create temp directories and set permissions
RUN mkdir -p /app/temp_uploads /app/logs \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose Streamlit port
EXPOSE 8501

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Use tini as PID 1 for proper signal handling and zombie reaping
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run Streamlit
CMD ["streamlit", "run", "streamlit_app/app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
