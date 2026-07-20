# Spotify README Widget Setup

## Chosen implementation

The README currently uses a safe placeholder instead of a live Spotify card. This avoids broken images and prevents exposing Spotify credentials before authentication is completed.

Recommended path: use a small serverless endpoint or an actively maintained hosted GitHub Spotify widget that stores Spotify OAuth credentials outside the repository.

## Why this approach

- GitHub README files cannot run JavaScript or securely store secrets.
- Spotify currently-playing access requires OAuth credentials and a refresh token.
- Secrets must live in environment variables on Vercel, Cloudflare Workers, or the hosted widget provider — never in `README.md`.
- The README should not show a broken image while setup is pending.

## Spotify authentication steps

1. Create an app in the Spotify Developer Dashboard.
2. Add the callback URL required by your chosen widget or serverless endpoint.
3. Authorize with scopes for currently playing and recently played tracks.
4. Store generated credentials only in the deployment/widget provider.
5. Copy the final public SVG/card URL into the `Current soundtrack` section of `README.md`.

## Required secrets

Do not commit these values:

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REFRESH_TOKEN`

## Where secrets should be configured

- Vercel: Project Settings → Environment Variables
- Cloudflare Workers: Worker Settings → Variables and Secrets
- Hosted widget: provider-specific private settings page

## Local testing

For a custom endpoint:

1. Create a local `.env` file that is ignored by Git.
2. Add Spotify credentials locally.
3. Run the endpoint locally.
4. Confirm it returns an SVG/card response without printing secrets.

## Deployment

Deploy the endpoint or configure the hosted widget, then embed only the public card URL in `README.md`.

## Security considerations

- Never paste client secrets, access tokens, or refresh tokens into Markdown.
- Rotate credentials if they are exposed.
- Use cache headers to reduce Spotify API calls.
- Return a neutral fallback when Spotify is unavailable.

## Fallback behavior

The final card should:

- Show the currently playing track when available.
- Fall back to the most recently played track when offline.
- Handle missing album art, podcasts, private sessions, and Spotify API errors.

## Troubleshooting

- Broken image: verify the SVG/card URL is public and returns image content.
- No current track: confirm Spotify scopes include currently playing access.
- No recent track: confirm recently played scope and recent account activity.
- Rate limits: add caching at the endpoint/widget layer.

## Revoke access

Revoke the app from Spotify Account → Apps, then rotate or delete credentials from the deployment provider.
