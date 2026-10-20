FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# LiteLLM Auto Router selects local Ollama or Kimi for each agent turn.
ENV OLLAMA_HOST=http://localhost:11434 \
    MODEL_PROVIDER=litellm \
    KIMI_MODEL_ID=kimi-k2.6

EXPOSE 8080

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080"]
