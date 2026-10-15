/**
 * The one page.
 *
 * What is here: the shell, and a survey list that calls the API and handles
 * its own loading and error states. Use it as the pattern, or replace it.
 *
 * What is not here: the weekly summary table. That is section A4 of the brief,
 * and so is the delete. Loading and error states for those are graded.
 */

import { useEffect, useState } from "react";
import { ApiError, api } from "./api";
import type { Survey, SurveySummary as Summary, WeekRow } from "./types";

export function App() {
  const [surveys, setSurveys] = useState<Survey[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api<Survey[]>("/api/surveys")
      .then((data) => !cancelled && setSurveys(data))
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedSurvey = surveys?.find((s) => s.survey_id === selected);

  return (
    <main>
      <h1>Survey reporting</h1>
      <div className="layout">
        <section>
          <h2>Surveys</h2>
          {error && <p className="error">Could not load surveys: {error}</p>}
          {!error && surveys === null && <p>Loading...</p>}
          {surveys?.length === 0 && <p>No surveys. Have you run the ingest?</p>}
          <ul>
            {surveys?.map((s) => (
              <li key={s.survey_id}>
                <button
                  onClick={() => setSelected(s.survey_id)}
                  aria-pressed={s.survey_id === selected}
                >
                  {s.survey_name}
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h2>Weekly summary</h2>
          {selectedSurvey === undefined ? (
            <p>Pick a survey.</p>
          ) : (
            <SurveySummary survey={selectedSurvey} />
          )}
        </section>
      </div>
    </main>
  );
}

function SurveySummary({ survey }: { survey: Survey }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Reset on every switch, and ignore a slow response for a survey that is
    // no longer selected, so clicking quickly never shows the wrong table.
    let cancelled = false;
    setSummary(null);
    setError(null);
    api<Summary>(`/api/surveys/${survey.survey_id}/summary`)
      .then((data) => !cancelled && setSummary(data))
      .catch((e) => {
        if (cancelled) return;
        setError(
          e instanceof ApiError && e.status === 404
            ? "This survey no longer exists."
            : String(e),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [survey.survey_id]);

  return (
    <div>
      <h3>{survey.survey_name}</h3>
      <p>
        {survey.client_name} &middot;{" "}
        {survey.invitations_sent.toLocaleString()} invitations sent
      </p>
      {error && <p className="error">Could not load the summary: {error}</p>}
      {!error && summary === null && <p>Loading...</p>}
      {summary && <WeekTable summary={summary} />}
    </div>
  );
}

function WeekTable({ summary }: { summary: Summary }) {
  const { weeks, invitations_sent } = summary;
  if (weeks.length === 0) {
    return <p>No responses have been recorded for this survey.</p>;
  }
  const started = weeks.reduce((n, w) => n + w.responses_started, 0);
  const completed = weeks.reduce((n, w) => n + w.responses_completed, 0);
  return (
    <>
      {invitations_sent === 0 && (
        <p>
          No invitations are recorded for this survey, so a completion rate
          can't be calculated.
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Week (Sat&ndash;Fri)</th>
            <th className="num">Responses</th>
            <th className="num">Completed</th>
            <th className="num">Completion rate</th>
            <th className="num">Median duration</th>
          </tr>
        </thead>
        <tbody>
          {weeks.map((w: WeekRow) => (
            <tr key={w.week_start}>
              <td>{formatWeek(w.week_start)}</td>
              <td className="num">{w.responses_started}</td>
              <td className="num">{w.responses_completed}</td>
              <td className="num">{formatRate(w.completion_rate)}</td>
              <td className="num">{formatDuration(w.median_duration_seconds)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th>Total</th>
            <th className="num">{started}</th>
            <th className="num">{completed}</th>
            <th className="num">
              {formatRate(invitations_sent ? completed / invitations_sent : null)}
            </th>
            <th />
          </tr>
        </tfoot>
      </table>
      <p className="note">
        Responses = completed + partial. Completion rate = completed &divide;
        invitations sent for the whole survey. Median duration is over completed
        responses. Weeks are in {summary.timezone} time.
      </p>
    </>
  );
}

const NONE = "\u2014";

// week_start is a calendar date ("2026-01-03"). Format it in UTC so the
// viewer's own timezone can't shift it to the previous day.
const dayFormat = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

function formatWeek(isoDate: string): string {
  const start = new Date(`${isoDate}T00:00:00Z`);
  const end = new Date(start.getTime() + 6 * 24 * 60 * 60 * 1000);
  return `${dayFormat.format(start)} \u2013 ${dayFormat.format(end)}`;
}

function formatRate(rate: number | null): string {
  if (rate === null || !Number.isFinite(rate)) return NONE;
  // Two decimals: weekly rates over a whole survey's invitations are small.
  return `${(rate * 100).toFixed(2)}%`;
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return NONE;
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}
