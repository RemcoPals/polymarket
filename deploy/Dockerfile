FROM python:3.13-slim

# Asset to run: btc, eth, sol, xrp
ARG ASSET=eth
ENV ASSET=${ASSET}

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy bot code
COPY bots/ ./bots/
COPY run_bot.py .

CMD ["sh", "-c", "python -u run_bot.py --asset ${ASSET}"]
