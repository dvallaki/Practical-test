export type Survey = {
  survey_id: number;
  survey_name: string;
  client_name: string;
  invitations_sent: number;
};

/**
 * One row of GET /api/surveys/{id}/summary.
 *
 * These field names come straight from section A3 of the brief. If you decide
 * a field should be able to come back empty, change the type here to say so -
 * we read this file.
 */
export type WeekRow = {
  /** ISO date (YYYY-MM-DD) of the Saturday the week starts on, client local time. */
  week_start: string;
  /** completed + partial. */
  responses_started: number;
  responses_completed: number;
  /** Fraction 0..1 of the survey's invitations. null when invitations_sent is 0. */
  completion_rate: number | null;
  /** Median over completed responses. null in a week with no completions. */
  median_duration_seconds: number | null;
};

/** GET /api/surveys/{id}/summary. `weeks` is empty for a survey with no responses. */
export type SurveySummary = {
  survey_id: number;
  invitations_sent: number;
  timezone: string;
  weeks: WeekRow[];
};
