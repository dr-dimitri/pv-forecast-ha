// Ausschließlich synthetische, deterministische Testdaten; keine Modellberechnung.
export const SCENARIOS = ["sunny", "gaps", "stale", "empty", "acl", "outage", "old", "roof", "deleted-roof", "spring", "fold", "kolkata", "midnight"];
const HOURS = [0, 0, 0, 0, 0, 0, 0.1, 0.38, 0.95, 1.7, 2.45, 3.1, 3.6, 3.4, 2.9, 2.1, 1.4, 0.7, 0.22, 0.04, 0, 0, 0, 0];
const iso = (instant) => new Date(instant).toISOString();

export function fixture(scenario = "sunny", { day = "today", roof_id } = {}) {
  let start = Date.parse("2026-09-09T22:00:00Z"), hours = 24, timezone = "Europe/Berlin";
  if (scenario === "spring") { start = Date.parse("2026-03-28T23:00:00Z"); hours = 23; }
  if (scenario === "fold") { start = Date.parse("2026-10-24T22:00:00Z"); hours = 25; }
  if (scenario === "kolkata") { start = Date.parse("2026-09-09T18:30:00Z"); timezone = "Asia/Kolkata"; }
  const hour = 3_600_000, todayStart = start, todayEnd = start + hours * hour;
  const asOf = scenario === "midnight" ? todayStart : start + 13.25 * hour;
  if (day === "tomorrow") { start = todayEnd; hours = 24; }
  const end = start + hours * hour;
  const boundaries = [start];
  for (let cursor = Math.ceil((start + 1) / hour) * hour; cursor < end; cursor += hour) boundaries.push(cursor);
  boundaries.push(end);
  const intervals = boundaries.slice(0, -1).map((value, index) => ({
    start: iso(value), end: iso(boundaries[index + 1]),
    energy_kwh: roof_id ? [0, 0, 0, 0, 0, 0, 0.07, 0.2, 0.4, 0.8, 1.2, 1.4, 1.6, 1.5, 1.4, 1.1, 0.8, 0.3, 0.1, 0, 0, 0, 0, 0, 0][index] : HOURS[index % 24],
    ac_power_kw: null, is_complete: true, quality_flags: [],
  }));
  if (scenario === "gaps") {
    for (const index of [9, 10, 17]) Object.assign(intervals[index], { energy_kwh: null, is_complete: false, quality_flags: ["missing_gti"] });
  }
  const view = {
    view_version: 1, as_of: iso(asOf), timezone, plant_name: "Sonnenhaus", roofs: [{ id: "south", name: "Süddach" }, { id: "east", name: "Garage Ost" }],
    roof_id: roof_id ?? null, day, date: scenario === "fold" ? "2026-10-25" : scenario === "spring" ? "2026-03-29" : "2026-09-10",
    start: iso(start), end: iso(end), today_start: iso(todayStart), today_end: iso(todayEnd),
    summary: { today_kwh: roof_id ? 12.47 : 23.14, tomorrow_kwh: roof_id ? 14.68 : 26.9, remaining_today_kwh: roof_id ? 6.82 : 10.76 },
    intervals, complete: scenario !== "gaps", stale: scenario === "stale",
  };
  if (scenario === "empty") Object.assign(view, { intervals: [], complete: false, summary: { today_kwh: null, tomorrow_kwh: null, remaining_today_kwh: null } });
  if (scenario === "old") delete view.view_version;
  const totalIntervals = intervals.map((item, index) => {
    const complete = Date.parse(item.end) <= asOf && !(scenario === "gaps" && [8, 9, 10, 11].includes(index));
    return { start: item.start, end: item.end, energy_kwh: complete ? [0, 0, 0, 0, 0, 0, 0.08, 0.31, 0.81, 1.4, 2.5, 3.0, 3.7][index] ?? 0 : null, ac_power_kw: null, energy_complete: complete, quality_flags: complete ? [] : ["incomplete"], source_count: 2 };
  });
  const history = {
    schema_version: 1, enabled: true, running: true, window_days: 7, retention_truncated: false,
    horizons: { hourly_1h: { count_expected: 168, count_forecasts: 153, count_valid: 136, coverage: 136 / 168, mae_kwh: 0.18, bias_kwh: -0.07 } },
    current_targets: { view_version: 1, as_of: iso(asOf), timezone, horizon: "hourly_1h", label: "Jeweils 1 Stunde vorher", intervals: intervals.filter((item, index) => Date.parse(item.start) - hour <= asOf && !(scenario === "gaps" && [10, 11].includes(index))).map((item, index) => ({ ...item, energy_kwh: [0, 0, 0, 0, 0, 0, 0.05, 0.28, 0.9, 1.4, 2.8, 3.3, 3.4, 3.1, 2.6][index] ?? 0, fetched_at: iso(Date.parse(item.start) - hour - 900_000), cutoff: iso(Date.parse(item.start) - hour) })) },
  };
  if (scenario === "empty" || day === "tomorrow") history.current_targets.intervals = [];
  return {
    forecast: { schema_version: 1, fetched_at: iso(asOf - (scenario === "stale" ? 7_200_000 : 900_000)), view },
    measurement: { schema_version: 1, total_energy: { energy_kwh: scenario === "empty" ? null : scenario === "gaps" ? 5.6 : 12.4, energy_complete: !["gaps", "empty"].includes(scenario), source_count: scenario === "empty" ? 0 : 2, quality_flags: scenario === "gaps" ? ["gap"] : [] }, total_intervals: scenario === "empty" ? [] : totalIntervals },
    history,
  };
}

export function fixtureHass(scenario = "sunny", { calls = [], connection = {}, delay = 0 } = {}) {
  return {
    connection,
    entities: { "sensor.pv_prognose": { platform: "pv_forecast", config_entry_id: "demo-plant" } },
    async callWS(message) {
      calls.push(message);
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      if (scenario === "outage" && message.service === "get_forecast") throw { code: "home_assistant_error" };
      if (scenario === "deleted-roof" && message.service === "get_forecast" && message.service_data.roof_id) throw { code: "home_assistant_error", translation_key: "roof_not_found" };
      if (scenario === "acl" && message.service !== "get_forecast") throw { code: "unauthorized" };
      const data = fixture(scenario, message.service_data);
      if (message.service === "get_forecast") return { response: data.forecast };
      if (message.service === "get_measurements") return { response: data.measurement };
      if (message.service === "get_history") return { response: { ...data.history, window_days: message.service_data.days } };
      throw new Error("Unbekannte Testaktion");
    },
  };
}
