"""Letter hint helpers for the title-reveal quiz type."""

from __future__ import annotations

import math
import random
import re
import unicodedata
from itertools import zip_longest

from music_assistant.helpers.util import parse_title_and_version
from music_assistant.providers.music_quiz.models import TitleRevealHintSpeed

MIN_HINT_LETTER_COUNT = 2
MAX_HINT_TITLE_LENGTH = 60
# the first hint waits so players who recognize the song at once earn full points,
# and the last one lands before the deadline so it can still help
FIRST_HINT_FRACTION = 0.2
LAST_HINT_FRACTION = 0.85
# a keycap emoji (1️⃣, #️⃣) carries a real digit or sign, so it is removed as a whole
KEYCAP_EMOJI = re.compile("[0-9#*]\ufe0f?\u20e3")
# emoji glue: zero-width joiner, variation selectors, combining keycap and skin tones
EMOJI_COMPONENTS = re.compile("[\u200d\ufe0e\ufe0f\u20e3\U0001f3fb-\U0001f3ff]")
# space, hyphen, en and em dash, pipe, middle dot, bullet and colon
EDGE_SEPARATORS = " -\u2013\u2014|\u00b7\u2022:"
HINT_REVEAL_FRACTIONS: dict[TitleRevealHintSpeed, float] = {
    TitleRevealHintSpeed.SLOW: 0.4,
    TitleRevealHintSpeed.NORMAL: 0.6,
    TitleRevealHintSpeed.FAST: 0.75,
}


def clean_hint_title(title: str) -> str:
    """
    Return a track title without the version suffixes players cannot be expected to guess.

    :param title: Raw track title.
    """
    clean_title, _ = parse_title_and_version(strip_emoji(title))
    clean_title, _ = parse_title_and_version(clean_title, strip_for_display=True)
    return " ".join(clean_title.split())


def strip_emoji(text: str) -> str:
    """
    Return text without emoji and other pictographic symbols, which nobody can type as a guess.

    :param text: Text to clean.
    """
    text = EMOJI_COMPONENTS.sub("", KEYCAP_EMOJI.sub("", text))
    text = "".join(character for character in text if unicodedata.category(character) != "So")
    # an emoji at either end can leave a dangling separator behind ("Song - 🔥")
    return " ".join(text.split()).strip(EDGE_SEPARATORS)


def is_hidden_character(character: str) -> bool:
    """
    Return whether a title character is masked until it is revealed.

    :param character: Single title character.
    """
    return character.isalnum()


def hidden_positions(title: str) -> list[int]:
    """
    Return the indexes of the title characters that start masked.

    :param title: Cleaned title.
    """
    return [index for index, character in enumerate(title) if is_hidden_character(character)]


def is_hint_title_usable(title: str) -> bool:
    """
    Return whether a cleaned title makes a playable title-reveal round.

    :param title: Cleaned title.
    """
    return (
        len(title) <= MAX_HINT_TITLE_LENGTH
        and len(hidden_positions(title)) >= MIN_HINT_LETTER_COUNT
    )


def build_reveal_order(title: str, rng: random.Random) -> list[int]:
    """
    Return a random reveal order that spreads early hints across the title's words.

    :param title: Cleaned title.
    :param rng: Random source.
    """
    words: list[list[int]] = []
    current_word: list[int] = []
    for index, character in enumerate(title):
        if is_hidden_character(character):
            current_word.append(index)
        elif current_word:
            words.append(current_word)
            current_word = []
    if current_word:
        words.append(current_word)
    for word in words:
        rng.shuffle(word)
    rng.shuffle(words)
    return [index for layer in zip_longest(*words) for index in layer if index is not None]


def hint_count(letter_count: int, speed: TitleRevealHintSpeed) -> int:
    """
    Return how many letters are revealed over a round, never the whole title.

    :param letter_count: Number of masked letters in the title.
    :param speed: Hint speed selected for the game.
    """
    return min(math.floor(letter_count * HINT_REVEAL_FRACTIONS[speed]), letter_count - 1)


def build_hint_offsets(count: int, answer_window: float) -> list[float]:
    """
    Return the evenly spread offsets, in seconds into the round, at which hints are revealed.

    :param count: Number of hints.
    :param answer_window: Seconds the round is open for answers.
    """
    if count <= 0:
        return []
    first = answer_window * FIRST_HINT_FRACTION
    if count == 1:
        return [round(first, 3)]
    step = (answer_window * LAST_HINT_FRACTION - first) / (count - 1)
    return [round(first + step * index, 3) for index in range(count)]


def build_mask(title: str, revealed_positions: set[int]) -> list[str | None]:
    """
    Return the title with every unrevealed letter replaced by None.

    :param title: Cleaned title.
    :param revealed_positions: Indexes of the letters revealed so far.
    """
    return [
        character if not is_hidden_character(character) or index in revealed_positions else None
        for index, character in enumerate(title)
    ]
