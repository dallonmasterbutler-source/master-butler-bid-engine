# Connecting Google Calendar — one-time setup

You only do this once, Dallon. It gives Rhythm permission to read when you're
busy and to add outings/shopping to your calendars. Until it's done, the whole
app still works — the outing suggestions just won't know your real free time.

Plan on about 15 minutes. Nothing here costs money.

## 1. Make a Google Cloud project
1. Go to <https://console.cloud.google.com/>.
2. Top bar → project dropdown → **New Project**. Name it `Rhythm`. Create.

## 2. Turn on the Calendar API
1. Left menu → **APIs & Services → Library**.
2. Search **Google Calendar API** → **Enable**.

## 3. Set up the consent screen
1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → Create.
3. App name `Rhythm`, your email for support + developer contact. Save & continue.
4. **Scopes** — skip (leave as is), Save & continue.
5. **Test users → Add users** — add **your** Google email **and your wife's**.
   (While the app is in "testing," only these two accounts can connect. That's
   exactly what we want — it stays private.) Save.

## 4. Create the credentials
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Web application**. Name it `Rhythm web`.
3. Under **Authorized redirect URIs → Add URI**, add your app's callback:
   - Hosted on Render: `https://YOUR-APP.onrender.com/oauth/callback`
   - Testing on your Mac: `http://localhost:8100/oauth/callback`
   (You can add both.)
4. **Create.** Google shows a **Client ID** and **Client secret** — copy both.

## 5. Give them to Rhythm
Set these two environment variables (Render → your service → **Environment**,
or in your shell when running locally):
```
GOOGLE_CLIENT_ID=...the client id...
GOOGLE_CLIENT_SECRET=...the client secret...
```
Also make sure `RHYTHM_BASE_URL` matches the site you added in step 3
(e.g. `https://YOUR-APP.onrender.com` or `http://localhost:8100`).

Redeploy / restart.

## 6. Link your calendars
Open Rhythm → **Calendar** (from Settings). You'll each see a **Connect**
button. Tap it, sign in with the matching Google account, allow access. Do it
once for you and once for your wife. Done — the **Shared openings** list and
"Add to calendar" buttons come alive.

### Notes
- The app asks for calendar read + event-write only. It reads your free/busy
  and adds events you choose — it never deletes anything or touches other
  people's calendars.
- If a **Connect** button ever errors, the usual cause is the redirect URI in
  step 3 not exactly matching `RHYTHM_BASE_URL` + `/oauth/callback`.
- To move past the "testing" banner later you'd "publish" the app, but you
  don't need to — two test users is plenty for the two of you.
