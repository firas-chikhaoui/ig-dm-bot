# Meta App Review guide

This page has everything you need to submit ig-dm-bot to **Meta App Review**: when you need it, what to set up first, the text to paste for each permission, and a script for the screencast.

> Meta changes its review forms from time to time. The field names below match the *App Review → Permissions and features* screen of the App Dashboard for apps using **Instagram API with Instagram Login**. If a label looks different, the content still applies.

## Do you need App Review at all?

| You want to… | Access level | App Review |
|---|---|---|
| Run the bot on **your own** Instagram account (or accounts you manage and added to the app in the App Dashboard) | Standard Access | **Not needed**. Switch the app to **Live** after filling in the basic settings below. |
| Run the bot for **other people's** accounts (clients who connect their own Instagram) | Advanced Access | **Required** for every permission listed below |

Meta: *"Your app must complete App Review before it can request permissions with Advanced Access from any app user."* Advanced Access usually also requires **Business Verification** of the business portfolio that owns the app (*App settings → Basic → Verification*), so start that early; it can take a few days.

## Permissions the bot uses

Request **only** these three. Asking for permissions the app doesn't use is one of the most common reasons for rejection, so do **not** add `instagram_business_content_publish`, Human Agent or any Facebook Page permission.

| Permission | What the bot does with it | Where in the code |
|---|---|---|
| `instagram_business_basic` | Reads the connected account's id and username, lists its posts and reels so the owner can pick which ones trigger the bot, refreshes the long-lived token, and is required by the two permissions below | `scripts/list_media.py`, `app/tokens.py` |
| `instagram_business_manage_comments` | Receives `comments` webhooks for the account's posts and posts a public reply under a matching comment | `app/bot.py`, `InstagramAPI.reply_to_comment` |
| `instagram_business_manage_messages` | Sends one private reply to the commenter, continues the conversation when they tap a button or answer (`messages`, `messaging_postbacks` webhooks), and checks whether they follow the account (`is_user_follow_business`) | `InstagramAPI.send`, `InstagramAPI.is_following` |

