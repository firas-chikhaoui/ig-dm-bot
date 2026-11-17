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
- 👥 **Multiple accounts**: Instagram accounts and their tokens live in the database (seeded from `.env`), with pause/resume; webhook events are routed to the right account automatically
- 🚨 **Error alerts**: failed Instagram API calls are logged clearly, and optionally sent to you on Telegram
- 🔁 **Hot reload**: edit `rules.yaml` while the bot runs. A broken file never takes the bot down.
- 🛡️ Webhook signature check, duplicate-event protection, loop protection, once-per-user per reel
- 🌐 **One-command production deploy**: Docker Compose + Caddy with automatic HTTPS
- 🧪 Tests and a `DRY_RUN` mode
- 📝 **Meta App Review guide** with ready-to-paste permission texts and a screencast script ([docs/app-review.md](docs/app-review.md))

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

> Running the bot only on your own account works with Standard Access, no App Review needed. To serve accounts you don't own, you need Advanced Access: **[docs/app-review.md](docs/app-review.md)** has the checklist, the permission texts to paste and a screencast script.

### 4. Deploy to production (~€4/month)

The repo ships a ready-to-use setup: `docker-compose.yml` runs the bot, and a **Caddy** service (`Caddyfile`) puts HTTPS in front of it with a free Let's Encrypt certificate that renews itself. You need a small Linux server and a domain or subdomain.

**1. Server.** Any small VPS with Ubuntu or Debian works (Hetzner CX22, Contabo, OVH, etc.; 1 vCPU / 1–2 GB RAM is plenty). Install Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

**2. Domain.** At your DNS provider, create an **A record** (and an AAAA record if the server has IPv6) for e.g. `bot.yourdomain.com` pointing at the server's IP. Wait until `ping bot.yourdomain.com` shows that IP.

**3. Firewall.** Open SSH, HTTP and HTTPS only. Port 80 is needed so Caddy can get the certificate; the bot's own port 8000 stays private (it is bound to `127.0.0.1`).

```bash
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw enable
```

**4. Install and configure.**

```bash
git clone https://github.com/<you>/ig-dm-bot.git && cd ig-dm-bot
cp .env.example .env && nano .env
```

Fill in the Instagram values and set `DOMAIN=bot.yourdomain.com`, `PUBLIC_URL=https://bot.yourdomain.com`, `DRY_RUN=false`, and long random strings for `VERIFY_TOKEN` and `ADMIN_TOKEN` (`openssl rand -hex 24` makes one).

**5. Start.**

```bash
docker compose --profile https up -d --build
docker compose --profile https logs -f          # Ctrl+C to stop watching
```

Within a minute Caddy logs `certificate obtained successfully`. Check it:

```bash
curl https://bot.yourdomain.com/health          # {"ok":true}
```

**6. Point Meta at it.** Use `https://bot.yourdomain.com/webhook` as the webhook callback URL (step 3 above), and the `/privacy` and `/data-deletion` URLs in *App settings → Basic*.

**Everyday commands**

| What | Command |
|---|---|
| Update to the latest version | `git pull && docker compose --profile https up -d --build` |
| Edit the rules | `nano rules.yaml` (reloaded automatically, no restart) |
| Restart after changing `.env` | `docker compose --profile https up -d` |
| See the logs | `docker compose --profile https logs -f bot` |
| Status | `docker compose --profile https ps` (the bot shows `healthy`) |
| Back up emails and state | `tar czf backup-$(date +%F).tgz data/ rules.yaml .env` |

Everything the bot stores lives in `data/bot.db` (SQLite), so a copy of `data/` is a full backup; keep it somewhere safe because it contains the email list and the access token. Certificates live in the `caddy_data` Docker volume; don't delete it, or Caddy has to request new ones (Let's Encrypt limits how often).

**Troubleshooting**

