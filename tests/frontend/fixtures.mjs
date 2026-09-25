// Ausschließlich synthetische, deterministische Testdaten; keine Modellberechnung.
export const SCENARIOS = ["partial-outlook", "offset-outlook", "unresolved-outlook", "derived-outlook", "derived-energy", "offset-measurements", "stale-measurement", "restored","no-source", "no-measurement", "zero", "shading", "horizon","sunny", "gaps", "stale", "empty", "acl", "outage", "old", "roof", "deleted-roof", "spring", "fold", "kolkata", "midnight", "planning-unavailable"];
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
    roof_id: roof_id ?? null, day, date: iso(Date.parse(`${scenario === "fold" ? "2026-10-25" : scenario === "spring" ? "2026-03-29" : "2026-09-10"}T12:00:00Z`) + (day === "tomorrow" ? 86400000 : 0)).slice(0, 10),
    start: iso(start), end: iso(end), today_start: iso(todayStart), today_end: iso(todayEnd),
    summary: { today_kwh: roof_id ? 12.47 : 23.14, tomorrow_kwh: roof_id ? 14.68 : 26.9, remaining_today_kwh: roof_id ? 6.82 : 10.76 },
    intervals, complete: scenario !== "gaps", stale: ["stale", "restored"].includes(scenario),
  };
  if (scenario === "empty") Object.assign(view, { intervals: [], complete: false, summary: { today_kwh: null, tomorrow_kwh: null, remaining_today_kwh: null } });
  if (scenario === "old") delete view.view_version;
  const totalIntervals = intervals.map((item, index) => {
    const complete = Date.parse(item.end) <= asOf && !(scenario === "gaps" && [8, 9, 10, 11].includes(index));
    return { start: item.start, end: item.end, energy_kwh: complete ? [0, 0, 0, 0, 0, 0, 0.08, 0.31, 0.81, 1.4, 2.5, 3.0, 3.7][index] ?? 0 : null, ac_power_kw: null, energy_complete: complete, quality_flags: complete ? [] : ["incomplete"], source_count: 2 };
  });
  if (["derived-energy", "derived-outlook"].includes(scenario)) for (const item of totalIntervals) item.quality_flags.push("derived_energy");
  if (scenario === "offset-measurements") {
    for (const item of totalIntervals) if (item.energy_kwh > 0) {
      // Entspricht ganzen Zählerdifferenzen innerhalb versetzter Stundenränder.
      Object.assign(item, { observed_energy_kwh: item.energy_kwh, energy_kwh: null, energy_complete: false, quality_flags: ["boundary_gap", "incomplete"] });
    }
  }
  if (scenario === "shading") view.horizon_shading = { rule_version: 1, active: true, experimental: true, measured_improvement: "unavailable" };
  if (scenario === "horizon") view.daily_forecasts = Array.from({ length: 7 }, (_, i) => ({ date: `2026-09-${10 + i}`, energy_kwh: i === 3 ? null : [24, 25, 18, 0, 30, 27, 22][i], tendency: i >= 2, quality_flags: i === 2 ? ["gti_fallback"] : [] }));
  const available = !["partial-outlook", "no-source", "no-measurement", "gaps", "empty", "stale", "restored"].includes(scenario);
  const forecastEnd = todayEnd + (scenario === "horizon" ? 6 : 1) * 24 * hour;
  const forecastBoundaries = [todayStart];
  for (let cursor = Math.ceil((todayStart + 1) / hour) * hour; cursor < forecastEnd; cursor += hour) forecastBoundaries.push(cursor);
  forecastBoundaries.push(forecastEnd);
  const outlook = { ...(scenario === "partial-outlook" ? { estimate: { schema_version: 1, status: "available", basis: "measurements_and_forecast", measured_kwh: 5.6, estimated_past_kwh: 4.8, remaining_kwh: 10.76, total_kwh: 21.16, forecast_stale: false, forecast_quality_flags: [], measurement_coverage_seconds: 18000 } } : {}), schema_version: 1, status: available ? "available" : "unavailable", reason: available ? null : "incomplete_measurements", as_of: iso(asOf), timezone, measured_until: ["partial-outlook", "no-source", "no-measurement", "empty"].includes(scenario) ? null : iso(asOf - (scenario === "stale-measurement" ? 6 * hour : 900_000)), measured_kwh: scenario === "stale-measurement" ? 5.4 : 12.1, bridge_kwh: scenario === "stale-measurement" ? 12 : 0.31, remaining_kwh: 10.76, total_kwh: scenario === "stale-measurement" ? 28.16 : 23.17, measurement_age_minutes: ["partial-outlook", "no-source", "no-measurement", "empty"].includes(scenario) ? null : scenario === "stale-measurement" ? 360 : 15, measurement_stale: scenario === "stale-measurement", measurement_quality_flags: scenario === "stale-measurement" ? ["stale"] : [], forecast_quality_flags: [], quality_flags: scenario === "stale-measurement" ? ["stale"] : [] };
  if (scenario === "derived-outlook") Object.assign(outlook, {
    measurement_quality_flags: ["derived_energy"], quality_flags: ["derived_energy"],
    estimate: { schema_version: 1, status: "available", reason: null, basis: "measurements_and_forecast", measured_kwh: 12.1, estimated_past_kwh: 0.31, remaining_kwh: 10.76, total_kwh: 23.17, forecast_stale: false, forecast_quality_flags: [], measurement_coverage_seconds: 46800 },
  });
  if (scenario === "unresolved-outlook") Object.assign(outlook, {
    status: "unavailable", reason: "unresolved_measurement_identity", measured_until: null, measured_kwh: null, bridge_kwh: null, total_kwh: null, measurement_age_minutes: null,
    estimate: { schema_version: 1, status: "available", reason: null, basis: "forecast_only", measured_kwh: null, estimated_past_kwh: 12.38, remaining_kwh: 10.76, total_kwh: 23.14, forecast_stale: false, forecast_quality_flags: [], measurement_coverage_seconds: 0 },
  });
  if (scenario === "offset-outlook") Object.assign(outlook, {
    status: "unavailable", reason: "no_common_measurement_boundary", measured_until: null, measured_kwh: null, bridge_kwh: null, total_kwh: null, measurement_age_minutes: null,
    estimate: { schema_version: 1, status: "available", reason: null, basis: "forecast_only", measurement_fallback_reason: "no_common_measurement_boundary", measured_kwh: null, estimated_past_kwh: 12.38, remaining_kwh: 10.76, total_kwh: 23.14, forecast_stale: false, forecast_quality_flags: [], measurement_coverage_seconds: 0 },
  });
  return {
    forecast: {
      schema_version: 1, origin: scenario === "restored" ? "restored" : "live", restored_at: scenario === "restored" ? iso(asOf) : null, last_update_success: scenario !== "restored", fetched_at: iso(asOf - (["stale", "restored"].includes(scenario) ? 7_200_000 : 900_000)), view,
      intervals: forecastBoundaries.slice(0, -1).map((value, index) => ({ start: iso(value), end: iso(forecastBoundaries[index + 1]), energy_kwh: 1, ac_power_kw: 1, is_complete: true, quality_flags: [] })),
      planning: { schema_version: 1, status: available && scenario !== "planning-unavailable" ? "available" : "unavailable", reason: ["stale", "restored"].includes(scenario) ? "stale_forecast" : "no_energy", as_of: iso(asOf), fetched_at: iso(asOf - 900_000), timezone, start: iso(todayStart + 14 * hour), end: iso(todayStart + 16 * hour), energy_kwh: 5.87, basis: "current_forecast", assumption: "constant_interval_mean_power", quality_flags: [], hysteresis_applied: false },
    },
    measurement: {
      schema_version: 1, total_energy: { energy_kwh: ["empty", "no-source", "no-measurement"].includes(scenario) ? null : scenario === "zero" ? 0 : scenario === "gaps" ? 5.6 : 12.4, energy_complete: !["gaps", "empty", "no-source", "no-measurement"].includes(scenario), source_count: ["empty", "no-source"].includes(scenario) ? 0 : 2, quality_flags: ["derived-energy", "derived-outlook"].includes(scenario) ? ["derived_energy"] : scenario === "gaps" ? ["gap"] : [] }, total_intervals: ["empty", "no-source", "no-measurement"].includes(scenario) ? [] : totalIntervals,
      outlook,
    },
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
      if (message.service === "get_forecast") {
        if (message.service_data.include_explanation) data.forecast.explanation = explanationFixture(data.forecast.view);
        data.forecast.view.day_views = Object.fromEntries(["today", "tomorrow"].map((day) => {
          const view = fixture(scenario, { ...message.service_data, day }).forecast.view;
          return [day, Object.fromEntries(["day", "date", "start", "end", "intervals", "complete", "stale"].map((key) => [key, view[key]]))];
        }));
        return { response: data.forecast };
      }
      if (message.service === "get_measurements") return { response: data.measurement };
      throw new Error("Unbekannte Testaktion");
    },
  };
}

export function explanationFixture(view) {
  const intervals = view.intervals.map(item => ({start: item.start, end: item.end, before_clipping_kwh: item.energy_kwh === null ? null : item.energy_kwh + 0.2, group_clipping_kwh: 0.1, total_clipping_kwh: 0.1, effective_kwh: item.energy_kwh}));
  return {schema_version: 2, scope: "total", status: "available", date: view.date, timezone: view.timezone, start: view.start, end: view.end, origin: "live", fetched_at: view.as_of, last_update_success: true, intervals, totals: {before_clipping_kwh: 30, group_clipping_kwh: 2, total_clipping_kwh: 3, effective_kwh: 25}, quality_flags: [], assumptions: ["temperature_model", "configured_efficiency", "horizon_profile"]};
}
