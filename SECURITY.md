# Security and privacy

This is an experimental, manual, single-owner call bridge, not an autodialer.
Do not use it to call people who have not consented.

- Use a dedicated assistant Telegram **user account**, not a bot and not the owner's account.
- Keep its dedicated Telethon session on one machine. Never reuse an active auth key from another host/IP.
- The source account and destination must match the configured IDs. No per-call arbitrary target is accepted.
- `call` requires `--confirm`. There is no background service, scheduled calling, incoming-call answering or automatic redial.
- The call has a hard time limit and best-effort hangup in `finally`. Network failure, SIGKILL or host failure can still prevent graceful cleanup.
- Protect `.env` and the session file (mode 0600), and never attach them to an issue.
- Voice audio is sent to Deepgram, reply text to ElevenLabs, and conversation/context to OpenRouter and its model provider. Read their data policies before using sensitive content.
- The standalone voice model has no tools or access to your agent's task systems. It cannot edit Notion or your calendar.
- Transcript persistence is opt-in. Do not commit recordings, logs, prompts or transcripts.
- Dependencies retain their own licenses, including LGPL for PyTgCalls/NTgCalls. They are installed from package indexes, not vendored here.

For a vulnerability, use GitHub private vulnerability reporting if available. Otherwise open an issue containing only a request for a private contact channel; do not include an exploit or secrets.
