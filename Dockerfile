FROM python:3.11-slim AS base

# Prevent Python from writing bytecode and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies (pandoc for LaTeX support)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Install Python dependencies first (for better layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip

# Copy project source
COPY . .

# Install the project itself
RUN pip install --no-cache-dir ".[ui]"

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

# Run Streamlit
CMD ["streamlit", "run", "streamlit_app/app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
