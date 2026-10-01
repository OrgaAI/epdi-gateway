# epdi-ai-gateway

Consumption gateway for the ORGA AI managed-LLM platform.

It speaks the **Anthropic Messages API** on the front, so Claude Code and any
Anthropic-compatible client can point straight at it, and calls Anthropic Claude
models on **Amazon Bedrock** at the back, streaming answers through unbuffered.
Technicians authenticate with a Cognito OIDC token, which the gateway validates
itself. Runs on ECS Fargate behind an ALB.

> **Status: inference path working, metering not built.** Auth, the
> Anthropic-compatible surface and streaming Bedrock inference are implemented.
> Per-user spend counters, budgets and 100% quota blocking are later roadmap
> stages. The gateway records **spend metadata only** (token counts) - never
> prompt or response content.

## What is here

```
app/server.py     HTTP server (Python stdlib + boto3): the Messages API surface
Dockerfile        Non-root, slim Python image; health-check baked in
requirements.txt  PyJWT[crypto] (token validation) + boto3 (Bedrock)
buildspec.yml     CodeBuild spec: build -> push to ECR -> imagedefinitions.json
```

Infrastructure (ECR, ECS, ALB, Cognito, CI/CD) lives in the separate
`epdi-ai-terraform` repository. This repo owns only the application and its build.

## Endpoints

| Method | Path             | Auth | Purpose                                        |
|--------|------------------|------|------------------------------------------------|
| POST   | `/v1/messages`   | yes  | Inference. Anthropic Messages; SSE when `stream: true` |
| GET    | `/v1/models`     | yes  | Model discovery for the Claude Code `/model` picker |
| GET    | `/health`        | no   | ALB target-group health check                  |
| GET    | `/`              | no   | JSON banner (version, default model, region)   |
| GET    | `/whoami`        | yes  | Debug: show the validated token's claims       |
| HEAD   | `/api/hello`     | no   | Claude Code connection-warming probe           |

`/health` is deliberately trivial. If it depended on Bedrock or Cognito, an
upstream blip would make ECS cycle otherwise-healthy tasks.

Auth is `Authorization: Bearer <cognito-access-token>` or `x-api-key` (Claude Code
sends one or both, depending on which variable the developer set). Validation
happens in the app, not on the ALB: ALB `authenticate-oidc` is a browser redirect
flow and does not fit a CLI client sending a bearer token.

## Connect Claude Code

```bash
export ANTHROPIC_BASE_URL="https://gateway.example.com"   # NO trailing /v1
export ANTHROPIC_AUTH_TOKEN="<cognito-access-token>"
claude
```

`ANTHROPIC_BASE_URL` must **not** end in `/v1`. Claude Code appends
`/v1/messages` itself, so a base URL ending in `/v1` produces `/v1/v1/messages`
and every request 404s.

Optional: `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` makes Claude Code query
`/v1/models` at startup and add the returned models to the `/model` picker.

Do **not** set `CLAUDE_CODE_USE_BEDROCK=1`. That points Claude Code straight at
Bedrock, taking this gateway out of the request path - which would make metering
and quota enforcement impossible.

## Getting a token

Two different flows, because the user pool holds two kinds of user.

### Local test user (password flow, no browser)

The app client has `USER_PASSWORD_AUTH` enabled for exactly this.

```bash
# client_id / user_pool_id come from the Terraform `cognito` output
terraform -chdir=../epdi-ai-terraform/environments/epdi-ai output -json cognito

TOKEN=$(aws cognito-idp initiate-auth \
  --region <AWS_REGION> \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id <CLIENT_ID> \
  --auth-parameters USERNAME='<email>',PASSWORD='<password>' \
  --query 'AuthenticationResult.AccessToken' --output text)
```

### Federated users (Identity Center / identity store)

`USER_PASSWORD_AUTH` does **not** work for them: Cognito never holds their
password, Identity Center verifies it. They must go through the Hosted UI
authorization-code flow, which involves a browser once:

```
client → Hosted UI /authorize?identity_provider=IdentityCenter
       → Identity Center login → SAML back to Cognito
       → authorization code on the redirect URI → exchange for tokens
```

`http://localhost:8080/callback` is already registered as a callback URL for this
purpose. A small helper that opens the browser, catches the code on localhost and
exchanges it for an access token is **not built yet** - it is the missing piece
for federated technicians using Claude Code.

## Try it with curl

