ARG BUILD_FROM
FROM ${BUILD_FROM}

# Set shell
SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# Install system dependencies
RUN apk add --no-cache \
    jpeg-dev \
    zlib-dev \
    libffi-dev \
    gcc \
    musl-dev \
    python3-dev \
    curl \
    tzdata

# Install Python dependencies
COPY rootfs/app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Copy application files
COPY rootfs/app /app

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Working directory
WORKDIR /app

# Expose ingress port
EXPOSE 8099

CMD ["/run.sh"]
