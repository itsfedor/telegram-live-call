---
name: telegram-live-call
description: "Use when calling the owner live in Telegram by voice."
version: 0.1.0
author: Fedor Molodtsov
license: MIT
metadata:
  hermes:
    tags: [telegram, voice, realtime, deepgram, elevenlabs]
---

# Telegram live voice call

Use the local `telegram-live-call` CLI to place one consented, time-limited call to the configured owner. This skill is a transport/pipeline helper, not a promise of the host agent's full tool access during calls.

## 0. Setup

1. Run `command -v telegram-live-call`. If missing, run `bash scripts/setup.sh` from this skill's directory. It installs the Python package, not accounts or credentials. Network access, Git and Python 3.13+ are required; `uv` is preferred.
2. Obtain the repo's `.env.example` and copy it to a private local `.env`; never fill it inside the skill checkout. Configure your own Telegram API credentials, distinct owner/source user IDs, speech-provider keys and voice ID. Do not request credentials in chat or put them in a public repository.
3. Ask the human to run `telegram-live-call --env-file .env login` in their own terminal. This creates a dedicated Telethon session for the assistant's Telegram **user account**. The owner and assistant must be different accounts. Never use bot credentials or copy another active session key.
4. Run `telegram-live-call --env-file .env check`. It only verifies the source and destination; it must not dial.
5. Run `telegram-live-call --env-file .env prepare` to prepare the greeting before a call. This uses paid TTS; do not generate multiple samples unless asked.

## Place one call

- Require an explicit current request from the owner. Installation is not permission for unsolicited or scheduled calls.
- Tell the owner which assistant account will call and the time limit before dialing.
- Run `telegram-live-call --env-file .env call --confirm` once. Do not add an arbitrary destination, retry automatically, answer strangers, create a cron job or use this for bulk calling.
- The default maximum is 60 seconds **including ringing**. `CALL_MAX_SECONDS` accepts 1..300; get the owner's agreement before increasing a test limit.
- To save a local transcript only with the owner's knowledge, add `--save-transcript`. Never publish its contents.
- If interrupted, wait for best-effort hangup and verify the completed call in the exact Telegram chat before claiming it ended.

## Voice conversation

The bundled pipeline uses streaming Deepgram STT, a configurable OpenRouter model and ElevenLabs TTS. The voice is explicitly configured; never substitute a different one silently. Use short replies, one question at a time, and confirm uncertain names/numbers. Speech-start/interim events can interrupt output, but interruption quality and latency depend on providers and network.

The standalone voice model has **no Notion, calendar or other tools**, and does not inherit the full host-agent session. An optional `CONTEXT_FILE` supplies a local prompt, sent to the model provider. Never claim a task was read/changed unless a separate tool actually did it. Return proposed changes to the regular chat for confirmation and execution.

## Safety and evidence

- Keep the dedicated session on one host/IP, private state directory, single process. Do not bypass session locks.
- Audio goes to Deepgram; speech text to ElevenLabs; context and conversation to OpenRouter/model provider. Explain this before sensitive use.
- Successful `play()` alone is not evidence of a working conversation. Verify inbound transcription, outbound audio and actual completed call.
- Clean provider errors and never print keys, session strings, login codes or passwords.
- No reconnect/redial is implemented. On provider failure end the test and report the real error.

Repository and docs: https://github.com/itsfedor/telegram-live-call
