# orga-login

Sign-in helper for the EPDI AI gateway. It obtains and refreshes the token that
Claude Code needs, so you sign in with your corporate Google account and never
handle an API key.

One file, no dependencies beyond Python's standard library.

---

## What you need

| | How to check |
|---|---|
| Python 3.9 or later | `python3 --version` |
| Claude Code | `claude --version` |
| Three connection values | Ask your administrator (see below) |

---

## Install

### 1. Get the tool

```bash
git clone <this repository>
cd <this repository>
chmod +x orga-login
```

Put it somewhere permanent. Claude Code will be pointed at this exact path, so
moving the folder later means running `./orga-login setup` again.

### 2. Configure it

The values that identify your deployment are not shipped with the tool. Your
administrator will give you three: a **client id**, a **Cognito domain** and a
**gateway URL**.

```bash
mkdir -p ~/.orga-ai
cp config.example.json ~/.orga-ai/config.json
```

Then edit `~/.orga-ai/config.json` and fill them in.

Use the file rather than environment variables. Claude Code runs this helper from
a non-interactive shell that does not reliably inherit variables you export in a
terminal, so exported values make sign-in work while Claude Code fails
intermittently.

Check it:

```bash
./orga-login status
```

### 3. Connect Claude Code

```bash
./orga-login setup
```

This edits `~/.claude/settings.json`: it points Claude Code at the gateway, sets
the models the gateway serves, and installs this script as the credential helper.
Your existing settings are preserved and a timestamped backup is written first.

---

## Daily use

```bash
./orga-login login     # once per session, opens your browser
claude                 # work as usual
```

The token lasts an hour and is renewed silently in the background, so you will
not see it expire. You only sign in again when the longer-lived session ends.

| Command | What it does |
|---|---|
| `login` | Browser sign-in. Stores the session under `~/.orga-ai` |
| `token` | Prints a valid token. This is what Claude Code calls |
| `status` | Shows the configuration in use and whether a session is stored |
| `logout` | Revokes the session, deletes the stored token, clears the browser session |
| `setup` | Wires this helper into Claude Code |

---

## Getting access approved

Signing in and being authorised are two different things. Your corporate account
lets you authenticate; using the service additionally requires that an
administrator has approved you.

The first time, expect this:

1. **You sign in.** It works.
2. **Your first request is refused**, with a message naming the group you need.
3. **You ask your administrator** to add you. They cannot do it before your first
   sign-in, because your profile does not exist until then.
4. **You sign in again.** This step is required, not optional: your permissions
   travel inside the token that was issued before you were approved, so a new one
   has to be issued.

```bash
./orga-login logout
./orga-login login
```

If you skip step 4 you keep getting refused for up to an hour, until the old
token expires on its own.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| `missing configuration: client_id, cognito_domain` | Step 2 was skipped. The message prints the exact commands |
| `Error 403: org_internal` from Google | Your account has not been authorised on the sign-in application. Ask your administrator |
| `not a member of ...` | You are signed in but not approved yet, or you were approved and have not signed in again. See the section above |
| `Model access is denied ... AWS Marketplace` | The model is unavailable on the platform, not a problem with your account. Report it |
| `apiKeyHelper failed` | Claude Code cannot run this script. Re-run `./orga-login setup`, which fixes the path |
| `role 'system' is not supported on this model` | Claude Code is asking for a model the gateway does not serve. Re-run `./orga-login setup` and restart Claude Code |
| `timed out waiting for the browser sign-in` | The sign-in took longer than five minutes. Run `login` again and complete it in one go |

If `status` reports a configuration you do not recognise, remember the order of
precedence: `ORGA_*` environment variables beat `~/.orga-ai/config.json`, which
beats the `config.json` shipped here.

---

## What is stored on your machine

| Path | Contents |
|---|---|
| `~/.orga-ai/credentials.json` | Your session tokens, permissions `0600` |
| `~/.orga-ai/config.json` | The three connection values |
| `~/.claude/settings.json` | Gateway URL, model pins, credential helper |

Your tokens are the only secret in the flow, and they never leave
`~/.orga-ai`. `logout` revokes them.

---

## Notes for administrators

Nothing in this repository is a credential. The Cognito app client is public by
design: no secret, authorization code flow with PKCE. The three values kept out
of `config.json` were removed so that a public copy of this tool does not publish
an account id, a user pool or a hostname to indexers, not because knowing them
grants anything. Every one of them appears in the user's own address bar at each
sign-in.

`claude_models` in `config.json` must stay in step with the model map the gateway
serves and with the models the account can actually invoke. If they drift, the
gateway substitutes its default model, the call succeeds against a different one
than was requested, and the error a user eventually sees names a `system` role
instead of the model.
