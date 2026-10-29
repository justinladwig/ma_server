"""Tests for the Music Quiz hint-text answer strategy."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.answer_types.hint_text import (
    MAX_ATTEMPTS_PER_ROUND,
    MAX_GUESS_LENGTH,
    HintTextAnswerType,
    HintTextSubmission,
)
from music_assistant.providers.music_quiz.constants import ASSUMED_AUDIO_START_LATENCY
from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizGuessTooFastError,
    MusicQuizInvalidAnswerError,
    MusicQuizTooManyAttemptsError,
)
from music_assistant.providers.music_quiz.models import (
    HintTextRoundState,
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizGame,
    MusicQuizPlayer,
    MusicQuizRound,
)

ANSWER_TYPE = HintTextAnswerType()
STARTED_AT = 100.0


def _state() -> HintTextRoundState:
    """Return hint-text round state for "Hey Jude"."""
    return HintTextRoundState(
        solution="Hey Jude",
        accepted_answers=["heyjude"],
        allowed_typos=1,
        reveal_order=[0, 4, 1, 5, 2, 6, 7],
        hint_offsets=[5.0, 10.0, 15.0],
    )


def _game(state: HintTextRoundState) -> MusicQuizGame:
    """Return a started one-round game with two players."""
    game = MusicQuizGame(
        config=MusicQuizConfig(answer_duration=31),
        quiz_type="title_reveal",
        answer_type=MusicQuizAnswerType.HINT_TEXT,
    )
    for name in ("alice", "bob"):
        game.players[name] = MusicQuizPlayer(
            player_id=name, name=name, joined_at=0, active_from_round=0
        )
    game.rounds.append(
        MusicQuizRound(
            round_index=0,
            answer_label="The Beatles - Hey Jude",
            answer_state=state,
            started_at=STARTED_AT,
            duration=180.0,
        )
    )
    game.current_round_index = 0
    ANSWER_TYPE.validate_round(game, state)
    return game


def _guess(game: MusicQuizGame, player_id: str, text: str, at: float) -> None:
    """Submit a guess for a player."""
    ANSWER_TYPE.submit(
        game,
        game.rounds[0].answer_state,
        game.players[player_id],
        HintTextSubmission(text=text),
        at,
    )


def test_parse_submission_collapses_whitespace() -> None:
    """Parse the wire payload into a typed guess with normalized spacing."""
    submission = ANSWER_TYPE.parse_submission({"answer_type": "hint_text", "text": "  Hey   Jude "})

    assert submission == HintTextSubmission(text="Hey Jude")


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "Hey Jude"},
        {"answer_type": "hint_text"},
        {"answer_type": "multiple_choice", "text": "Hey Jude"},
        {"answer_type": "hint_text", "text": 1},
        {"answer_type": "hint_text", "text": "   "},
        {"answer_type": "hint_text", "text": "a" * (MAX_GUESS_LENGTH + 1)},
        {"answer_type": "hint_text", "text": "Hey Jude", "extra": True},
    ],
)
def test_parse_submission_rejects_malformed_payload(payload: dict[str, object]) -> None:
    """Reject missing, mismatched, empty, oversized and extra fields."""
    with pytest.raises(MusicQuizInvalidAnswerError):
        ANSWER_TYPE.parse_submission(payload)


def test_hints_uncover_letters_in_reveal_order() -> None:
    """Anchor hints to the audible start and uncover one letter per hint."""
    state = _state()
    _game(state)

    hint_times = ANSWER_TYPE.open_round(state, STARTED_AT + 1)
    assert hint_times == [106.0, 111.0, 116.0]
    assert ANSWER_TYPE.serialize_round(state, revealed=False)["next_hint_at"] == 106.0

    assert ANSWER_TYPE.reveal_hint(state, 1) is True
    assert ANSWER_TYPE.reveal_hint(state, 0) is False
    hidden = ANSWER_TYPE.serialize_round(state, revealed=False)

    assert hidden["mask"] == ["H", None, None, " ", "J", None, None, None]
    assert hidden["revealed_count"] == 2
    assert hidden["next_hint_at"] == 116.0
    assert "solution" not in hidden


def test_round_serialization_reveals_the_solution_only_after_reveal() -> None:
    """Expose the whole title only once the round is revealed."""
    state = _state()

    revealed = ANSWER_TYPE.serialize_round(state, revealed=True)

    assert revealed["solution"] == "Hey Jude"
    assert revealed["mask"] == list("Hey Jude")
    assert revealed["next_hint_at"] is None


def test_wrong_guesses_stay_private_and_correct_guess_locks_the_player() -> None:
    """Keep guess texts out of public state and lock a player after a correct guess."""
    state = _state()
    game = _game(state)

    _guess(game, "alice", "Hey Joe", 102.0)
    _guess(game, "alice", "hey jud", 103.0)

    public = ANSWER_TYPE.serialize_public_player(state, "alice", revealed=False)
    personal = ANSWER_TYPE.serialize_personal_player(state, "alice", revealed=False)
    assert public == {"answered": True, "solved_rank": 1}
    assert personal["guesses"] == [
        {"text": "Hey Joe", "correct": False, "close": True},
        {"text": "hey jud", "correct": True, "close": False},
    ]
    assert personal["solution"] == "Hey Jude"
    assert personal["attempts_left"] == MAX_ATTEMPTS_PER_ROUND - 2
    assert ANSWER_TYPE.serialize_personal_player(state, "bob", revealed=False) == {
        "guesses": [],
        "attempts_left": MAX_ATTEMPTS_PER_ROUND,
        "solved": False,
    }
    with pytest.raises(MusicQuizAlreadyAnsweredError):
        _guess(game, "alice", "Hey Jude", 104.0)


def test_guesses_are_rate_limited_and_capped() -> None:
    """Reject rapid-fire guesses and guesses beyond the per-round limit."""
    state = _state()
    game = _game(state)

    _guess(game, "bob", "wrong", 101.0)
    with pytest.raises(MusicQuizGuessTooFastError):
        _guess(game, "bob", "wrong", 101.2)
    for index in range(1, MAX_ATTEMPTS_PER_ROUND):
        _guess(game, "bob", "wrong", 101.0 + index)
    with pytest.raises(MusicQuizTooManyAttemptsError):
        _guess(game, "bob", "Hey Jude", 200.0)


def test_earlier_correct_guesses_score_more_points() -> None:
    """Score correct guesses by time and complete the round once everyone solved it."""
    state = _state()
    game = _game(state)
    players = list(game.players.values())

    _guess(game, "alice", "Hey Jude", STARTED_AT + ASSUMED_AUDIO_START_LATENCY)
    assert ANSWER_TYPE.is_round_complete(state, players) is False
    _guess(game, "bob", "Hey Jude", STARTED_AT + 16.0)
    assert ANSWER_TYPE.is_round_complete(state, players) is True

    assert ANSWER_TYPE.reveal(game, state) == STARTED_AT + 16.0
    assert game.players["alice"].score == 1000
    assert game.players["bob"].score == 550
    assert ANSWER_TYPE.serialize_public_player(state, "bob", revealed=True) == {
        "answered": True,
        "solved_rank": 2,
        "last_answer": {"correct": True, "points": 550, "attempt_count": 1},
    }


def test_remove_player_drops_guesses() -> None:
    """Remove a leaving player's guesses and solve."""
    state = _state()
    game = _game(state)
    _guess(game, "alice", "Hey Jude", 102.0)

    ANSWER_TYPE.remove_player(state, "alice")

    assert state.attempts == {}
    assert state.solves == {}


def test_validate_round_rejects_hints_that_reveal_the_whole_title() -> None:
    """Refuse a round whose hints would uncover every letter."""
    state = _state()
    state.hint_offsets = [1.0] * len(state.reveal_order)

    with pytest.raises(InvalidDataError, match="never reveal"):
        _game(state)
