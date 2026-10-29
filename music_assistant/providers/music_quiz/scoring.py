"""Scoring helpers for the Music Quiz provider."""

from __future__ import annotations


def calculate_linear_scores(
    correct_answer_order: list[str], max_points: int = 1000
) -> dict[str, int]:
    """
    Calculate linearly descending scores for correct answers.

    :param correct_answer_order: Player IDs ordered by correct answer time.
    :param max_points: Points awarded to the first correct answer.
    """
    correct_count = len(correct_answer_order)
    if correct_count == 0:
        return {}
    return {
        player_id: round(max_points * (correct_count - index) / correct_count)
        for index, player_id in enumerate(correct_answer_order)
    }


def calculate_time_score(
    elapsed: float,
    answer_window: float,
    max_points: int = 1000,
    min_points: int = 100,
) -> int:
    """
    Calculate a score that descends linearly with the time taken to answer correctly.

    :param elapsed: Seconds between the answer opening and the correct answer.
    :param answer_window: Total seconds the answer was open.
    :param max_points: Points awarded to an instant correct answer.
    :param min_points: Points awarded to a correct answer at the deadline.
    """
    if answer_window <= 0:
        return max_points
    remaining_fraction = 1 - min(max(elapsed / answer_window, 0.0), 1.0)
    return round(min_points + (max_points - min_points) * remaining_fraction)


def round_answer_window(answer_duration: float, track_duration: float | None) -> float:
    """
    Return the effective answering window of a round in seconds.

    :param answer_duration: Configured answer duration.
    :param track_duration: Duration of the round's track, if known.
    """
    answer_window = float(answer_duration)
    if track_duration and track_duration > 0:
        answer_window = min(answer_window, track_duration)
    return answer_window
