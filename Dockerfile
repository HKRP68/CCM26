FROM python:3.11-slim

WORKDIR /app

# Install fonts and Chromium for the original HTML tournament summary export.
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-dejavu-core fonts-bebas-neue chromium && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
