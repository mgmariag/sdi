from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from database import get_connection


class ActuationRepository:
    """Persistence boundary for planned irrigation actuator work."""

    def due(self, limit: int = 100) -> list[dict[str, Any]]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    ia.*,
                    p.pot_code,
                    p.label AS pot_label
                FROM irrigation_actuations ia
                JOIN pots p ON p.id = ia.pot_id
                WHERE ia.status = 'planned'
                  AND ia.scheduled_start_at <= now()
                ORDER BY ia.scheduled_start_at, ia.id
                LIMIT %(limit)s
                """,
                {"limit": limit},
            ).fetchall()
            return rows

    def mark_completed(self, actuation_id: int, actuator_node: str) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                UPDATE irrigation_actuations
                SET status = 'completed',
                    actuator_node = %(actuator_node)s,
                    started_at = COALESCE(started_at, now()),
                    completed_at = now(),
                    delivered_volume_ml = planned_volume_ml,
                    last_error = NULL,
                    changed_at = now()
                WHERE id = %(id)s
                  AND status = 'planned'
                RETURNING *
                """,
                {"id": actuation_id, "actuator_node": actuator_node},
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE irrigation_events
                    SET status = 'completed',
                        changed_at = now()
                    WHERE id = %(event_id)s
                      AND status = 'planned'
                    """,
                    {"event_id": row["event_id"]},
                )
            conn.commit()
            return row

    def mark_failed(self, actuation_id: int, actuator_node: str, error: str) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                UPDATE irrigation_actuations
                SET status = 'failed',
                    actuator_node = %(actuator_node)s,
                    last_error = %(error)s,
                    changed_at = now()
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": actuation_id, "actuator_node": actuator_node, "error": error},
            ).fetchone()
            conn.commit()
            return row

    def summary(self) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT status, count(*) AS count
                FROM irrigation_actuations
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            return {"actuations": rows}
