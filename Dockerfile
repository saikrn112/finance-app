FROM python:3.11-slim

WORKDIR /app

# System deps + Playwright browser dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Node.js for frontend
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Frontend deps (if package.json exists)
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm install

# Install Playwright browsers
RUN cd frontend && npx playwright install chromium

# Keep container running for dev
CMD ["tail", "-f", "/dev/null"]