- *Caddy can't get a certificate:* the DNS record doesn't point at the server yet, or ports 80/443 are blocked (also check your hosting provider's firewall).
- *`502 Bad Gateway`:* the bot container isn't running; look at `docker compose logs bot`, usually a typo in `.env` or `rules.yaml`.
- *Already running another web server (nginx, Apache) on 80/443:* skip `--profile https` and proxy your existing server to `http://127.0.0.1:8000` instead, forwarding the `X-Forwarded-*` headers.

Without `--profile https` (for example on your own computer) only the bot starts, on `http://localhost:8000`.

### 5. Token refresh (automatic)

Long-lived Instagram tokens expire after 60 days. The bot takes care of this itself:

- On first start it copies `IG_ACCESS_TOKEN` into its database (`data/bot.db`) as the account `IG_USER_ID` (see [Accounts](#accounts)).
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

#### Accounts

Every Instagram account the bot knows is a row in the `accounts` table (account id, username, token, expiry, active). The account from `IG_USER_ID` / `IG_ACCESS_TOKEN` in `.env` is added on start, so a normal single-account setup needs nothing extra; `.env` only *seeds* the token and stays the fallback while nothing is stored. Databases from older versions are moved over automatically. All active accounts get their tokens refreshed, paused ones are skipped. Manage them from the server:

```bash
python scripts/accounts.py list                          # accounts + token health
python scripts/accounts.py add 1784xxxx --username shop  # asks for the token (kept out of shell history)
python scripts/accounts.py pause 1784xxxx                # stop answering webhooks and refreshing
python scripts/accounts.py resume 1784xxxx
python scripts/accounts.py remove 1784xxxx
python scripts/refresh_token.py --account 1784xxxx       # refresh one stored account now
```

With Docker, prefix the commands with `docker compose exec bot`.

**Routing.** All accounts share one webhook URL. Every webhook `entry` carries the id of the Instagram account it is about, and the bot hands it to that account, which replies with its own token. Events for a paused account are ignored. If the bot serves only one account, events with an unknown id still go to it (it logs the id once, so you can put it into `IG_USER_ID`); with several accounts, events for unknown ids are ignored with a warning. To test routing locally: `python scripts/simulate.py comment "guide" --account 1784xxxx`.

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

## Dashboard

Open `https://YOUR-DOMAIN/admin` and enter your `ADMIN_TOKEN`. The dashboard shows:

- 📊 totals: flows started, comment replies, button clicks, emails, follow checks
- 📈 flows started per day for the last 14 days, and your most used flows
- 🔑 Instagram token health (days left, last refresh, last error)
- buttons to download the email list as CSV, refresh the token and send a test alert

The token is kept only in that browser tab (session storage) and the page cannot be embedded in other sites. Without `ADMIN_TOKEN` in `.env` the dashboard is turned off.

## Admin endpoints

| Endpoint | |
|---|---|
| `GET /health` | Health check for uptime monitoring |
| `GET /admin` | Dashboard page (asks for the admin token) |
| `GET /admin/stats?token=ADMIN_TOKEN` | Counts of triggers, button clicks, follow checks and emails |
| `GET /admin/activity?token=ADMIN_TOKEN&days=14` | Events per day and the most started flows |
| `GET /admin/emails.csv?token=ADMIN_TOKEN&list=waitlist` | Downloads the collected emails |
| `GET /admin/accounts?token=ADMIN_TOKEN` | Stored Instagram accounts with token health |
| `GET /admin/token?token=ADMIN_TOKEN&account=ID` | Token expiry, last refresh and last error (`account` optional, default: the `.env` account) |
| `POST /admin/token/refresh?token=ADMIN_TOKEN&account=ID` | Refreshes a token now |
| `POST /admin/alerts/test?token=ADMIN_TOKEN` | Sends a test message to your Telegram alert chat |
| `DELETE /admin/users/<user_id>?token=ADMIN_TOKEN` | Deletes everything stored about one person |
| `GET /privacy`, `GET /data-deletion` | Public privacy policy and data deletion pages |
| `POST /data-deletion` | Meta's data deletion callback |

## Project structure

```
app/
  main.py        web server: webhook + admin endpoints
  admin.html     admin dashboard (stats, token health, CSV export)
  bot.py         flow engine (comments, buttons, follow gate, email)
  rules.py       rules.yaml loader, validation, hot reload
  instagram.py   Instagram API client
  privacy.py     privacy policy, data deletion page + Meta callback
  tokens.py      auto-refreshes the access token of every account
  accounts_cli.py  scripts/accounts.py: list/add/pause/resume/remove accounts
  router.py      routes webhook events to the right account (one bot + API client each)
  alerts.py      optional Telegram alerts for errors
  storage.py     SQLite: state, dedup, emails, stats, accounts
  config.py      settings from .env
rules.yaml       your reels, keywords and flows
Caddyfile        HTTPS reverse proxy for production (docker compose --profile https)
docker-compose.yml
scripts/         accounts.py, list_media.py, refresh_token.py, simulate.py
docs/            app-review.md (Meta App Review: permission texts, screencast script)
tests/
```

## Roadmap

- [x] Web dashboard to see stats, token health and export emails
- [ ] Edit rules from the dashboard
- [ ] Multiple Instagram accounts
- [ ] Send waitlist emails to a newsletter tool (Mailchimp, Brevo, etc.)

## License

MIT
