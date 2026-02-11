# FoXYiZ Docker Image
# This Dockerfile sets up FoXYiZ test automation framework with Chrome and ChromeDriver

FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    FOXYIZ_HEADLESS=false \
    DISPLAY=:99

# Install system dependencies and Chrome
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    xvfb \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgcc1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome (using modern GPG key method)
RUN mkdir -p /etc/apt/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Install ChromeDriver
# Download and install ChromeDriver that matches Chrome version
RUN CHROME_VERSION=$(google-chrome --version | sed 's/.* \([0-9.]*\).*/\1/' | cut -d. -f1) && \
    echo "Chrome major version: $CHROME_VERSION" && \
    CHROMEDRIVER_VERSION=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION}" 2>/dev/null || \
        curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION}" 2>/dev/null || \
        echo "120.0.6099.109") && \
    echo "Installing ChromeDriver version: $CHROMEDRIVER_VERSION" && \
    (wget -q --no-check-certificate "https://storage.googleapis.com/chrome-for-testing-public/${CHROMEDRIVER_VERSION}/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip 2>/dev/null || \
     wget -q --no-check-certificate "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip" -O /tmp/chromedriver.zip 2>/dev/null || \
     wget -q "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/${CHROMEDRIVER_VERSION}/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip 2>/dev/null) && \
    unzip -q /tmp/chromedriver.zip -d /tmp && \
    (mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver 2>/dev/null || \
     mv /tmp/chromedriver /usr/local/bin/chromedriver) && \
    chmod +x /usr/local/bin/chromedriver && \
    rm -rf /tmp/chromedriver* && \
    chromedriver --version || echo "ChromeDriver installed (version check may fail, but driver is available)"

# Install webdriver-manager as backup option
RUN pip install --no-cache-dir webdriver-manager

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY fEngine.py .
COPY fStart.json .
COPY x/ ./x/
COPY y/ ./y/
COPY y_val/ ./y_val/
COPY z/ ./z/

# Create z directory if it doesn't exist (for output)
RUN mkdir -p z

# Set Python path
ENV PYTHONPATH=/app

# Default command - can be overridden
CMD ["python", "fEngine.py", "--config", "fStart.json"]

