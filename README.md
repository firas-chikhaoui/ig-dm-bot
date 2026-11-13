# ig-dm-bot 🤖📩

A self-hosted **Instagram comment-to-DM bot**: an open-source alternative to ManyChat and similar platforms.

Someone comments a keyword on your reel. The bot replies under the comment, sends them a DM, can ask them to **follow you before they get the link**, and can collect their **email for a waitlist**. Everything is configured in one file: `rules.yaml`.

> Hosting it on a small cloud server costs about **€4/month**, compared with roughly €40/month for a subscription tool.

## Features

- 🎯 **Per-reel keyword triggers**: one or more keywords per reel, or `"*"` for every post
- 💬 **Public comment replies**: picked at random from a list so they don't look spammy
- 📩 **DM flows with buttons**: postback buttons (go to another step) and link buttons
- ✅ **Follow gate**: checks whether the user follows you before sending the link
- 📧 **Email capture**: validates the email and saves it to a waitlist (export as CSV)
- 🔒 **Privacy policy and data deletion** pages built in (Meta requires both), plus a "delete my data" DM command and Meta's deletion callback
- 🔑 **Automatic token refresh**: the 60-day Instagram token is renewed in the background before it expires
- 🚨 **Error alerts**: failed Instagram API calls are logged clearly, and optionally sent to you on Telegram
- 🔁 **Hot reload**: edit `rules.yaml` while the bot runs. A broken file never takes the bot down.
- 🛡️ Webhook signature check, duplicate-event protection, loop protection, once-per-user per reel
- 🧪 Tests and a `DRY_RUN` mode

## How it works

```
User comments "guide" on your reel
        │
        ▼
Instagram ──webhook──▶  /webhook  (FastAPI)
                            │
                            ├─ matches reel id + keyword in rules.yaml
                            ├─ public reply under the comment
                            └─ private DM  ──▶  flow: buttons → follow check → link → email
```

## The rules file

```yaml
reels:
  - id: "17900000000000000"        # media id of the reel ("*" = any post)
    keywords: ["guide", "link"]
    comment_replies: ["Sent you a DM 📩", "Check your DMs {username} 🔥"]
    flow: free_guide

flows:
  free_guide:
    start: welcome
    steps:
      welcome:
        message: "Hey {username}! Want the free guide?"
        buttons:
          - { title: "Yes, send it 🙌", next: check_follow }
      check_follow:
        require_follow: { ok: send_link, not_following: ask_to_follow }
      ask_to_follow:
        message: "Follow me first, then tap the button 👇"
        buttons:
          - { title: "I followed ✅", next: check_follow }
      send_link:
        message: "Here's your guide 🎁"
        buttons:
          - { title: "Open the guide", url: "https://example.com/guide" }
          - { title: "Join the waitlist", next: ask_email }
      ask_email:
        ask_email:
          prompt: "Drop your email 📧"
          list: waitlist
          success: "You're on the list! 🎉"
```

The fully commented version is in [`rules.yaml`](rules.yaml).

**Step types**

