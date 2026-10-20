# MLflow tracking server + LiteLLM.
#
# The stock image has no `litellm`, and MLflow evaluates any judge whose model
# URI is not `openai:/...` through the LiteLLM provider — including scorers
# registered from the MLflow UI, which run inside this container rather than in
# the caller's process. Without the package those scorers fail verification with
# "The `litellm` package is required to use the LiteLLM provider".
#
# The `openai:/...` judges in evaluations/ run in the caller's process and do
# not depend on this.
FROM ghcr.io/mlflow/mlflow:v3.14.0

# >= 1.9x: older releases (1.63.x) reject `moonshot/...` with "LLM Provider NOT
# provided" — the Moonshot provider is not in their routing table.
RUN pip install --no-cache-dir "litellm==1.93.0"
