import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import Layout from "@/components/Layout";
import { api, VoiceAgent } from "@/lib/api";
import styles from "@/styles/dialer.module.css";

interface Customer {
  id: string;
  company: string;
  name: string;
  phone_number: string;
  notes: string;
  contact_allowed: boolean;
  opted_out: boolean;
}
interface Campaign {
  id: string;
  name: string;
  company: string;
  mode: string;
  purpose: string;
  status: string;
}
interface Job {
  id: string;
  customer_id: string;
  state: string;
  attempts: number;
  approved: boolean;
  available_at: string;
  outcome: string | null;
  error: string | null;
  call_id: string | null;
}
interface Capabilities {
  live_enabled: boolean;
  tenant_concurrency: number;
  modes: Record<string, string>;
}
interface ImportResult {
  rows: number;
  valid: number;
  errors: { row: number; error: string }[];
  created: number;
  existing: number;
  committed: boolean;
}
interface Simulation {
  capacity_without_provider_validation: number;
  jobs: {
    job_id: string;
    customer: string;
    eligible: boolean;
    reason: string;
  }[];
}
interface Report {
  states: Record<string, number>;
  attempts: number;
  note: string;
}

const input =
  "mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm text-slate-900";
const button =
  "rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 hover:bg-slate-100";
const primary = `${button} border-indigo-600 bg-indigo-600 text-white hover:bg-indigo-700`;
const card = "space-y-4 rounded-xl border border-slate-200 bg-white p-5";
const initial = {
  name: "",
  company: "",
  agent_id: "",
  mode: "progressive",
  purpose: "survey",
  timezone: "Asia/Dubai",
  calling_hours_start: "09:00",
  calling_hours_end: "18:00",
  max_concurrent_calls: 1,
  target_live_calls: 1,
  max_attempts_per_customer: 1,
  max_attempts_total: 100,
  retry_delay_hours: 24,
  approved_offer: "",
  compliance_approved: false,
};

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function DialerPage() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [campaignId, setCampaignId] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [form, setForm] = useState(initial);
  const [file, setFile] = useState<File | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [schedule, setSchedule] = useState("");
  const [history, setHistory] = useState<{ name: string; jobs: Job[] } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [customerOffset, setCustomerOffset] = useState(0);
  const [jobOffset, setJobOffset] = useState(0);
  const queueKey = useRef("");
  const refreshGeneration = useRef(0);
  const jobsGeneration = useRef(0);
  const campaign = campaigns.find((c) => c.id === campaignId);

  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current;
    const [capabilities, people, lists, voices] = await Promise.all([
      api.dialer<Capabilities>("capabilities"),
      api.dialer<Customer[]>(`customers?offset=${customerOffset}`),
      api.dialer<Campaign[]>("campaigns"),
      api.listAgents(),
    ]);
    if (generation === refreshGeneration.current) {
      setCaps(capabilities);
      setCustomers(people);
      setCampaigns(lists);
      setAgents(voices);
    }
  }, [customerOffset]);
  const refreshJobs = useCallback(async () => {
    const generation = ++jobsGeneration.current;
    if (!campaignId) {
      setJobs([]);
      setReport(null);
      return;
    }
    const [items, summary] = await Promise.all([
      api.dialer<Job[]>(`campaigns/${campaignId}/jobs?offset=${jobOffset}`),
      api.dialer<Report>(`campaigns/${campaignId}/report`),
    ]);
    if (generation === jobsGeneration.current) {
      setJobs(items);
      setReport(summary);
    }
  }, [campaignId, jobOffset]);
  useEffect(() => {
    let active = true;
    const generationRef = refreshGeneration;
    const timer = setTimeout(() => {
      void refresh().catch((e) => {
        if (active) setError(String(e.message || e));
      });
    }, 0);
    return () => {
      active = false;
      clearTimeout(timer);
      generationRef.current++;
    };
  }, [refresh]);
  useEffect(() => {
    let active = true;
    const generationRef = jobsGeneration;
    const timer = setTimeout(() => {
      void refreshJobs().catch((e) => {
        if (active) setError(String(e.message || e));
      });
    }, 0);
    return () => {
      active = false;
      clearTimeout(timer);
      generationRef.current++;
    };
  }, [refreshJobs]);

  async function run(task: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await task();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function upload(commit: boolean) {
    if (!file) return;
    const data = new FormData();
    data.append("file", file);
    const result = await api.dialer<ImportResult>(
      `customers/import?commit=${commit}`,
      data,
    );
    setImportResult(result);
    if (commit) {
      setNotice(
        `Imported ${result.created}; preserved ${result.existing} existing records. No calls started.`,
      );
      await refresh();
    }
  }
  function create(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const result = await api.dialer<Campaign>("campaigns", form);
      await refresh();
      setCampaignId(result.id);
      jobsGeneration.current++;
      setJobs([]);
      setReport(null);
      setSimulation(null);
      setJobOffset(0);
      setNotice("Campaign saved as a draft.");
    });
  }
  async function queue() {
    if (!campaignId || !selected.length) return;
    if (!queueKey.current) queueKey.current = crypto.randomUUID();
    await api.dialer(`campaigns/${campaignId}/queue`, {
      customer_ids: selected,
      event_key: queueKey.current,
      available_at: schedule ? new Date(schedule).toISOString() : null,
    });
    queueKey.current = "";
    setSelected([]);
    setNotice(
      "Customers queued. Importing or queueing does not activate a draft campaign.",
    );
    setSimulation(null);
    await refreshJobs();
  }

  return (
    <Layout>
      <div className={`${styles.root} mx-auto max-w-7xl space-y-6 pb-12`}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">AI Dialer</h1>
            <p className="text-sm text-slate-600">
              Customer workspace, controlled campaigns and scheduled follow-ups.
            </p>
          </div>
          <button
            className={button}
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await refresh();
                await refreshJobs();
              })
            }
          >
            Refresh status
          </button>
        </div>
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm">
          <strong>
            {caps?.live_enabled
              ? "Live dialing is enabled. Starting a campaign can place paid calls."
              : "Simulation rollout — live bulk calling is disabled."}
          </strong>
          <p className="mt-1">
            Uploading a list never starts calls. Booking confirmation requires
            an HIS/calendar integration; collections require verified identity
            and live ERP data. Neither workflow is enabled by this dialer alone.
          </p>
        </div>
        {error && (
          <div role="alert" className="rounded-lg bg-red-50 p-3 text-red-800">
            {error}
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="rounded-lg bg-green-50 p-3 text-green-800"
          >
            {notice}
          </div>
        )}
        <div className="grid gap-6 lg:grid-cols-2">
          <section className={card}>
            <h2 className="text-lg font-semibold">1. Import customers</h2>
            <p className="text-sm text-slate-600">
              CSV or XLSX, up to 2 MB and 5,000 rows. Required: name,
              phone_number (+country code), company. Existing records and
              opt-outs are never overwritten. Keep confidential
              medical/financial history in your source system.
            </p>
            <button
              className={button}
              onClick={() =>
                save(
                  new Blob(
                    [
                      "name,phone_number,company,external_id,language,timezone,contact_allowed,consent_reference,notes\r\n",
                    ],
                    { type: "text/csv" },
                  ),
                  "customer-template.csv",
                )
              }
            >
              Download CSV template
            </button>
            <label className="block text-sm">
              Customer file
              <input
                className={input}
                type="file"
                accept=".csv,.xlsx"
                disabled={busy}
                onChange={(e) => {
                  setFile(e.target.files?.[0] || null);
                  setImportResult(null);
                }}
              />
            </label>
            <div className="flex gap-2">
              <button
                className={button}
                disabled={busy || !file}
                onClick={() => void run(() => upload(false))}
              >
                Validate file
              </button>
              <button
                className={primary}
                disabled={
                  busy ||
                  !file ||
                  !importResult ||
                  importResult.errors.length > 0 ||
                  importResult.committed
                }
                onClick={() => void run(() => upload(true))}
              >
                Import validated customers
              </button>
            </div>
            {importResult && (
              <div className="text-sm" role="status">
                {importResult.valid} valid of {importResult.rows} rows.
                <ul className="max-h-40 overflow-auto text-red-700">
                  {importResult.errors.map((e) => (
                    <li key={e.row}>
                      Row {e.row}: {e.error}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
          <form className={card} onSubmit={create}>
            <h2 className="text-lg font-semibold">2. Configure a campaign</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-sm">
                Campaign name
                <input
                  className={input}
                  required
                  minLength={2}
                  maxLength={255}
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </label>
              <label className="text-sm">
                Company (match the import)
                <input
                  className={input}
                  required
                  maxLength={160}
                  value={form.company}
                  onChange={(e) =>
                    setForm({ ...form, company: e.target.value })
                  }
                />
              </label>
              <label className="text-sm">
                Voice agent
                <select
                  className={input}
                  required
                  value={form.agent_id}
                  onChange={(e) =>
                    setForm({ ...form, agent_id: e.target.value })
                  }
                >
                  <option value="">Select agent</option>
                  {agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-sm">
                Dialing mode
                <select
                  className={input}
                  value={form.mode}
                  onChange={(e) => setForm({ ...form, mode: e.target.value })}
                >
                  {Object.keys(caps?.modes || { progressive: "" }).map((m) => (
                    <option key={m} value={m}>
                      {m === "predictive"
                        ? "Predictive AI pacing (capacity-bounded)"
                        : m}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-sm">
                Purpose
                <select
                  className={input}
                  value={form.purpose}
                  onChange={(e) =>
                    setForm({ ...form, purpose: e.target.value })
                  }
                >
                  {[
                    "survey",
                    "reactivation",
                    "qualification",
                    "reminder",
                    "collections",
                  ].map((m) => (
                    <option key={m} value={m}>
                      {m === "collections"
                        ? "Collections — integration required"
                        : m}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-sm">
                Timezone
                <input
                  className={input}
                  required
                  value={form.timezone}
                  onChange={(e) =>
                    setForm({ ...form, timezone: e.target.value })
                  }
                />
              </label>
              <label className="text-sm">
                Calling window starts
                <input
                  className={input}
                  type="time"
                  required
                  value={form.calling_hours_start}
                  onChange={(e) =>
                    setForm({ ...form, calling_hours_start: e.target.value })
                  }
                />
              </label>
              <label className="text-sm">
                Calling window ends
                <input
                  className={input}
                  type="time"
                  required
                  value={form.calling_hours_end}
                  onChange={(e) =>
                    setForm({ ...form, calling_hours_end: e.target.value })
                  }
                />
              </label>
              {(
                [
                  ["max_concurrent_calls", "Reserved channels", 20],
                  ["target_live_calls", "Target live calls", 20],
                  ["max_attempts_total", "Total attempt budget", 10000],
                  [
                    "max_attempts_per_customer",
                    "Attempts per queued customer",
                    3,
                  ],
                ] as const
              ).map(([key, label, max]) => (
                <label key={key} className="text-sm">
                  {label}
                  <input
                    className={input}
                    type="number"
                    min={1}
                    max={max}
                    required
                    value={form[key]}
                    onChange={(e) =>
                      setForm({ ...form, [key]: Number(e.target.value) })
                    }
                  />
                </label>
              ))}
            </div>
            <p className="text-sm text-slate-600">
              {caps?.modes[form.mode]} Retries for busy/no-answer wait at least
              24 hours; preview retries require fresh approval.
            </p>
            <label className="block text-sm">
              Approved offer / reminder context
              <textarea
                className={input}
                maxLength={1000}
                value={form.approved_offer}
                onChange={(e) =>
                  setForm({ ...form, approved_offer: e.target.value })
                }
              />
            </label>
            <p className="text-xs text-slate-600">
              Use an agent with approved outbound instructions and matching
              company knowledge. Templates can use {"{{customer_name}}"},{" "}
              {"{{company_name}}"}, {"{{call_purpose}}"} and{" "}
              {"{{approved_offer}}"}.
            </p>
            <label className="flex gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.compliance_approved}
                onChange={(e) =>
                  setForm({ ...form, compliance_approved: e.target.checked })
                }
              />
              I have reviewed contact permission, applicable calling rules and
              national do-not-call requirements. Workspace DNC is enforced
              automatically.
            </label>
            <button className={primary} disabled={busy || !caps}>
              Save draft campaign
            </button>
          </form>
        </div>
        <section className={card}>
          <h2 className="text-lg font-semibold">
            3. Select customers and schedule
          </h2>
          <label className="block text-sm">
            Campaign
            <select
              className={input}
              value={campaignId}
              onChange={(e) => {
                setCampaignId(e.target.value);
                jobsGeneration.current++;
                setJobs([]);
                setReport(null);
                setSimulation(null);
                setSelected([]);
                setJobOffset(0);
                queueKey.current = "";
              }}
            >
              <option value="">Select a campaign</option>
              {campaigns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} · {c.mode} · {c.status}
                </option>
              ))}
            </select>
          </label>
          <p className="text-sm text-slate-600">
            {campaign
              ? `Company: ${campaign.company}. Only matching customers can be selected.`
              : "Choose a campaign to queue matching customers."}{" "}
            Consent reference is required before dialing.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  <th className="p-2">Select</th>
                  <th>Customer / company</th>
                  <th>Phone</th>
                  <th>Permission</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {customers.map((c) => (
                  <tr key={c.id} className="border-t">
                    <td className="p-2">
                      <input
                        type="checkbox"
                        aria-label={`Select ${c.name}`}
                        disabled={
                          !campaign ||
                          campaign.company !== c.company ||
                          c.opted_out ||
                          !c.contact_allowed ||
                          busy
                        }
                        checked={selected.includes(c.id)}
                        onChange={(e) => {
                          setSelected(
                            e.target.checked
                              ? [...selected, c.id]
                              : selected.filter((id) => id !== c.id),
                          );
                          queueKey.current = "";
                        }}
                      />
                    </td>
                    <td>
                      {c.name}
                      <div className="text-xs text-slate-500">{c.company}</div>
                    </td>
                    <td>{c.phone_number}</td>
                    <td>
                      {c.opted_out
                        ? "Opted out"
                        : c.contact_allowed
                          ? "Permission recorded"
                          : "Not granted"}
                    </td>
                    <td className="space-x-2 py-2">
                      <button
                        className={button}
                        disabled={busy}
                        onClick={() =>
                          void run(async () =>
                            setHistory({
                              name: c.name,
                              jobs: await api.dialer<Job[]>(
                                `customers/${c.id}/history`,
                              ),
                            }),
                          )
                        }
                      >
                        History
                      </button>
                      <button
                        className={button}
                        disabled={busy || c.opted_out}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Suppress calls to ${c.phone_number} throughout this workspace?`,
                            )
                          )
                            void run(async () => {
                              await api.dialer(
                                `customers/${c.id}/permission`,
                                {
                                  opted_out: true,
                                  reason: "Operator recorded customer opt-out",
                                },
                                "PATCH",
                              );
                              await refresh();
                              setSimulation(null);
                            });
                        }}
                      >
                        Opt out
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!customers.length && (
            <p className="text-sm">
              No customers on this page. Import a validated list to get started.
            </p>
          )}
          <div className="flex gap-2">
            <button
              className={button}
              disabled={busy || customerOffset === 0}
              onClick={() =>
                setCustomerOffset(Math.max(0, customerOffset - 100))
              }
            >
              Previous customers
            </button>
            <button
              className={button}
              disabled={busy || customers.length < 100}
              onClick={() => setCustomerOffset(customerOffset + 100)}
            >
              Next customers
            </button>
          </div>
          <label className="block text-sm">
            Call after (required for scheduled callbacks; shown in this
            browser’s timezone)
            <input
              className={input}
              type="datetime-local"
              value={schedule}
              onChange={(e) => {
                setSchedule(e.target.value);
                queueKey.current = "";
              }}
            />
          </label>
          <button
            className={primary}
            disabled={
              busy ||
              !campaign ||
              !selected.length ||
              (campaign.mode === "scheduled" && !schedule)
            }
            onClick={() => void run(queue)}
          >
            Queue {selected.length} selected customers
          </button>
          {campaign?.mode === "event" && (
            <p className="text-sm">
              Event ingestion: authenticated POST /api/v1/dialer/campaigns/
              {campaign.id}/queue with customer_ids, event_key and optional
              available_at. Reusing the same event key and customer prevents a
              duplicate job. Third-party connector setup is separate.
            </p>
          )}
          {history && (
            <div className="rounded-lg bg-slate-50 p-3">
              <h3 className="font-semibold">{history.name}: last 200 jobs</h3>
              <button className={button} onClick={() => setHistory(null)}>
                Close history
              </button>
              <ul className="max-h-48 overflow-auto text-sm">
                {history.jobs.map((j) => (
                  <li key={j.id}>
                    {new Date(j.available_at).toLocaleString()} · {j.state} ·{" "}
                    {j.outcome || "No outcome yet"}
                  </li>
                ))}
              </ul>
              {!history.jobs.length && <p>No call history yet.</p>}
            </div>
          )}
        </section>
        {campaign && (
          <section className={card}>
            <h2 className="text-lg font-semibold">
              4. Validate and operate · {campaign.status}
            </h2>
            <div className="flex flex-wrap gap-2">
              <button
                className={button}
                disabled={busy}
                onClick={() =>
                  void run(async () =>
                    setSimulation(
                      await api.dialer<Simulation>(
                        `campaigns/${campaignId}/simulation`,
                      ),
                    ),
                  )
                }
              >
                Simulate eligibility (no calls)
              </button>
              <button
                className={primary}
                disabled={
                  busy ||
                  !caps?.live_enabled ||
                  campaign.status === "cancelled" ||
                  campaign.status === "running"
                }
                onClick={() => {
                  if (
                    window.confirm(
                      "Start this campaign? Eligible customers may receive paid calls.",
                    )
                  )
                    void run(async () => {
                      await api.dialer(`campaigns/${campaignId}/action`, {
                        action: "start",
                      });
                      await refresh();
                    });
                }}
              >
                Start live campaign
              </button>
              {(["pause", "cancel"] as const).map((action) => (
                <button
                  key={action}
                  className={button}
                  disabled={busy || campaign.status === "cancelled"}
                  onClick={() => {
                    if (
                      action === "pause" ||
                      window.confirm(
                        "Cancel this campaign permanently? Existing connected calls will not be hung up.",
                      )
                    )
                      void run(async () => {
                        await api.dialer(`campaigns/${campaignId}/action`, {
                          action,
                        });
                        await refresh();
                        await refreshJobs();
                      });
                  }}
                >
                  {action === "pause" ? "Pause new calls" : "Cancel campaign"}
                </button>
              ))}
              <button
                className={button}
                disabled={busy}
                onClick={() =>
                  void run(async () =>
                    save(
                      await api.downloadDialerReport(campaignId),
                      "dialer-results.csv",
                    ),
                  )
                }
              >
                Export results CSV
              </button>
            </div>
            <p className="text-xs text-slate-600">
              Refresh to see current status. Scheduled calls run on the next
              queue sweep after they become eligible; the sweep interval is 30
              seconds. Pausing prevents new dispatches but does not hang up
              connected calls.
            </p>
            {report && (
              <div className="rounded-lg bg-slate-50 p-3 text-sm">
                Attempts: {report.attempts} ·{" "}
                {Object.entries(report.states)
                  .map(([state, count]) => `${state}: ${count}`)
                  .join(" · ")}
                <p className="mt-1 text-slate-600">{report.note}</p>
              </div>
            )}
            {simulation && (
              <div className="rounded-lg border border-indigo-200 p-3 text-sm">
                <strong>
                  Policy simulation only — no provider readiness or live call
                  test
                </strong>
                <p>
                  Campaign capacity estimate:{" "}
                  {simulation.capacity_without_provider_validation}. Tenant and
                  provider capacity may reduce this further.
                </p>
                <ul className="max-h-48 overflow-auto">
                  {simulation.jobs.map((j) => (
                    <li key={j.job_id}>
                      {j.customer}: {j.reason.replaceAll("_", " ")}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr>
                    <th>Customer</th>
                    <th>Due</th>
                    <th>State / result</th>
                    <th>Attempts</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.id} className="border-t">
                      <td className="py-3">
                        {customers.find((c) => c.id === j.customer_id)?.name ||
                          j.customer_id.slice(0, 8)}
                      </td>
                      <td>{new Date(j.available_at).toLocaleString()}</td>
                      <td>
                        {j.state}
                        {j.approved && " · approved"}
                        <div className="text-xs text-slate-600">
                          {j.error || j.outcome}
                        </div>
                      </td>
                      <td>{j.attempts}</td>
                      <td className="space-x-2">
                        {campaign.mode === "preview" &&
                          j.state === "queued" &&
                          !j.approved && (
                            <button
                              className={button}
                              disabled={busy}
                              onClick={() =>
                                void run(async () => {
                                  await api.dialer(`jobs/${j.id}/approve`, {});
                                  await refreshJobs();
                                  setSimulation(null);
                                })
                              }
                            >
                              Approve this call
                            </button>
                          )}
                        {["queued", "reserved", "dispatching"].includes(
                          j.state,
                        ) && (
                          <button
                            className={button}
                            disabled={busy}
                            onClick={() =>
                              void run(async () => {
                                await api.dialer(`jobs/${j.id}/cancel`, {});
                                await refreshJobs();
                                setSimulation(null);
                              })
                            }
                          >
                            Cancel queued call
                          </button>
                        )}
                        {j.call_id && (
                          <Link
                            className="text-indigo-700 underline"
                            href="/calls"
                            title={`Call ${j.call_id}`}
                          >
                            Call log
                          </Link>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex gap-2">
              <button
                className={button}
                disabled={busy || jobOffset === 0}
                onClick={() => setJobOffset(Math.max(0, jobOffset - 200))}
              >
                Previous jobs
              </button>
              <button
                className={button}
                disabled={busy || jobs.length < 200}
                onClick={() => setJobOffset(jobOffset + 200)}
              >
                Next jobs
              </button>
            </div>
          </section>
        )}
      </div>
    </Layout>
  );
}
