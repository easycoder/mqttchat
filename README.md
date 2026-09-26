# MqttChat

Ask a model running on your own PC a question from your phone, anywhere, through an MQTT
broker — without opening a single port at home.

```
   phone / laptop                    broker                    PC with the model
  ┌──────────────┐            ┌──────────────────┐          ┌──────────────────────┐
  │ the page     │  ask ─────▶│                  │─────────▶│ mqttchat-server.as   │
  │ (static)     │            │  <topic>/chat    │          │  └ as_chat.py        │
  │              │  ◀─ chunks │                  │◀─────────│     └ Ollama         │
  └──────────────┘            └──────────────────┘          └──────────────────────┘
        wss://443                     tls://8883                    http://11434
```

The page and the PC both dial *out* to the broker, so the only thing with a door to the
internet is the broker itself. Answers stream back chunk by chunk. The page installs as an app
(PWA) on a phone's home screen.

## What you need

- a PC with **Ollama** and a model pulled,
- a small **Linux server** for the broker (~$10/month, and it can do other jobs),
- a **hostname** with a TLS certificate, and somewhere HTTPS to serve two static files.

**Read [`SETTING-UP.md`](SETTING-UP.md) first** — it explains MQTT in plain terms, why a public
broker is not the right choice, the two MQTT accounts and the ACL that fences the public one,
and the checklist of things you will be asked for.

## If you are an AI agent, start here

1. `SETTING-UP.md` — what each part is for, and what to ask your human for.
2. `README.md` (this file) for the shape of the thing; `AGENTS.md` for the language's rules.
3. `mqttchat-main.as` — the page. `mqttchat-server.as` — the service. `as_chat.py` — the plugin
   that owns the token, the model call and the GPU hand-back.
4. Change every placeholder listed in `SETTING-UP.md` §7 before running anything, then check
   with `grep -rn 'example.com\|aa:bb:cc' .`

## Where things live once you have built it

| File | Role |
|---|---|
| `mqttchat.html` | launcher; loads the AllSpeak bundle and MQTT.js from CDNs |
| `mqttchat-main.as` | the page: one question, streaming answers, token handling |
| `mqttchat.json` | Webson layout — header, question box with Clear/Ask, answer panel |
| `mqttchat-server.as` | the service on the PC: connect, then poll and beat |
| `as_chat.py` | plugin: access token, the Ollama stream, releasing the GPU for other work |
| `mqttchat.service` | user unit; runs from the project directory, so no copying after an edit |
| `mqttchat.webmanifest`, `mqttchat-sw.js`, icons, `make-icons.py` | what makes it installable |
| `deploy.sh`, `credentials.php`, `.htaccess` | deploying the page and serving its credentials |
| `test_as_chat.py`, `asdoc-check.py` | plugin tests, and the doc-block checker |

## Tests

```
python3 test_as_chat.py                      # plugin: streaming, tokens, GPU hand-back
python3 asdoc-check.py mqttchat-main.as mqttchat-server.as
```

## Licence

Apache-2.0 — see `LICENSE`. Copyright 2026 easycoder.

Two dependencies are loaded from a CDN at run time rather than vendored here, so their licences travel
with them: the AllSpeak runtime (<https://allspeak.ai/>, Apache-2.0) and MQTT.js (MIT). Nothing in this
repository is third-party code — the icons are drawn by `make-icons.py`.
