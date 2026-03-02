FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source
COPY main.py strategy.py client.py config.py ./

CMD ["python", "-u", "main.py"]
