# After-action report: card text + timeline image + thumbnails + contact-sheet links

*Suggested by Claude, 2026-11-13, at Jeremy's request. To file in the Notion
queue (Safety & alerts / Dashboard, P2) when the Notion connector is back;
the number PS-254 is provisional.*

**Why:** Jeremy: "I want the images and the text". The PS-166 morning card is
text only and the Pushover cuts it to about 1,000 characters, so most of it
never reaches the phone. The nightly email depended on a Claude session and
its Gmail connector (both failed on 2026-11-12).

## Scope (built)
- `scheduler/after_action.py`: build(cfg, night) = the full PS-166 card, the
  night timeline bars drawn server-side with Pillow (night_timeline rows +
  kept / rejected sub spans per rig), a few thumbnails (best kept sub per rig
  and target by HFR, one typical reject per rig with its reason), and every
  goal's links (the progression contact sheets) from the project store.
- `GET /report/{night}`: the page (images inline, phone-friendly over Tailscale).
- `GET /api/runs/{night}/timeline.png`, `POST /api/report/{night}/send` (manual).
- Loop: once per night, after_action_after_shutdown_min (40) after the dawn
  shutdown. after_action_mode: push (default: Pushover with the timeline
  attached + the page link) | email (SMTP, images inline) | both | off.
- Email: smtp.gmail.com:587 with a Google app password
  (after_action_smtp_user / _password / _email_to on the System page). No
  Twilio / SendGrid.

## Open
- Jeremy: create a Gmail app password for the sending account and enter it,
  then set after_action_mode=both.
- Claude's morning reply (stacks, contact-sheet updates, tonight's changes)
  as a reply in the same thread: a desktop scheduled task.
