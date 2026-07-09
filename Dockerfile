FROM python:3.12-slim

# git: the AI feature-coder works in worktrees and commits/pushes.
# nodejs: optional JS syntax check for edited HTML (feature_coder degrades
# gracefully if absent, but it's cheap to include).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client nodejs ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The actual source is bind-mounted over /app at runtime (so the AI coder's
# merges + Lauren's approved features persist on the host and can be pushed to
# GitHub). This COPY is just a sane fallback if run without the mount.
COPY . .

EXPOSE 8000
CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8000"]
