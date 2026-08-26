# Render Free deployment and operations

This repository includes a Render Blueprint in `render.yaml`.

## Deploy

1. Push the project to a private GitHub, GitLab, or Bitbucket repository.
2. In Render, create a **Blueprint** and select the repository.
3. Enter every environment variable marked `sync: false` in the Blueprint form.
4. Keep secrets as Render **Environment Variables**, not Secret Files.
5. Verify `/health` returns `{"status":"ok"}` after every deploy.

## Required production controls

- Keep `APP_API_KEY` secret. Publishing and status endpoints require it in the
  `X-API-Key` request header.
- Keep `OAUTH_SETUP_ENABLED=false` in Render. OAuth login and callback endpoints
  return 404 while setup mode is disabled.
- Never commit `.env`. It is ignored by Git and is only for local configuration.
- Publish to each platform separately. `/publish-all` is preview-only and rejects
  real bulk publishing with HTTP 409 to prevent partial or duplicate posts.

## X authentication

Production publishing prefers OAuth 1.0a because its Access Token and Secret do
not depend on writes to Render Free's ephemeral filesystem.

Required Render variables:

- `X_OAUTH1_CONSUMER_KEY`
- `X_OAUTH1_CONSUMER_SECRET`
- `X_OAUTH1_ACCESS_TOKEN`
- `X_OAUTH1_ACCESS_TOKEN_SECRET`

The existing OAuth 2.0 variables can remain as a fallback. Check production with
`GET /x/status`; a durable configuration returns `auth_mode: "oauth1"`.

## LinkedIn maintenance

Standard LinkedIn access tokens are issued with a 60-day lifetime. Check
`GET /linkedin/status` regularly. Renew when `renewal_required` becomes `true`
or before `days_remaining` reaches zero.

Renewal procedure:

1. Use the local callback flow whenever possible.
2. Temporarily set local `OAUTH_SETUP_ENABLED=true`.
3. Complete `/linkedin/login` and save the new token locally.
4. Copy only `LINKEDIN_ACCESS_TOKEN` to the Render Environment setting.
5. Save and wait for the deploy to become Live.
6. Restore `OAUTH_SETUP_ENABLED=false` and verify `/linkedin/status`.

Do not expose access tokens in screenshots, URLs, chat messages, or logs.

## Platform status checks

- Service: `GET /health`
- Instagram: `GET /instagram/status`
- X: `GET /x/status` with `X-API-Key`
- LinkedIn: `GET /linkedin/status` with `X-API-Key`

Status checks do not publish content.

## Render Free limitations

- The service sleeps after inactivity, so the first request can be delayed.
- The filesystem is ephemeral. Files written at runtime are lost after a restart
  or deploy.
- Persistent disks are not available to Free web service instances.
- OAuth setup state is stored in process memory and is lost on restart. Complete
  an enabled OAuth flow in one session, then disable setup mode again.
