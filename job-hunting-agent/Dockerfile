# Use an official lightweight Python image.
# 3.9-slim is used because it is already cached in the local demo machine.
ARG PYTHON_IMAGE=python:3.9-slim
FROM ${PYTHON_IMAGE}

# Prevent python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Install system dependencies including curl and git (often needed for packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file first to utilize Docker layer cache
COPY requirements.txt .

# Install python dependencies
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Playwright browsers and system dependencies (specifically for chromium headless)
RUN playwright install-deps chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy the rest of the application
COPY . .
RUN mkdir -p data/generated_resumes data/extracted_jds data/apply_form_discovery data/apply_form_answers screenshots

# Expose ports:
# 8001: MongoDB Server
# 8002: Elasticsearch Server
# 8003: Arize Phoenix Server
# 8004: Playwright Server
# 8501: Streamlit Frontend
EXPOSE 8001 8002 8003 8004 8501

# Entry point defaults to launching the Streamlit app
# Can be overridden in docker-compose.yml to run specific servers
CMD ["python", "cloudrun/run_streamlit.py"]