| Step | What it does |
|---|---|
| `message` | Sends text with up to 3 buttons (`next:` jumps to a step, `url:` opens a link). Without buttons, `next:` continues automatically. |
| `require_follow` | Goes to `ok` if the user follows you, otherwise to `not_following`. Optional: `still_not_following` (step used on later failed checks so the bot doesn't repeat itself), `max_retries` and `gave_up` (see below). |
| `ask_email` | Asks for an email, validates it, saves it to `list`, then sends `success` and optionally goes to `next`. |

**Follow gate retries.** The first failed check goes to `not_following`; each further failed check goes to `still_not_following` (or `not_following` again if you leave it out). After `max_retries` re-checks (default: `settings.follow_max_retries`, which defaults to 3) the bot sends the `gave_up` step once, if set, and then stops answering that check. If the person does follow later and taps the button, they still get `ok`. The counter resets when they follow or start the flow again.

```yaml
check_follow:
  require_follow:
    ok: send_link
    not_following: ask_to_follow
    still_not_following: still_not_following
    max_retries: 3
    gave_up: gave_up
```

> ⚠️ Instagram allows only **one** private reply per comment. The first DM of a flow should therefore be a message **with a button**: when the user taps it, the conversation opens and the bot can continue. If a flow starts with a follow check or email step, the bot sends a short opener with a button automatically.

## Setup

### 1. Meta / Instagram app

1. Your Instagram account must be a **Professional** account (Business or Creator).
2. On [developers.facebook.com](https://developers.facebook.com), create an app and add the **Instagram** product. Use **API setup with Instagram login**.
3. Add your Instagram account and generate an **access token**. This is the `IG_ACCESS_TOKEN`.
   Permissions needed: `instagram_business_basic`, `instagram_business_manage_messages`, `instagram_business_manage_comments`.
4. Copy the **Instagram app secret**. This is the `APP_SECRET`.
5. In the Instagram app on your phone, go to *Settings → Messages and story replies → Message controls → Connected tools* and turn on **Allow access to messages**.

### Privacy policy and data deletion (required by Meta)

Meta asks every app for a **Privacy Policy URL** and a **User data deletion** option before it can go Live. The bot serves both:

| URL | What it is |
|---|---|
| `https://YOUR-DOMAIN/privacy` | Privacy policy, filled in from `PRIVACY_OPERATOR`, `PRIVACY_CONTACT` and `IG_USERNAME` in `.env` |
| `https://YOUR-DOMAIN/data-deletion` | Data deletion instructions (and status page for confirmation codes) |
| `POST https://YOUR-DOMAIN/data-deletion` | Meta's *Data Deletion Request Callback* (signed with `APP_SECRET`) |

In the Meta dashboard (*App settings → Basic*): paste the `/privacy` URL into **Privacy Policy URL**, and under **User data deletion** choose *Data deletion callback URL* and paste the `/data-deletion` URL. Set `PUBLIC_URL` in `.env` so the confirmation link Meta shows to the person points to your domain.

People can delete their data in three ways, and each one removes their state, emails, follow-check counters and activity log entries:

- DM **`delete my data`** to your account (change or turn off the phrase with `settings.data_deletion` in `rules.yaml`)
- remove the app in Instagram's settings, which triggers the callback above
- ask you, and you run `curl -X DELETE "https://YOUR-DOMAIN/admin/users/<instagram-user-id>?token=ADMIN_TOKEN"`

> The privacy page is a sensible default for this bot, not legal advice. If you collect emails for marketing in the EU, check it against your own GDPR obligations.

### 2. Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in the values
pytest                      # everything should pass
uvicorn app.main:app --port 8000
```

Find the media id of your reels and paste it into `rules.yaml`:

```bash
python scripts/list_media.py
```

For local testing, Instagram needs a public HTTPS URL. Use a tunnel such as `ngrok http 8000` or `cloudflared tunnel --url http://localhost:8000`.

### See it with your own eyes: the playground 👀

Set `DRY_RUN=true` in `.env`, start the bot, then open **http://localhost:8000/playground**.

Comment on the fake reel on the left; the DMs appear on the right like on a phone. You can tap the buttons, type your email, and use the toggle to pretend the follower does or doesn't follow you. Nothing is sent to Instagram. (The page is disabled when `DRY_RUN=false`.)

### Test without Instagram from the command line

Set `DRY_RUN=true` in `.env`, start the bot, and in a second terminal:

```bash
python scripts/simulate.py comment "send me the guide"     # someone comments on the reel
python scripts/simulate.py click free_guide check_follow   # taps "Yes, send it"
python scripts/simulate.py click free_guide ask_email      # taps "Join the waitlist"
python scripts/simulate.py dm "me@example.com"             # types their email
```

Every message the bot *would* send is printed in the bot's terminal as `[DRY_RUN]`.
To test the "not following" path, set `DRY_RUN_FOLLOWS=false` and restart.

### 3. Webhook

In the Meta dashboard (Instagram → Webhooks):

- **Callback URL:** `https://YOUR-DOMAIN/webhook`
- **Verify token:** the same value as `VERIFY_TOKEN` in `.env`
- Subscribe to the fields **`comments`**, **`messages`** and **`messaging_postbacks`**

> If the bot only reacts to comments from your own or test accounts, the app is still in **Development mode**. To make it work for everyone, switch the app to **Live**. This may require App Review for advanced access to the permissions above.

### 4. Deploy to the cloud (~€4/month)

Any small VPS works (Hetzner, Contabo, OVH, etc.):

```bash
git clone https://github.com/<you>/ig-dm-bot.git && cd ig-dm-bot
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

Put HTTPS in front of it. Caddy is the easiest option; an example `Caddyfile`:

```
bot.yourdomain.com {
    reverse_proxy localhost:8000
}
```

`rules.yaml` and `data/` are mounted as volumes, so you can edit the rules on the server without rebuilding, and your emails survive restarts.

### 5. Token refresh (automatic)

Long-lived Instagram tokens expire after 60 days. The bot takes care of this itself:

- On first start it copies `IG_ACCESS_TOKEN` into its database (`data/bot.db`).
- Every few hours (`TOKEN_CHECK_HOURS`, default 6) it checks the token and refreshes it once fewer than `TOKEN_REFRESH_DAYS_BEFORE` days (default 10) are left. The new token is used right away, no restart needed.
- Right after setup the expiry is unknown, so the bot tries a refresh straight away. Instagram only allows refreshing tokens that are at least 24 hours old, so a warning like `refresh failed, will retry later` on the first day is normal.
- If refreshing keeps failing and fewer than 7 days are left, the bot logs an **error** on every check. If the token does expire, generate a new one in the Meta dashboard, put it in `IG_ACCESS_TOKEN` and restart: a new token in `.env` always replaces the stored one.

Check the status (the token itself is never shown) or force a refresh:

```bash
curl "https://YOUR-DOMAIN/admin/token?token=ADMIN_TOKEN"
curl -X POST "https://YOUR-DOMAIN/admin/token/refresh?token=ADMIN_TOKEN"
python scripts/refresh_token.py            # same thing from the command line (--status to only look)
```

Set `TOKEN_AUTO_REFRESH=false` to turn this off. Nothing is refreshed in `DRY_RUN`.

### 6. Error alerts on Telegram (optional)

Every failed Instagram API call is logged as one readable line, with Meta's error code and a hint, for example:

```
ERROR instagram: Instagram API POST 1784.../messages failed: HTTP 400, code 10/2534022, OAuthException: ... (permission missing or the 24h messaging window is closed) [fbtrace_id AbC...]
```

To also get these on your phone, let the bot send every **error** to a Telegram chat:

1. In Telegram, talk to **@BotFather**, send `/newbot` and copy the token it gives you.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number after `"chat":{"id":`.
3. Put both in `.env` and restart:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```
4. Check it works: `curl -X POST "https://YOUR-DOMAIN/admin/alerts/test?token=ADMIN_TOKEN"`

You get an alert for failed API calls, crashes while handling a comment or DM, a broken `rules.yaml` and an Instagram token that is about to expire. The same error is sent at most once every 5 minutes (`TELEGRAM_ALERT_COOLDOWN`, in seconds); the next alert says how many were skipped. Access tokens are removed from alert texts. With either value empty, alerts are off.

## Admin endpoints

| Endpoint | |
|---|---|
| `GET /health` | Health check for uptime monitoring |
| `GET /admin/stats?token=ADMIN_TOKEN` | Counts of triggers, button clicks, follow checks and emails |
| `GET /admin/emails.csv?token=ADMIN_TOKEN&list=waitlist` | Downloads the collected emails |
| `GET /admin/token?token=ADMIN_TOKEN` | Instagram token expiry, last refresh and last error |
| `POST /admin/token/refresh?token=ADMIN_TOKEN` | Refreshes the Instagram token now |
| `POST /admin/alerts/test?token=ADMIN_TOKEN` | Sends a test message to your Telegram alert chat |
| `DELETE /admin/users/<user_id>?token=ADMIN_TOKEN` | Deletes everything stored about one person |
| `GET /privacy`, `GET /data-deletion` | Public privacy policy and data deletion pages |
| `POST /data-deletion` | Meta's data deletion callback |

## Project structure

```
app/
  main.py        web server: webhook + admin endpoints
  bot.py         flow engine (comments, buttons, follow gate, email)
  rules.py       rules.yaml loader, validation, hot reload
  instagram.py   Instagram API client
  privacy.py     privacy policy, data deletion page + Meta callback
  tokens.py      stores and auto-refreshes the Instagram access token
  alerts.py      optional Telegram alerts for errors
  storage.py     SQLite: state, dedup, emails, stats, token
  config.py      settings from .env
rules.yaml       your reels, keywords and flows
scripts/         list_media.py, refresh_token.py, simulate.py
tests/
```

## Roadmap

- [ ] Web dashboard (frontend) to edit rules and see stats
- [ ] Multiple Instagram accounts
- [ ] Send waitlist emails to a newsletter tool (Mailchimp, Brevo, etc.)

## License

MIT
