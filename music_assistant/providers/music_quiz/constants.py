"""Constants for the Music Quiz provider."""

from __future__ import annotations

# guards applied to every AI request and response in this provider, so a wedged or
# runaway engine fails the round instead of the whole quiz
AI_QUERY_TIMEOUT_SECONDS = 30.0
MAX_AI_PROMPT_BYTES = 8192
MAX_AI_RESPONSE_BYTES = 4096
MAX_AI_RESPONSE_LINES = 32

# a track is not audible the instant playback is commanded: the stream still has to be
# resolved, encoded and buffered by the receiver. ASSUMED_AUDIO_START_LATENCY is what we
# assume when the player never reported a real position, while the MIN/MAX pair bounds a
# player-reported start relative to the play command so a stale report is discarded.
ASSUMED_AUDIO_START_LATENCY = 1.0
MIN_REPORTED_AUDIO_START_LATENCY = -1.0
MAX_REPORTED_AUDIO_START_LATENCY = 10.0
