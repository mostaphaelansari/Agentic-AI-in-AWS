# Build context must be the repo root (see opensource/compose/docker-compose.yml):
# the app resolves repo-relative paths (apps/comparison/assets, docs/) at runtime.
FROM python:3.12-slim

WORKDIR /app

COPY opensource/ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opensource/ui/ opensource/ui/
COPY apps/comparison/assets/ apps/comparison/assets/
COPY apps/comparison/molecule_viewer.py apps/comparison/molecule_viewer.py
COPY apps/comparison/molstar_component.py apps/comparison/molstar_component.py
COPY docs/ docs/

EXPOSE 8501

CMD ["streamlit", "run", "opensource/ui/app.py", \
     "--server.port", "8501", "--server.address", "0.0.0.0", \
     "--server.headless", "true"]
