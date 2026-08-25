# Render Free deployment

This repository includes a Render Blueprint in `render.yaml`.

## Deploy

1. Push the project to a private GitHub, GitLab, or Bitbucket repository.
2. In Render, create a new **Blueprint** and select the repository.
3. Enter every environment variable marked `sync: false` in the Blueprint form.
4. After Render assigns the service URL, set these callback URLs:
   - `LINKEDIN_REDIRECT_URI=https://YOUR-SERVICE.onrender.com/linkedin/callback`
   - `X_REDIRECT_URI=https://YOUR-SERVICE.onrender.com/x/callback`
5. Add the same callback URLs to the corresponding LinkedIn and X developer apps.
6. Verify `https://YOUR-SERVICE.onrender.com/health` returns `{"status":"ok"}`.
7. Open `https://YOUR-SERVICE.onrender.com/docs` for the API UI.

## Important limitations

- Never commit `.env`; enter secrets in Render's environment settings.
- Render Free web services sleep after inactivity, so the first request can be slow.
- Files written to the service filesystem, including `.env`, are not durable across
  deploys or restarts. OAuth tokens must ultimately be stored in a durable secret or
  database service. The current callbacks write refreshed tokens only to the runtime
  filesystem.
- The in-memory X OAuth state is lost when the service restarts. Complete login in
  one session and do not treat it as a production-grade state store.
