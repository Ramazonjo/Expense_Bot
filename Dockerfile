FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --create-home --uid 10001 bot
COPY expense_bot /app/expense_bot
RUN mkdir /app/data && chown -R bot:bot /app/data
USER bot
CMD ["python", "-m", "expense_bot"]
