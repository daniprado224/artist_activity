FROM python:3.12.5-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ scripts/
COPY seed/ seed/
COPY sql/ sql/

WORKDIR /app/scripts

CMD ["python", "--version"]
