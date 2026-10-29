"""Hint-text answer strategy for Music Quiz: free-text guesses against a revealing mask."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import ClassVar

from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import SerializableType
from music_assistant.providers.music_quiz.answer_types.base import (
    QuizAnswerSubmission,
    QuizAnswerType,
)
from music_assistant.providers.music_quiz.constants import ASSUMED_AUDIO_START_LATENCY
from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizGuessTooFastError,
    MusicQuizInvalidAnswerError,
    MusicQuizTooManyAttemptsError,
)
from music_assistant.providers.music_quiz.hints import build_mask, is_hidden_character
from music_assistant.providers.music_quiz.matching import compare_hint_guess
from music_assistant.providers.music_quiz.models import (
    HintTextAttempt,
    HintTextRoundState,
    HintTextSolve,
    MusicQuizAnswerType,
    MusicQuizGame,
    MusicQuizPlayer,
    QuizRoundAnswerState,
)
from music_assistant.providers.music_quiz.scoring import (
    calculate_time_score,
    round_answer_window,
)

MAX_GUESS_LENGTH = 200
MAX_ATTEMPTS_PER_ROUND = 30
MIN_GUESS_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class HintTextSubmission(QuizAnswerSubmission):
    """A validated free-text guess."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.HINT_TEXT
    text: str


