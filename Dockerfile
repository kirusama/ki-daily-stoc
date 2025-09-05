# Dockerfile
FROM python:3.11-slim

# set working directory
WORKDIR /app

# install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy project files
COPY . .

# expose Django port
EXPOSE 8000

# run Django with gunicorn
CMD ["gunicorn", "stockmonitor.wsgi:application", "--bind", "0.0.0.0:8000"]
