"use client";

import { FormEvent, useEffect, useState } from "react";
import { authenticatedFetch } from "../lib/api";

type Props = { accessToken: string; apiBase: string };
type JobStatus = {
  job_id: string; status: string; sample_verified: boolean; chunks_total: number; chunks_completed: number;
  chunks_failed: number; rows_received: number; rows_upserted: number; failures: { error_code?: string; error_message?: string; range_start: string; range_end: string }[];
  coverage?: { earliest_available?: string | null; latest_available?: string | null; observations?: number; missing_data_ranges?: { from: string; to: string; missing_observations_estimate: number }[] };
};

export function HistoricalAdminPanel({ accessToken, apiBase }: Props) {
  const [symbol, setSymbol] = useState("NIFTY");
  const [kind, setKind] = useState<"underlying" | "instrument">("underlying");
  const [interval, setInterval] = useState("1");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!job?.job_id || ["succeeded", "failed", "partial"].includes(job.status)) return;
    let active = true;
    const poll = async () => {
      try {
        const response = await authenticatedFetch(`${apiBase}/admin/historical/jobs/${job.job_id}`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } });
        if (!response.ok) throw new Error("Could not refresh ingestion progress.");
        if (active) setJob(await response.json() as JobStatus);
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : "Progress service unavailable."); }
    };
    void poll();
    const timer = window.setInterval(poll, 4000);
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.job_id, job?.status, accessToken, apiBase]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setError(""); setBusy(true); setJob(null);
    try {
      const response = await authenticatedFetch(`${apiBase}/admin/historical/backfill`, {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ [kind]: symbol, interval, from_date: fromDate, to_date: toDate }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "Backfill could not be queued.");
      setJob({ ...result, sample_verified: false, chunks_completed: 0, chunks_failed: 0, rows_received: 0, rows_upserted: 0, failures: [] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Historical service unavailable."); }
    finally { setBusy(false); }
  };

  return <section className="historical-admin" aria-labelledby="historical-admin-title">
    <div className="historical-admin-heading"><div><p className="eyebrow">ADMIN · DHANHQ</p><h2 id="historical-admin-title">Historical data backfill</h2></div><span className="source-label">PROVIDER DATA ONLY</span></div>
    <form className="historical-form" onSubmit={submit}>
      <label>Lookup by<select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="underlying">Underlying</option><option value="instrument">Instrument symbol</option></select></label>
      <label>Provider symbol<input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="NIFTY or mapped symbol" required /></label>
      <label>Interval<select value={interval} onChange={(event) => setInterval(event.target.value)}><option value="1">1 minute</option><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="25">25 minutes</option><option value="60">60 minutes</option><option value="1d">Daily</option></select></label>
      <label>From date<input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} required /></label>
      <label>To date (exclusive)<input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} required /></label>
      <button disabled={busy}>{busy ? "Queueing…" : "Start Dhan backfill"}</button>
    </form>
    <p className="historical-note">The worker validates a real Dhan sample before bulk requests. Minute requests are split into at most 90-day ranges. Dhan uses an exclusive end date. Results show only returned and stored candles.</p>
    {error && <p className="auth-error" role="alert">{error}</p>}
    {job && <div className="historical-result" aria-live="polite">
      <div className="historical-job-line"><strong>{job.status.toUpperCase()}</strong><code>{job.job_id}</code></div>
      <p>{job.sample_verified ? "Real Dhan sample verified" : "Waiting for the real Dhan sample"} · {job.chunks_completed}/{job.chunks_total} chunks · {job.chunks_failed} failed · {job.rows_upserted} observations stored</p>
      {job.failures?.map((failure, index) => <p className="historical-failure" key={`${failure.range_start}-${index}`}>{failure.error_code}: {failure.error_message}</p>)}
      {job.coverage && <div className="historical-coverage"><h3>Verified coverage</h3><p>Earliest {job.coverage.earliest_available ? new Date(job.coverage.earliest_available).toLocaleString() : "—"} · Latest {job.coverage.latest_available ? new Date(job.coverage.latest_available).toLocaleString() : "—"} · {job.coverage.observations ?? 0} observations</p><p>{job.coverage.missing_data_ranges?.length ?? 0} observed gap ranges. Coverage reflects stored provider timestamps; it does not imply a five-year history.</p>{Boolean(job.coverage.missing_data_ranges?.length) && <ul className="historical-gaps">{job.coverage.missing_data_ranges?.slice(0, 20).map((gap, index) => <li key={`${gap.from}-${index}`}>{new Date(gap.from).toLocaleString()} → {new Date(gap.to).toLocaleString()} · about {gap.missing_observations_estimate} missing bars</li>)}</ul>}{(job.coverage.missing_data_ranges?.length ?? 0) > 20 && <p>Showing the first 20 gaps; the API returns up to 1,000.</p>}</div>}
    </div>}
  </section>;
}