Meta also asks for **at least one successful API call** per permission (within the last 30 days or so) before you can submit. Do a real test run first (see [Before you submit](#before-you-submit)); that makes the calls for you.

## Before you submit

Checklist for the App Dashboard. Replace `bot.yourdomain.com` with your domain.

**App settings → Basic**

- [ ] **App icon**: 1024 × 1024 px
- [ ] **Privacy Policy URL**: `https://bot.yourdomain.com/privacy` (set `PRIVACY_OPERATOR`, `PRIVACY_CONTACT` and `IG_USERNAME` in `.env` so the page shows your details)
- [ ] **User data deletion**: *Data deletion callback URL* → `https://bot.yourdomain.com/data-deletion`
- [ ] **Category**: *Business and pages* (or *Messaging*)
- [ ] **Contact email**: an address you read

**Instagram → API setup with Instagram login**

- [ ] Business login settings → **OAuth redirect URIs** contains `https://bot.yourdomain.com/connect/callback` (needed for client accounts to connect themselves)
- [ ] Webhook callback URL `https://bot.yourdomain.com/webhook`, verify token = `VERIFY_TOKEN`, subscribed to **`comments`**, **`messages`** and **`messaging_postbacks`**
- [ ] Your account is added and has a token (`IG_ACCESS_TOKEN`, or connected with the dashboard's **Connect Instagram** button), and **Allow access to messages** is on in the Instagram app (*Settings → Messages and story replies → Message controls → Connected tools*)

**The bot itself**

- [ ] Running in production over HTTPS with `DRY_RUN=false` (README → *Deploy to production*)
- [ ] `curl https://bot.yourdomain.com/health` returns `{"ok":true}`
- [ ] `rules.yaml` has a **demo reel** set up for the reviewer (see below), and a test run from a second Instagram account worked end to end: public reply, DM, button, follow check, link, email. This also makes the "successful API call" Meta requires.

### A demo setup for the reviewer

Post a reel (or use an existing post) and add a rule that is easy to trigger and clearly shows each permission:

```yaml
reels:
  - id: "REEL_MEDIA_ID"            # python scripts/list_media.py
    name: "App Review demo"
    keywords: ["demo"]
    comment_replies: ["Thanks! Check your DMs 📩"]
    flow: free_guide               # the example flow: button → follow check → link → email
```

Keep this rule and the reel online until the review is finished. If you prefer the demo to also work for reviewers who don't follow you, give `check_follow` a `not_following` step that tells them to follow and tap again (the example flow already does).

## Text to paste for each permission

Meta asks, per permission, *"Tell us how you're using this permission or feature"* and for step-by-step testing instructions. Copy these and adjust the parts in `[brackets]`.

### `instagram_business_basic`

**How we use it**

> [Your business name] uses ig-dm-bot, a self-hosted tool that automatically answers comments on our Instagram posts and reels and sends the commenter the resource they asked for by DM. We use instagram_business_basic to read the connected professional account's ID and username, to list its media (ID, caption, permalink, media type, timestamp) so the account owner can choose which posts trigger an automatic reply, and to refresh the account's long-lived access token. We do not read or store any media content or data of other accounts through this permission. It is also required by instagram_business_manage_comments and instagram_business_manage_messages, which we request for the reasons described there.

**Testing instructions**

> 1. The app runs at https://[bot.yourdomain.com]. It is connected to the Instagram professional account @[your_username].
> 2. The screencast shows the account being connected with Instagram Login and the list of media returned by the API, from which the media ID of the demo reel is copied into the configuration.
> 3. To see it working, follow the steps under instagram_business_manage_comments.

### `instagram_business_manage_comments`

**How we use it**

> When someone comments a keyword (for example "demo") on one of our posts or reels, Instagram sends us a comments webhook. Our server checks the comment against keywords that the account owner configured for that specific post and, on a match, posts one short public reply under the comment (for example "Thanks! Check your DMs") so the commenter knows a message is on its way. We only reply to comments on the connected account's own media, at most once per person per post, and never to our own comments. We store the comment ID only to avoid answering the same comment twice. We do not hide, delete or moderate comments.

**Testing instructions**

> 1. From any Instagram account, open this reel: [permalink of the demo reel]
> 2. Comment the word: demo
> 3. Within a few seconds a public reply from @[your_username] appears under your comment ("Thanks! Check your DMs 📩").
> 4. Continue with the steps under instagram_business_manage_messages to see the DM.
> Comments without the keyword, or on other posts, get no reply.

### `instagram_business_manage_messages`

**How we use it**

> After a matching comment, we send the commenter one private reply (Instagram's private replies feature) with a button, for example "Want the free guide? [Yes, send it]". Only when the person taps the button or replies does the conversation continue, always within the 24-hour messaging window that the person opened. Depending on the flow the account owner configured, the bot may then check whether the person follows our account (is_user_follow_business), send them the link to the resource they asked for, and ask, optionally, for their email address to add them to a waitlist. People can stop at any time, and can send "delete my data" to have everything we stored about them deleted immediately. We never send promotional messages outside the 24-hour window, we do not message people who have not commented or written to us first, and we store only the data described in our privacy policy (Instagram user ID, username, conversation step, and email if they choose to give it).

**Testing instructions**

> 1. After commenting "demo" on [permalink] (see instagram_business_manage_comments), open your Instagram inbox. You'll find a message request from @[your_username]: "Hey [your name]! Want the free guide?" with a button.
> 2. Tap "Yes, send it". The bot checks whether you follow @[your_username]:
>    - if you don't, it asks you to follow and shows an "I followed" button; follow the account and tap it;
>    - if you do, it sends the guide with an "Open the guide" link button.
> 3. Tap "Join the waitlist" and reply with any email address (for example reviewer@example.com). The bot confirms it was saved.
> 4. Send "delete my data". The bot deletes everything it stored about you and replies with a confirmation code.

### Data handling questions

The *Data handling* section asks who processes the data and where. For a self-hosted install the honest answers are:

- **Data processors / service providers with access to Platform Data:** your hosting provider only (for example Hetzner), since the bot runs on your server. If you turned on Telegram alerts, note that error messages are sent to your own Telegram chat (access tokens are removed from them first).
- **Country of the data:** where your server is located.
- **Requests from public authorities:** answer for your business; the software itself shares nothing.
- **Policies:** your privacy policy is `https://bot.yourdomain.com/privacy`; deletion works through the callback, the "delete my data" DM, or by asking you (README → *Privacy policy and data deletion*).

## Screencast script

Meta wants a screencast **per permission** showing the end-to-end experience, in English, with captions where something isn't obvious. The easiest way: record **one** video following this script, then upload the same file for each permission (or cut it into three clips with the timestamps you noted). Aim for 2–4 minutes in total.

**Setup for recording**

- Two screens side by side, or one after the other: the **browser** (App Dashboard / your server) on the left, a **phone or phone emulator** with a second Instagram account (the "visitor") on the right. [scrcpy](https://github.com/Genymobile/scrcpy) mirrors an Android phone onto your computer; on iPhone use the built-in screen recording and edit the clips together.
- Set the Instagram app and the browser to **English**.
- The visitor account should **not** follow your account yet, so the follow check is visible.
- Use a screen recorder that can add text (OBS, Loom, Clipchamp, iMovie). Add captions as written below; the reviewer watches without sound.
- Don't show `.env`, `ADMIN_TOKEN`, access tokens or the App Secret in the video. Blur them if they appear.

| # | Show | Caption to add | Permission |
|---|---|---|---|
| 1 | The App Dashboard → *Instagram → API setup with Instagram login*: click **Add account / Generate token**, log in with Instagram as @[your_username] and **show the permissions screen** listing the three permissions, then allow | "The account owner connects their Instagram professional account with Instagram Login and grants the permissions." | basic (all) |
| 2 | Terminal: `python scripts/list_media.py`, which prints the account's posts with their IDs. Copy the demo reel's ID into `rules.yaml` next to the keyword `demo` | "instagram_business_basic: we list the account's media so the owner can choose which post triggers the bot." | basic |
| 3 | Phone (visitor account): open the demo reel, comment `demo` | "A visitor comments the keyword on the reel." | comments |
| 4 | Phone: refresh the comments, show the public reply from your account under the comment | "instagram_business_manage_comments: the bot receives the comment webhook and posts a public reply." | comments |
| 5 | Phone: open the inbox / message requests, open the DM with the button | "instagram_business_manage_messages: the bot sends one private reply to the comment, with a button." | messages |
| 6 | Tap **Yes, send it** → bot says to follow first. Follow the account, go back, tap **I followed ✅** | "When the visitor taps the button, the bot checks whether they follow the account (is_user_follow_business)." | messages |
| 7 | The guide message with the link button appears; tap **Open the guide** briefly | "The visitor receives the resource they asked for." | messages |
| 8 | Tap **Join the waitlist**, type an email, show the confirmation | "Optional: the visitor can join a waitlist by sending their email." | messages |
| 9 | Browser: `https://bot.yourdomain.com/admin` → dashboard with the new activity; download the emails CSV | "The account owner sees the results in their own dashboard. Data stays on their server." | messages |
| 10 | Phone: send `delete my data`, show the confirmation code. Browser: open `https://bot.yourdomain.com/privacy` and `/data-deletion` | "People can delete their data at any time. Privacy policy and data deletion are public." | all |

Tips that avoid rejections:

- Show the **login and the permission consent screen** (step 1). Reviewers reject videos that start with an already connected account.
- Show the *actual* Instagram app receiving the reply and the DM. The bot's `/playground` is great for testing, but it is a simulation; don't use it in the review video.
- Keep the demo reel public and the bot running until the result is in. If the reviewer can't reproduce it, the submission is rejected.
- If it's rejected, Meta says which permission and why. Fix that part, re-record only that clip, and resubmit.

## After approval

- The permissions show **Advanced Access** in *App Review → Permissions and features*.
- Make sure the app is in **Live** mode (toggle at the top of the App Dashboard).
- You can remove the demo rule from `rules.yaml` (it reloads automatically).

## References

- [App Review for Instagram API](https://developers.facebook.com/docs/instagram-platform/app-review)
- [Instagram API with Instagram Login: messaging](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api)
- [Business Login for Instagram](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login)