```bash
# Non-streaming
curl -s https://gateway.example.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"sonnet","max_tokens":256,
       "messages":[{"role":"user","content":"Say hi in one sentence."}]}'

# Streaming (-N disables curl buffering so the SSE events show as they arrive)
curl -N -s https://gateway.example.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"model":"sonnet","max_tokens":256,"stream":true,
       "messages":[{"role":"user","content":"Count to five."}]}'
```

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app/server.py
```

Unauthenticated endpoints work immediately:

```bash
curl localhost:8080/health     # {"status": "ok"}
curl localhost:8080/           # banner
curl -i localhost:8080/whoami  # 401 - no token
```

For `/v1/messages` to reach `200`, the server needs the Cognito coordinates and
AWS credentials with Bedrock access:

```bash
export COGNITO_ISSUER="https://cognito-idp.<AWS_REGION>.amazonaws.com/<USER_POOL_ID>"
export COGNITO_JWKS_URI="$COGNITO_ISSUER/.well-known/jwks.json"
export COGNITO_CLIENT_ID="<CLIENT_ID>"
python app/server.py
```

Without them, protected endpoints answer `401 auth not configured` by design -
the container stays deployable and health-checkable before Cognito is wired.

Or via Docker:

```bash
docker build -t epdi-ai-gateway:local .
docker run --rm -p 8080:8080 epdi-ai-gateway:local
```

## Configuration

| Env var                    | Default                                        | Meaning                                                       |
|----------------------------|------------------------------------------------|---------------------------------------------------------------|
| `PORT`                     | `8080`                                         | Listen port. Must match the ECS container port / ALB TG.      |
| `APP_VERSION`              | `dev`                                          | Reported on `/`. CodeBuild sets it to the image tag.          |
| `COGNITO_ISSUER`           | *(empty)*                                      | Cognito issuer URL; the token `iss` is checked against it.    |
| `COGNITO_JWKS_URI`         | *(empty)*                                      | JWKS endpoint for signature verification.                     |
| `COGNITO_CLIENT_ID`        | *(empty)*                                      | App client id; access-token `client_id` is checked against it. |
| `BEDROCK_REGION`           | `AWS_REGION` or `eu-west-1`                    | Region of the `bedrock-runtime` endpoint.                     |
| `BEDROCK_MODEL_ID`         | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | Fallback model. EU inference profile keeps inference in the EU. |
| `MODEL_MAP`                | *(empty)*                                      | JSON map of client model name → Bedrock id, e.g. `{"sonnet":"eu.anthropic.…"}`. |
| `DEFAULT_MAX_TOKENS`       | `4096`                                         | Used only when the client omits `max_tokens`.                 |
| `PING_INTERVAL_SECONDS`    | `15`                                           | SSE keep-alive interval during upstream silence.              |
| `FORWARD_ANTHROPIC_BETA`   | *(off)*                                        | Translate the `anthropic-beta` header into the Bedrock `anthropic_beta` body field. |
| `BEDROCK_ANTHROPIC_VERSION`| `bedrock-2023-05-31`                           | Bedrock dialect string in the request body.                   |

Cognito values come from the Terraform `cognito` output. The task role needs
`bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, plus Bedrock
model access enabled for the Claude models.

## Protocol behaviour worth knowing

These are requirements of the Claude Code gateway contract, not stylistic choices:

- **Never buffer.** Responses stream; a gateway that collects the full answer
  first makes Claude Code stall.
- **Keep-alive pings are generated here.** Claude Code aborts a stream that is
  silent for 300s. Bedrock's event stream sends no pings of its own, so during a
  long thinking pause the gateway emits its own `ping` events.
- **Request body fields are forwarded as an open list.** Only fields Bedrock
  rejects are dropped (`model`, `stream`, `context_management`, `output_config`,
  `metadata`). Allowlisting instead would break each new Claude Code capability
  on the release that introduces it. `cache_control` markers and block-form
  `system` content pass through untouched, or prompt caching silently stops
  working.
- **Upstream errors are relayed verbatim.** Claude Code matches on the error
  wording to decide whether to retry and disable a capability, so Bedrock's
  message is not rewrapped. Throttling is surfaced as `429`.

## Not built yet

Per-user spend metering and DynamoDB counters, S3 request history, Athena/Glue
reporting, tiered budget alerts (50/75/90%), 100% quota enforcement, the OAuth
helper for federated logins, group-claim passthrough from federation, and the
optional `/v1/messages/count_tokens` endpoint (without it Claude Code falls back
to a character-based context estimate).
