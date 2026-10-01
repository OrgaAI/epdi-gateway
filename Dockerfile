# Gateway image: Python control plane plus the admin dashboard bundle.
#
# Multi-stage, ONE image. The dashboard is served by the same process, on the same
# ALB target group, behind the same Cognito session - so a second image and a second
# ECS service would double the Fargate bill and add a target group and a pipeline to
# maintain, in exchange for isolating a page that reads the same data the gateway
# already writes.
#
# The Node stage does not reach the runtime image: only ui/dist is copied forward, so
# node_modules and the toolchain stay out of the shipped artefact and out of its
# vulnerability surface.

# --- stage 1: build the dashboard ---------------------------------------------
# Pinned to a specific minor, matching the Python base below: "node:22" would drift
# between builds and make a rebuild of the same commit non-reproducible.
FROM node:22.13-slim AS ui

WORKDIR /ui

# Manifests first, in their own layer. This is the layer CodeBuild's
# LOCAL_DOCKER_LAYER_CACHE actually reuses: a commit that only touches Python or
# .tsx files leaves package-lock.json untouched, so the whole install is a cache hit
# instead of a minute of npm.
COPY ui/package.json ui/package-lock.json* ./

# `npm ci` rather than `npm install`: it installs exactly the lock file and fails if
# the two have drifted, which is what makes the build reproducible. --no-audit and
# --no-fund drop two network round trips that only print advice.
#
# If no lock file exists yet, fall back to `npm install` so a fresh checkout can
# bootstrap one. Commit the generated ui/package-lock.json - without it every build
# resolves dependencies afresh and the pinned versions in package.json are the only
# thing standing between two builds producing different bundles.
RUN if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund; \
    else \
      echo "WARN: ui/package-lock.json missing, resolving fresh (commit the lock file)"; \
      npm install --no-audit --no-fund; \
    fi

COPY ui/ ./

# Runs tsc --noEmit then vite build, so a type error fails the image build rather
# than shipping a dashboard that breaks in the browser.
RUN npm run build

# --- stage 2: runtime ----------------------------------------------------------
# Slim Python base, non-root user. Two dependencies: PyJWT[crypto] for validating
# Cognito tokens and boto3 for Bedrock, CloudWatch, Firehose and DynamoDB.
FROM python:3.12-slim

# Image tag baked in at build time, so "/" reports the running version. buildspec.yml
# passes it with --build-arg; without this ARG the value is silently dropped and the
# banner always reads "dev". The ECS task definition also sets APP_VERSION, which
# overrides this at runtime.
ARG APP_VERSION=dev

# Do not buffer stdout/stderr: logs reach CloudWatch immediately, and no .pyc
# clutter in the layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

# Install deps first (better layer caching), then the app.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# The built dashboard. admin.py serves it from here, and server.py caches
# /admin/assets/* immutably because Vite fingerprints those filenames.
COPY --from=ui /ui/dist /app/static

# Run as an unprivileged user. The port is 8080 (>1024) precisely so a non-root
# process can bind it without extra capabilities.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# HEALTHCHECK is for local `docker run` convenience; in ECS the authoritative
# health signal is the ALB target group hitting /health. Uses urllib from the
# stdlib so no extra tool (curl/wget) has to be installed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["python", "server.py"]
