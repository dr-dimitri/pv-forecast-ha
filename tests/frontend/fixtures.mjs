// Ausschließlich synthetische, deterministische Testdaten; keine Modellberechnung.
export const SCENARIOS = ["restored","no-source", "no-measurement", "archive-off", "archive-empty", "zero", "shading", "horizon","underperformance", "sunny", "gaps", "stale", "empty", "acl", "outage", "old", "roof", "deleted-roof", "spring", "fold", "kolkata", "midnight", "experience", "planning-unavailable"];
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
    intervals, complete: scenario !== "gaps", stale: ["stale", "restored"].includes(scenario),
  };
  if (scenario === "empty") Object.assign(view, { intervals: [], complete: false, summary: { today_kwh: null, tomorrow_kwh: null, remaining_today_kwh: null } });
  if (scenario === "old") delete view.view_version;
  const totalIntervals = intervals.map((item, index) => {
    const complete = Date.parse(item.end) <= asOf && !(scenario === "gaps" && [8, 9, 10, 11].includes(index));
    return { start: item.start, end: item.end, energy_kwh: complete ? [0, 0, 0, 0, 0, 0, 0.08, 0.31, 0.81, 1.4, 2.5, 3.0, 3.7][index] ?? 0 : null, ac_power_kw: null, energy_complete: complete, quality_flags: complete ? [] : ["incomplete"], source_count: 2 };
  });
  if (scenario === "shading") view.horizon_shading = { rule_version: 1, active: true, experimental: true, measured_improvement: "unavailable" };
  if (scenario === "horizon") view.daily_forecasts = Array.from({ length: 7 }, (_, i) => ({ date: `2026-09-${10 + i}`, energy_kwh: i === 3 ? null : [24, 25, 18, 0, 30, 27, 22][i], tendency: i >= 2, quality_flags: i === 2 ? ["gti_fallback"] : [] }));
  const history = {
    schema_version: 1, enabled: true, running: true, window_days: 7, retention_truncated: false,
    horizons: { hourly_1h: { count_expected: 168, count_forecasts: 153, count_valid: 136, coverage: 136 / 168, mae_kwh: 0.18, bias_kwh: -0.07 } },
    current_targets: { view_version: 1, as_of: iso(asOf), timezone, horizon: "hourly_1h", label: "Jeweils 1 Stunde vorher", intervals: intervals.filter((item, index) => Date.parse(item.start) - hour <= asOf && !(scenario === "gaps" && [10, 11].includes(index))).map((item, index) => ({ ...item, energy_kwh: [0, 0, 0, 0, 0, 0, 0.05, 0.28, 0.9, 1.4, 2.8, 3.3, 3.4, 3.1, 2.6][index] ?? 0, fetched_at: iso(Date.parse(item.start) - hour - 900_000), cutoff: iso(Date.parse(item.start) - hour) })) },
  };
  if (scenario === "experience") history.short_term = { schema_version: 1, rule_version: 1, enabled: true, window_days: 60, minimum_days: 30, horizons: { hourly_1h: { days: 18, count: 90, baseline_mae_kwh: 0.4, candidate_mae_kwh: 0.38, baseline_bias_kwh: 0.1, candidate_bias_kwh: 0.08, criterion_met: false }, hourly_3h: { days: 15, count: 60, baseline_mae_kwh: 0.5, candidate_mae_kwh: 0.52, baseline_bias_kwh: 0.15, candidate_bias_kwh: 0.12, criterion_met: false }, daily_remaining_12: { days: 16, count: 16, baseline_mae_kwh: 2.1, candidate_mae_kwh: 2.0, baseline_bias_kwh: 0.4, candidate_bias_kwh: 0.3, criterion_met: false } } };
  if (scenario === "underperformance") history.underperformance = {schema_version: 1, status: "active", experimental: true, first_day: "2026-09-01", last_day: "2026-09-07", raw_kwh: 140, actual_kwh: 84, difference_kwh: 56, shortfall_fraction: 0.4, coverage_fraction: 1, below_days: 7, accepted_factor: 0.9, learning_paused: true, comparison: {training_count: 60, validation_count: 30, evaluation: {coverage_fraction: 0.8}}};
  if (scenario === "experience") history.temperature_comparison = { schema_version: 1, model: "ross_comparison_v1", enabled: true, parameter_id: "synthetic", window_days: 90, horizons: { daily_previous_18: { days: 18, count: 18, raw_mae_kwh: 2.4, alternative_mae_kwh: 2.3, raw_bias_kwh: 0.4, alternative_bias_kwh: 0.1 }, daily_same_06: { days: 17, count: 17, raw_mae_kwh: 2.2, alternative_mae_kwh: 2.1, raw_bias_kwh: 0.3, alternative_bias_kwh: 0.1 }, hourly_1h: { days: 20, count: 100, raw_mae_kwh: 0.4, alternative_mae_kwh: 0.38, raw_bias_kwh: 0.2, alternative_bias_kwh: 0.1 }, hourly_3h: { days: 20, count: 100, raw_mae_kwh: 0.5, alternative_mae_kwh: 0.51, raw_bias_kwh: 0.2, alternative_bias_kwh: 0.1 } } };
  if (scenario === "empty" || day === "tomorrow") history.current_targets.intervals = [];
  const available = !["no-source", "no-measurement", "gaps", "empty", "stale", "restored"].includes(scenario);
  history.uncertainty = {
    schema_version: 1, label: "Erfahrungsband", as_of: iso(asOf), timezone, basis: "frozen_daily_forecast", retention_truncated: false,
    days: {
      [day]: scenario === "experience" ? { status: "available", target_date: view.date, horizon: day === "today" ? "daily_same_06" : "daily_previous_18", cutoff: iso(todayStart + 6 * hour), forecast_observed_at: iso(todayStart + 5.75 * hour), variant: "raw_model", lower_kwh: 17.2, central_kwh: 22.5, upper_kwh: 28.4, training_count: 60, validation_count: 30, target_coverage: 0.8, evaluation: { coverage_fraction: 0.833, count: 30, mean_width_kwh: 11.2, coverage_wilson95: { lower: 0.664, upper: 0.927, indicative_only: true, assumption: "independent_days" } }, quality_flags: [] } : { status: "unavailable", reasons: ["insufficient_validation"] },
    },
    frozen_hours: scenario === "experience" ? [{ rule_version: 2, status: "available", target_date: view.date, horizon: "hourly_3h", start: iso(todayStart + 15 * hour), end: iso(todayStart + 16 * hour), cutoff: iso(todayStart + 12 * hour), forecast_observed_at: iso(todayStart + 11.75 * hour), lower_kwh: 1.4, central_kwh: 2.1, upper_kwh: 2.8, training_count: 60, validation_count: 30, target_coverage: 0.8, evaluation: { coverage_fraction: 0.8, mean_width_kwh: 1.4, winkler_score_kwh: 1.7, reference_winkler_score_kwh: 2.1, coverage_wilson95: { lower: 0.627, upper: 0.905 } } }] : [],
    remaining_today: { status: "unavailable", reasons: ["unsupported_horizon"] }, next_60_minutes: { status: "unavailable", reasons: ["unsupported_horizon"] },
  };
  const forecastEnd = todayEnd + (scenario === "horizon" ? 6 : 1) * 24 * hour;
  const forecastBoundaries = [todayStart];
  for (let cursor = Math.ceil((todayStart + 1) / hour) * hour; cursor < forecastEnd; cursor += hour) forecastBoundaries.push(cursor);
  forecastBoundaries.push(forecastEnd);
  if (scenario === "archive-off") history.enabled = false;
  if (["archive-off", "archive-empty"].includes(scenario)) history.current_targets.intervals = [];
  return {
    forecast: {
      schema_version: 1, origin: scenario === "restored" ? "restored" : "live", restored_at: scenario === "restored" ? iso(asOf) : null, last_update_success: scenario !== "restored", fetched_at: iso(asOf - (["stale", "restored"].includes(scenario) ? 7_200_000 : 900_000)), view,
      intervals: forecastBoundaries.slice(0, -1).map((value, index) => ({ start: iso(value), end: iso(forecastBoundaries[index + 1]), energy_kwh: 1, ac_power_kw: 1, is_complete: true, quality_flags: [] })),
      planning: { schema_version: 1, status: available && scenario !== "planning-unavailable" ? "available" : "unavailable", reason: ["stale", "restored"].includes(scenario) ? "stale_forecast" : "no_energy", as_of: iso(asOf), fetched_at: iso(asOf - 900_000), timezone, start: iso(todayStart + 14 * hour), end: iso(todayStart + 16 * hour), energy_kwh: 5.87, basis: "current_forecast", assumption: "constant_interval_mean_power", quality_flags: [], hysteresis_applied: false, uncertainty: { status: "unavailable", reason: "unsupported_horizon" } },
    },
    measurement: {
      schema_version: 1, total_energy: { energy_kwh: ["empty", "no-source", "no-measurement"].includes(scenario) ? null : scenario === "zero" ? 0 : scenario === "gaps" ? 5.6 : 12.4, energy_complete: !["gaps", "empty", "no-source", "no-measurement"].includes(scenario), source_count: ["empty", "no-source"].includes(scenario) ? 0 : 2, quality_flags: scenario === "gaps" ? ["gap"] : [] }, total_intervals: ["empty", "no-source", "no-measurement"].includes(scenario) ? [] : totalIntervals,
      outlook: { schema_version: 1, status: available ? "available" : "unavailable", reason: available ? null : "incomplete_measurements", as_of: iso(asOf), timezone, measured_until: iso(asOf - 900_000), measured_kwh: 12.1, bridge_kwh: 0.31, remaining_kwh: 10.76, total_kwh: 23.17, quality_flags: [], correction: "off" },
    },
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
      if (message.service === "get_forecast") {
        data.forecast.view.day_views = Object.fromEntries(["today", "tomorrow"].map((day) => {
          const view = fixture(scenario, { ...message.service_data, day }).forecast.view;
          return [day, Object.fromEntries(["day", "date", "start", "end", "intervals", "complete", "stale"].map((key) => [key, view[key]]))];
        }));
        return { response: data.forecast };
      }
      if (message.service === "get_measurements") return { response: data.measurement };
      if (message.service === "get_history" && message.service_data.day_view) return { response: { ...data.history, day_view: archiveFixture(scenario, message.service_data.day_view) } };
      if (message.service === "get_history") return { response: { ...data.history, window_days: message.service_data.days } };
      throw new Error("Unbekannte Testaktion");
    },
  };
}

