"use client";

import { FormEvent, useState } from "react";
import { authenticatedFetch } from "../lib/api";

type Observation = {
  phase: string; available: boolean; status: string; reason?: string; source?: string;
  premium?: string | number; expiry?: string; strike?: string | number; option_type?: string;
  provider_timestamp?: string | null; source_timestamp?: string | null; ingested_at?: string;
  stale?: boolean; timestamp_kind?: string; security_id?: string;
};
type Comparison = {
  comparison: string; available: boolean; reason?: string; missing_phases?: string[];
  source_premium?: string | number; destination_premium?: string | number;
  premium_change?: string | number; percentage_change?: string | number;
  premium_multiple?: string | number; time_between_observations?: string;
  source_expiry?: string; destination_expiry?: string; source_timestamp?: string;
  destination_timestamp?: string; different_contracts?: boolean; interpretation?: string;
};
type Analysis = {
  status: string; phases: Record<string, Observation>; comparisons: Comparison[];
  data_completeness?: { available_phases: number; total_phases: number; fresh_phases: number; stale_phases: number; missing_phases: string[] };
  historical_maximum_multiple?: { available: boolean; multiple_from_first_observation?: string | number; baseline_timestamp?: string; maximum_timestamp?: string; reason?: string };
  quality?: { expiry_note?: string; synthetic_future_prices?: boolean; interpolation?: boolean };
};

const phases = ["PAST", "PRESENT", "NEXT", "FUTURE"];
const formatMoney = (value?: string | number) => value == null ? "—" : `₹${Number(value).toLocaleString("en-IN", { maximumFractionDigits: 4 })}`;
const formatTime = (value?: string | null) => value ? new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Kolkata" }).format(new Date(value)) : "No provider event timestamp";

export function PastPresentNextFuturePanel({ accessToken, apiBase }: { accessToken: string; apiBase: string }) {
  const [form, setForm] = useState({ underlying: "", strike: "", option_type: "CALL", past_expiry: "", present_expiry: "", next_expiry: "", future_expiry: "", past_from: "", past_to: "" });
  const [result, setResult] = useState<Analysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (key: keyof typeof form, value: string) => setForm((old) => ({ ...old, [key]: value }));

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true); setError(""); setResult(null);
    try {
      const query = new URLSearchParams(form);
      const response = await authenticatedFetch(`${apiBase}/analysis/past-present-next-future?${query}`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Analysis request failed.");
      setResult(body as Analysis);
    } catch (err) { setError(err instanceof Error ? err.message : "Analysis unavailable."); }
    finally { setBusy(false); }
  };

  return <section className="ppnf-section" id="analysis">
    <div className="ppnf-heading"><div><p className="eyebrow">OBSERVATION COMPARISON</p><h2>Past / Present / Next / Future</h2><p>Compare real Dhan observations for a selected underlying, strike and option side.</p></div><span className="ppnf-source">DHANHQ ONLY</span></div>
    <form className="ppnf-form" onSubmit={submit}>
      <label>Underlying<input required maxLength={40} placeholder="e.g. NIFTY" value={form.underlying} onChange={(e) => set("underlying", e.target.value.toUpperCase())} /></label>
      <label>Strike<input required type="number" min="0.00000001" step="any" value={form.strike} onChange={(e) => set("strike", e.target.value)} /></label>
      <label>Option type<select value={form.option_type} onChange={(e) => set("option_type", e.target.value)}><option value="CALL">CALL</option><option value="PUT">PUT</option></select></label>
      {phases.map((phase) => <label key={phase}>{phase} expiry<input required type="date" value={form[`${phase.toLowerCase()}_expiry` as keyof typeof form]} onChange={(e) => set(`${phase.toLowerCase()}_expiry` as keyof typeof form, e.target.value)} /></label>)}
      <label>Past observations from<input required type="date" value={form.past_from} onChange={(e) => set("past_from", e.target.value)} /></label>
      <label>Past observations through<input required type="date" value={form.past_to} onChange={(e) => set("past_to", e.target.value)} /></label>
      <button type="submit" disabled={busy}>{busy ? "Comparing provider observations…" : "Run comparison"}</button>
    </form>
    <p className="ppnf-disclaimer">Next and Future show current Dhan quotes for the selected later-expiry contracts. They are not future price estimates. Different expiries are different contracts.</p>
    {error && <p className="ppnf-error" role="alert">{error}</p>}
    {result && <div className="ppnf-results" aria-live="polite">
      <div className="ppnf-result-top"><strong>{result.status}</strong><span>{result.data_completeness?.available_phases ?? 0}/4 phases available · {result.data_completeness?.fresh_phases ?? 0} fresh · {result.data_completeness?.stale_phases ?? 0} stale</span></div>
      <div className="ppnf-phase-grid">{phases.map((phase) => {
        const observation = result.phases[phase];
        return <article className="ppnf-phase" key={phase}><div className="ppnf-card-title"><h3>{phase}</h3><span className={observation?.stale ? "is-stale" : ""}>{observation?.status ?? "UNAVAILABLE"}</span></div>
          {observation?.available ? <><strong className="ppnf-premium">{formatMoney(observation.premium)}</strong><dl>
            <div><dt>Expiry</dt><dd>{observation.expiry}</dd></div><div><dt>Provider time</dt><dd>{formatTime(observation.provider_timestamp)}</dd></div>
            {observation.timestamp_kind === "backend_received_at" && <div><dt>Response received</dt><dd>{formatTime(observation.source_timestamp)}</dd></div>}
            <div><dt>Source</dt><dd>{observation.source}</dd></div>
            {observation.security_id && <div><dt>Security ID</dt><dd>{observation.security_id}</dd></div>}
          </dl></> : <p className="ppnf-missing">{observation?.reason ?? "No matching real observation."}</p>}</article>;
      })}</div>
      <h3 className="ppnf-subheading">Required comparisons</h3>
      <div className="ppnf-comparison-grid">{result.comparisons.map((comparison) => <article className="ppnf-comparison" key={comparison.comparison}>
        <h4>{comparison.comparison}</h4>{comparison.available ? <><strong>{formatMoney(comparison.source_premium)} → {formatMoney(comparison.destination_premium)}</strong><dl>
          <div><dt>Premium change</dt><dd>{formatMoney(comparison.premium_change)}</dd></div><div><dt>Percentage</dt><dd>{Number(comparison.percentage_change).toFixed(2)}%</dd></div>
          <div><dt>Premium multiple</dt><dd>{Number(comparison.premium_multiple).toFixed(4)}×</dd></div><div><dt>Time between</dt><dd>{comparison.time_between_observations ?? "Unavailable"}</dd></div>
          <div><dt>Expiries</dt><dd>{comparison.source_expiry} → {comparison.destination_expiry}</dd></div>
        </dl><p>{comparison.interpretation}</p></> : <p className="ppnf-missing">Unavailable: {comparison.reason} {comparison.missing_phases?.join(", ")}</p>}
      </article>)}</div>
      <div className="ppnf-maximum"><strong>Historical maximum multiple</strong>{result.historical_maximum_multiple?.available ? <span>{Number(result.historical_maximum_multiple.multiple_from_first_observation).toFixed(4)}× · maximum {formatTime(result.historical_maximum_multiple.maximum_timestamp)} · baseline {formatTime(result.historical_maximum_multiple.baseline_timestamp)}</span> : <span>{result.historical_maximum_multiple?.reason ?? "Unavailable"}</span>}</div>
      <p className="ppnf-disclaimer">{result.quality?.expiry_note}</p>
    </div>}
  </section>;
}
