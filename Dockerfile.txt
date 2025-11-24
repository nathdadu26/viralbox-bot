FROM python:3.11-slim

# Install aria2 and other dependencies
RUN apt-get update && apt-get install -y \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY terabot.py .

# Create download directory
RUN mkdir -p /tmp/aria2_downloads

# Create startup script that uses environment variables
RUN echo '#!/bin/bash\n\
aria2c --enable-rpc --rpc-listen-all --rpc-secret=${ARIA2_SECRET:-mysecret} \
--dir=${DOWNLOAD_DIR:-/tmp/aria2_downloads} --continue=true \
--max-concurrent-downloads=5 --max-connection-per-server=10 \
--split=10 --daemon=true\n\
sleep 2\n\
python terabot.py' > /app/start.sh && chmod +x /app/start.sh

# Expose port (optional, for health checks)
EXPOSE 8080

# Start aria2 and bot
CMD ["/app/start.sh"]
