FROM python:3.10-slim

WORKDIR /app

# Install PyTorch CPU-only first (lightweight ~150MB, separate layer for caching)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Upgrade pip and install other dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download MobileNetV2 weights during build (baked into image layer)
RUN python -c "import torchvision.models as m; m.mobilenet_v2(weights='DEFAULT')"

# Copy application code
COPY core.py .
COPY app.py .
COPY ImageNFT.abi.json .
COPY start-backend.sh .

# Create volume for logs/temp if needed
VOLUME ["/tmp"]

# Expose port (default for Flask)
EXPOSE 5000

# Use Gunicorn for production server
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