class HintTextAnswerType(QuizAnswerType):
    """Free-text answer strategy with progressively revealed letter hints."""

    answer_type = MusicQuizAnswerType.HINT_TEXT

    def parse_submission(self, payload: Mapping[str, object]) -> QuizAnswerSubmission:
        """
        Parse a free-text guess.

        :param payload: Raw JSON object submitted by a player.
        :return: A validated free-text guess.
        """
        if payload.keys() != {"answer_type", "text"}:
            raise MusicQuizInvalidAnswerError("Hint-text submissions require answer_type and text")
        answer_type = payload["answer_type"]
        if not isinstance(answer_type, str) or answer_type != self.answer_type:
            raise MusicQuizInvalidAnswerError("Answer type must be hint_text")
        text = payload["text"]
        if not isinstance(text, str):
            raise MusicQuizInvalidAnswerError("Guess must be a string")
        text = " ".join(text.split())
        if not text or len(text) > MAX_GUESS_LENGTH:
            raise MusicQuizInvalidAnswerError(
                f"Guess must contain between 1 and {MAX_GUESS_LENGTH} characters"
            )
        return HintTextSubmission(text=text)

    def validate_round(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> None:
        """
        Validate a hint-text round.

        :param game: Game the round belongs to.
        :param state: Prepared answer state to validate.
        """
        hint_state = self._get_state(state)
        hidden = {
            index
            for index, character in enumerate(hint_state.solution)
            if is_hidden_character(character)
        }
        if not hidden or not hint_state.accepted_answers:
            raise InvalidDataError("Round requires a solution with letters to guess")
        if set(hint_state.reveal_order) != hidden or len(hint_state.reveal_order) != len(hidden):
            raise InvalidDataError("Round reveal order does not match the solution")
        if len(hint_state.hint_offsets) >= len(hidden):
            raise InvalidDataError("Round hints must never reveal the whole solution")

    def open_round(self, state: QuizRoundAnswerState, audio_started_at: float) -> list[float]:
        """
        Anchor the round's hints to the moment its track became audible.

        :param state: Answer state of the round that started.
        :param audio_started_at: Expected timestamp at which the round's track became audible.
        """
        hint_state = self._get_state(state)
        hint_state.revealed_count = 0
        hint_state.hint_times = [audio_started_at + offset for offset in hint_state.hint_offsets]
        return list(hint_state.hint_times)

    def reveal_hint(self, state: QuizRoundAnswerState, hint_index: int) -> bool:
        """
        Uncover the letters of all hints up to and including the given one.

        :param state: Current round answer state.
        :param hint_index: Index of the hint in the list returned by open_round.
        """
        hint_state = self._get_state(state)
        revealed_count = min(hint_index + 1, len(hint_state.hint_offsets))
        if revealed_count <= hint_state.revealed_count:
            return False
        hint_state.revealed_count = revealed_count
        return True

    def submit(
        self,
        game: MusicQuizGame,
        state: QuizRoundAnswerState,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Check a player's guess, locking the player once they guessed correctly.

        :param game: Game receiving the submission.
        :param state: Current round answer state.
        :param player: Player submitting the answer.
        :param submission: Validated free-text guess.
        :param submitted_at: Server timestamp of the submission.
        """
        hint_state = self._get_state(state)
        if not isinstance(submission, HintTextSubmission):
            raise MusicQuizInvalidAnswerError("Submission does not match the hint-text answer type")
        if player.player_id in hint_state.solves:
            raise MusicQuizAlreadyAnsweredError("Player already guessed this round")
        attempts = hint_state.attempts.setdefault(player.player_id, [])
        if len(attempts) >= MAX_ATTEMPTS_PER_ROUND:
            raise MusicQuizTooManyAttemptsError("Player used all guesses for this round")
        if attempts and submitted_at - attempts[-1].submitted_at < MIN_GUESS_INTERVAL_SECONDS:
            raise MusicQuizGuessTooFastError("Player guessed again too quickly")
        is_correct, is_close = compare_hint_guess(
            submission.text,
            hint_state.accepted_answers,
            hint_state.allowed_typos,
        )
        attempts.append(
            HintTextAttempt(
                text=submission.text,
                submitted_at=submitted_at,
                is_correct=is_correct,
                is_close=is_close,
            )
        )
        if is_correct:
            hint_state.solves[player.player_id] = HintTextSolve(
                solved_at=submitted_at,
                points=self._score(game, submitted_at),
            )

    def remove_player(self, state: QuizRoundAnswerState, player_id: str) -> None:
        """
        Remove a player's guesses.

        :param state: Round answer state to mutate.
        :param player_id: Player whose guesses should be removed.
        """
        hint_state = self._get_state(state)
        hint_state.attempts.pop(player_id, None)
        hint_state.solves.pop(player_id, None)

    def is_player_complete(self, state: QuizRoundAnswerState, player_id: str) -> bool:
        """
        Return whether a player guessed correctly.

        :param state: Round answer state to inspect.
        :param player_id: Player to inspect.
        """
        return player_id in self._get_state(state).solves

    def is_round_complete(
        self,
        state: QuizRoundAnswerState,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player guessed correctly.

        :param state: Round answer state to inspect.
        :param active_players: Players eligible for the round.
        """
        hint_state = self._get_state(state)
        return bool(active_players) and all(
            player.player_id in hint_state.solves for player in active_players
        )

    def reveal(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> float | None:
        """
        Apply the points earned by correct guesses.

        :param game: Game being revealed.
        :param state: Round answer state being revealed.
        :return: Timestamp of the latest submitted guess, if any.
        """
        hint_state = self._get_state(state)
        for player_id, solve in hint_state.solves.items():
            if player := game.players.get(player_id):
                player.score += solve.points
        return max(
            (
                attempt.submitted_at
                for attempts in hint_state.attempts.values()
                for attempt in attempts
            ),
            default=None,
        )

    def serialize_game_config(self, game: MusicQuizGame) -> dict[str, SerializableType]:
        """
        Serialize hint-text game configuration.

        :param game: Game whose configuration should be serialized.
        """
        return {}

    def serialize_host_round(self, state: QuizRoundAnswerState) -> dict[str, SerializableType]:
        """
        Serialize hint-text state for the host's flat round payload.

        :param state: Round answer state to serialize.
        """
        hint_state = self._get_state(state)
        return {
            "solution": hint_state.solution,
            "allowed_typos": hint_state.allowed_typos,
            "mask": self._mask(hint_state, revealed=False),
            "revealed_count": hint_state.revealed_count,
            "hint_count": len(hint_state.hint_offsets),
            "hint_times": list(hint_state.hint_times),
            "attempts": {
                player_id: [
                    {
                        "text": attempt.text,
                        "submitted_at": attempt.submitted_at,
                        "correct": attempt.is_correct,
                        "close": attempt.is_close,
                    }
                    for attempt in attempts
                ]
                for player_id, attempts in hint_state.attempts.items()
            },
            "solves": {
                player_id: {"solved_at": solve.solved_at, "points": solve.points}
                for player_id, solve in hint_state.solves.items()
            },
        }

    def serialize_round(
        self, state: QuizRoundAnswerState, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize hint-text round state.

        :param state: Round answer state to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """
        hint_state = self._get_state(state)
        hint_count = len(hint_state.hint_offsets)
        next_hint_at = (
            hint_state.hint_times[hint_state.revealed_count]
            if not revealed and hint_state.revealed_count < len(hint_state.hint_times)
            else None
        )
        serialized_state: dict[str, SerializableType] = {
            "mask": self._mask(hint_state, revealed=revealed),
            "revealed_count": hint_state.revealed_count,
            "hint_count": hint_count,
            "next_hint_at": next_hint_at,
            "allowed_typos": hint_state.allowed_typos,
            "solved_count": len(hint_state.solves),
        }
        if revealed:
            serialized_state["solution"] = hint_state.solution
        return serialized_state

    def serialize_public_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's public guessing progress.

        :param state: Current round answer state, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {"answered": False, "solved_rank": None}
        hint_state = self._get_state(state)
        solve = hint_state.solves.get(player_id)
        serialized_state: dict[str, SerializableType] = {
            "answered": solve is not None,
            "solved_rank": self._solved_rank(hint_state, player_id),
        }
        if revealed:
            serialized_state["last_answer"] = {
                "correct": solve is not None,
                "points": solve.points if solve else 0,
                "attempt_count": len(hint_state.attempts.get(player_id, [])),
            }
        return serialized_state

    def serialize_personal_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's own guesses.

        :param state: Current round answer state, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {}
        hint_state = self._get_state(state)
        attempts = hint_state.attempts.get(player_id, [])
        solve = hint_state.solves.get(player_id)
        serialized_state: dict[str, SerializableType] = {
            "guesses": [
                {"text": attempt.text, "correct": attempt.is_correct, "close": attempt.is_close}
                for attempt in attempts
            ],
            "attempts_left": max(MAX_ATTEMPTS_PER_ROUND - len(attempts), 0),
            "solved": solve is not None,
        }
        if solve is not None:
            # a player who guessed correctly already knows the title
            serialized_state["solution"] = hint_state.solution
            serialized_state["points"] = solve.points
        return serialized_state

    @staticmethod
    def _mask(hint_state: HintTextRoundState, *, revealed: bool) -> list[SerializableType]:
        """Return the masked solution, or the whole solution once revealed."""
        if revealed:
            return list(hint_state.solution)
        return list(
            build_mask(
                hint_state.solution,
                set(hint_state.reveal_order[: hint_state.revealed_count]),
            )
        )

    @staticmethod
    def _solved_rank(hint_state: HintTextRoundState, player_id: str) -> int | None:
        """Return the 1-based position of a player among the correct guessers."""
        if player_id not in hint_state.solves:
            return None
        ordered = sorted(hint_state.solves, key=lambda item: hint_state.solves[item].solved_at)
        return ordered.index(player_id) + 1

    @staticmethod
    def _score(game: MusicQuizGame, submitted_at: float) -> int:
        """Return the points of a correct guess, measured from when the track became audible."""
        if game.current_round_index is None:
            return calculate_time_score(0, 0)
        current_round = game.rounds[game.current_round_index]
        started_at = current_round.started_at or submitted_at
        answer_window = round_answer_window(game.config.answer_duration, current_round.duration)
        audible_window = max(answer_window - ASSUMED_AUDIO_START_LATENCY, 0.0)
        elapsed = submitted_at - started_at - ASSUMED_AUDIO_START_LATENCY
        return calculate_time_score(elapsed, audible_window)

    def _get_state(self, state: QuizRoundAnswerState) -> HintTextRoundState:
        """Return validated hint-text round state."""
        if state.answer_type != self.answer_type or not isinstance(state, HintTextRoundState):
            raise InvalidDataError("Round state does not match the hint-text answer type")
        return state
