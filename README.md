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
| `require_follow` | Goes to `ok` if the user follows you, otherwise to `not_following`. |
| `ask_email` | Asks for an email, validates it, saves it to `list`, then sends `success` and optionally goes to `next`. |

> ⚠️ Instagram allows only **one** private reply per comment. The first DM of a flow should therefore be a message **with a button**: when the user taps it, the conversation opens and the bot can continue. If a flow starts with a follow check or email step, the bot sends a short opener with a button automatically.

## Setup

### 1. Meta / Instagram app

1. Your Instagram account must be a **Professional** account (Business or Creator).
2. On [developers.facebook.com](https://developers.facebook.com), create an app and add the **Instagram** product. Use **API setup with Instagram login**.
3. Add your Instagram account and generate an **access token**. This is the `IG_ACCESS_TOKEN`.
   Permissions needed: `instagram_business_basic`, `instagram_business_manage_messages`, `instagram_business_manage_comments`.
4. Copy the **Instagram app secret**. This is the `APP_SECRET`.
5. In the Instagram app on your phone, go to *Settings → Messages and story replies → Message controls → Connected tools* and turn on **Allow access to messages**.

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

### 5. Token refresh

Long-lived Instagram tokens expire after 60 days. To get a new one, run:

```bash
python scripts/refresh_token.py
```

Then update `.env` and run `docker compose restart`. A monthly cron job works well for this.

## Admin endpoints

| Endpoint | |
|---|---|
| `GET /health` | Health check for uptime monitoring |
| `GET /admin/stats?token=ADMIN_TOKEN` | Counts of triggers, button clicks, follow checks and emails |
| `GET /admin/emails.csv?token=ADMIN_TOKEN&list=waitlist` | Downloads the collected emails |

## Project structure

```
app/
  main.py        web server: webhook + admin endpoints
  bot.py         flow engine (comments, buttons, follow gate, email)
  rules.py       rules.yaml loader, validation, hot reload
  instagram.py   Instagram API client
  storage.py     SQLite: state, dedup, emails, stats
  config.py      settings from .env
rules.yaml       your reels, keywords and flows
scripts/         list_media.py, refresh_token.py
tests/
```

## Roadmap

- [ ] Web dashboard (frontend) to edit rules and see stats
- [ ] Multiple Instagram accounts
- [ ] Send waitlist emails to a newsletter tool (Mailchimp, Brevo, etc.)

## License

MIT