export function archiveFixture(scenario, selection) {
  const live = fixture(scenario).forecast.view;
  const shift = Date.parse(`${selection.date}T12:00:00Z`) - Date.parse(`${live.date}T12:00:00Z`);
  const move = (value) => iso(Date.parse(value) + shift);
  const intervals = live.intervals.map((item, index) => ({ ...item, start: move(item.start), end: move(item.end), source_start: move(item.start), source_end: move(item.end), raw_energy_kwh: item.energy_kwh, cutoff: iso(Date.parse(item.start) + shift - 3600000), fetched_at: iso(Date.parse(item.start) + shift - 3900000), observed_at: iso(Date.parse(item.start) + shift - 3600000), measurement: { energy_kwh: scenario === "gaps" && index === 10 ? null : item.energy_kwh, complete: !(scenario === "gaps" && index === 10), assessed_at: iso(Date.parse(live.end) + shift + 3600000), previous_revision_count: index === 12 ? 1 : 0, revised: index === 12, whole_interval_energy_kwh: item.energy_kwh } }));
  return { schema_version: 1, scope: "total", date: selection.date, horizon: selection.horizon ?? "hourly_1h", label: selection.horizon === "hourly_3h" ? "Jeweils 3 Stunden vorher" : "Jeweils 1 Stunde vorher", timezone: live.timezone, start: move(live.start), end: move(live.end), configuration_id: selection.configuration_id ?? "synthetic-current", contexts: [{ configuration_id: "synthetic-current", timezone: live.timezone, active: true, min_date: "2026-01-01", max_date: "2026-12-31" }, { configuration_id: "synthetic-old", timezone: live.timezone, active: false, min_date: "2026-01-01", max_date: "2026-12-31" }], status: scenario === "gaps" ? "partial" : "complete", running: true, intervals, daily_forecasts: { daily_previous_18: { energy_kwh: 24 }, daily_same_06: { energy_kwh: 25 } }, daily_measurement: { energy_kwh: scenario === "gaps" ? null : 23, complete: scenario !== "gaps" } };
}
