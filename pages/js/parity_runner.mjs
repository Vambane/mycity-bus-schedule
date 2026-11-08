/**
 * Compare the static planner with cases produced by the Python tests.
 * Usage: node pages/js/parity_runner.mjs cases.json
 * cases.json is {dataDir?, catalog?, cases:[...]}.
 */
import fs from "node:fs";

import {
  createCatalog,
  dayTypeFor,
  easterSunday,
  encodeLegs,
  holidayName,
  journeyView,
  legSequenceOf,
  searchJourneys,
  stopBoard,
} from "./planner.mjs";

const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

function loadDir(dir) {
  const meta = JSON.parse(fs.readFileSync(`${dir}/meta.json`, "utf8"));
  const days = {};
  for (const day of ["weekday", "saturday", "sunday"]) {
    days[day] = JSON.parse(fs.readFileSync(`${dir}/timetable/${day}.json`, "utf8"));
  }
  return createCatalog(meta, days);
}

const catalog = input.dataDir
  ? loadDir(input.dataDir)
  : createCatalog(input.catalog.meta, input.catalog.days);

const out = input.cases.map((item) => {
  if (item.kind === "search") return {id: item.id, result: searchJourneys(catalog, item)};
  if (item.kind === "stop") return {id: item.id, result: stopBoard(catalog, item)};
  if (item.kind === "day") {
    return {
      id: item.id,
      day_type: dayTypeFor(item.date, item.extra || []),
      holiday: holidayName(item.date, item.extra || []),
    };
  }
  if (item.kind === "easter") return {id: item.id, easter: easterSunday(item.year)};
  if (item.kind === "legs") return {id: item.id, token: encodeLegs(item.legs)};
  if (item.kind === "sequence") {
    return {
      id: item.id,
      stops: legSequenceOf(catalog, item.day_type, item.route_id, item.direction, item.board, item.alight),
    };
  }
  if (item.kind === "journey") return {id: item.id, result: journeyView(catalog, item, item.now || "2026-11-09T12:00:00+02:00")};
  throw new Error(`unknown kind ${item.kind}`);
});

process.stdout.write(JSON.stringify(out));
