FROM python:3.13-slim

WORKDIR /app

# Install deps (fyers-apiv3 separately with --no-deps; see requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps fyers-apiv3==3.1.15

# App code + watchlist (secrets .env / token.json are mounted at runtime, NOT baked in)
COPY *.py watchlist.csv ./

CMD ["python", "run.py"]
