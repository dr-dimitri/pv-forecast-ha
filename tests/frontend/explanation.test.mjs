import test from "node:test";
import assert from "node:assert/strict";
import {loadView, renderContent, renderExplanation, currentExplanation, selectedSeries, tableRows, intervalKey, SharedReadCache, PvForecastCard} from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
import {fixtureHass} from "./fixtures.mjs";
const config={config_entry_id:"demo-plant",day:"today"};
async function load(options={}) {
  let state; const calls=[];
  await loadView(fixtureHass("sunny",{calls}),{...config,...options},value=>state=value);
  return {state,calls};
}

test("Geschlossene Erklärung fordert keine Zusatzdaten, aktive Ansicht nur vorhandenen Lesezyklus",async()=>{
  const closed=await load(), opened=await load({include_explanation:true});
  assert.equal(closed.calls[0].service_data.include_explanation,undefined);
  assert.equal(opened.calls[0].service_data.include_explanation,true);
  assert.equal(opened.calls.length,closed.calls.length);
  assert.equal(currentExplanation(opened.state).factor,1.2);
  assert.match(renderContent(config,opened.state),/Prognose erklärt/);
  assert.doesNotMatch(renderContent(config,opened.state),/class="raw-line"/);
  const html=renderContent({...config,show_raw_forecast:true},opened.state);
  assert.doesNotMatch(html,/class="raw-line"/);assert.match(html,/Grundmodell ohne Selbstkalibrierung/);
  const chart = html.match(/<svg id="interval-chart"[\s\S]*?<\/svg>/)[0];
  for (const item of opened.state.forecast.envelope.explanation.raw_intervals) item.energy_kwh = 10000;
  assert.equal(renderContent({...config,show_raw_forecast:true},opened.state).match(/<svg id="interval-chart"[\s\S]*?<\/svg>/)[0], chart);
  assert.match(html,/Zusätzliche Kürzung durch Anlagenlimit/);
  assert.match(html,/keine gemessenen Geräteverluste/);
});

test("Erklärung bleibt derselben Tagesgeneration zugeordnet, heutige Messbasis bei Morgen unverändert",async()=>{
  const {state,calls}=await load({day:"tomorrow",include_explanation:true});
  assert.equal(calls[0].service_data.day,"tomorrow");
  assert.equal(currentExplanation(state).start,state.forecast.data.start);
  assert.equal(calls[1].service_data.start,state.forecast.data.today_start);
  assert.equal(calls[1].service_data.interval_windows[0].start,state.forecast.data.today_start);
  state.forecast.envelope.explanation.start="2020-01-01T00:00:00Z";
  assert.equal(currentExplanation(state),null);
});

test("Mehrere Karten teilen identische Erklärung einschließlich Tag",async()=>{
  const cache=new SharedReadCache(),calls=[],hass=fixtureHass("sunny",{calls});
  const active={...config,include_explanation:true};
  await Promise.all([loadView(hass,active,()=>{},()=>true,cache),loadView(hass,active,()=>{},()=>true,cache)]);
  assert.equal(calls.filter(c=>c.service==="get_forecast").length,1);
});

test("Dachansicht hat keine Gesamtbilanz; Kurvenwerte werden nicht im Browser berechnet",async()=>{
  const {state}=await load({include_explanation:true});
  const raw=state.forecast.envelope.explanation.raw_intervals;
  assert.deepEqual(selectedSeries({...state,showRaw:true}).raw,raw);
  state.forecast.data.roof_id="south";
  assert.equal(renderExplanation(state),"");
  assert.equal(selectedSeries({...state,showRaw:true}).raw,undefined);
  assert.ok(PvForecastCard.getConfigForm().schema.some(item=>item.name==="show_raw_forecast"));
});

for (const mode of ["available", "roof", "missing", "unavailable", "wrong_day", "wrong_version"]) {
  test(`Intervallauswahl mit Grundmodell-Präferenz bleibt bedienbar: ${mode}`, async () => {
    const options = { ...config, show_raw_forecast: true };
    const { state } = await load(mode === "roof" ? { roof_id: "south" } : { include_explanation: true });
    if (mode === "missing") delete state.forecast.envelope.explanation;
    if (mode === "unavailable") state.forecast.envelope.explanation = { ...state.forecast.envelope.explanation, status: "unavailable", raw_intervals: undefined };
    if (mode === "wrong_day") state.forecast.envelope.explanation.date = "2020-01-01";
    if (mode === "wrong_version") state.forecast.envelope.explanation.schema_version = 2;
    const key = intervalKey(tableRows(state)[0]);
    const html = renderContent(options, state, 360, null, 7, {}, key);
    const details = html.match(/<section id="interval-detail"[\s\S]*?<\/section>/)[0];
    assert.match(details, /Aktuelle Prognose/);
    assert.match(details, /id="interval-next"/);
    if (mode === "available") assert.match(details, /Grundmodell ohne Selbstkalibrierung/);
    else assert.doesNotMatch(details, /Grundmodell ohne Selbstkalibrierung/);
    if (mode === "roof") assert.doesNotMatch(html, /Grundmodell ohne Selbstkalibrierung|class="raw-line"/);
    assert.equal(options.show_raw_forecast, true);

    const restored = await load({ include_explanation: true });
    const returned = renderContent(options, restored.state, 360, null, 7, {}, intervalKey(tableRows(restored.state)[0]));
    assert.doesNotMatch(returned, /class="raw-line"/);
    assert.match(returned.match(/<section id="interval-detail"[\s\S]*?<\/section>/)[0], /Grundmodell ohne Selbstkalibrierung/);
  });
}
