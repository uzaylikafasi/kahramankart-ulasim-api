import assert from "node:assert/strict";
import test from "node:test";
import { FavoriteStore, TransportClient } from "../examples/client.mjs";

test("HTTP client encodes search and timetable queries", async () => {
  const calls = [];
  const client = new TransportClient({ apiKey: "local-key", fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ data: [], meta: {} }), { status: 200 });
  }});
  await client.search("H/1 & meydan");
  await client.timetables("H/1");
  assert.equal(calls[0].url.searchParams.get("q"), "H/1 & meydan");
  assert.equal(calls[1].url.searchParams.get("line_code"), "H/1");
  assert.equal(calls[0].options.headers["X-API-Key"], "local-key");
});

test("HTTP error carries status and sanitized server payload", async () => {
  const client = new TransportClient({ fetchImpl: async () => new Response(
    JSON.stringify({ error: { message: "Service unavailable" } }), { status: 502 }) });
  await assert.rejects(client.stop(6968), error => error.status === 502 && error.message === "Service unavailable");
});

test("Favorites persist, update, remove and survive invalid stored JSON", () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) };
  const favorites = new FavoriteStore(storage);
  favorites.add({ kind: "stop", id: 6968, label: "Meydan" });
  favorites.add({ kind: "stop", id: 6968, label: "New label" });
  assert.equal(new FavoriteStore(storage).list().length, 1);
  assert.equal(favorites.list()[0].label, "New label");
  assert.equal(favorites.remove("stop", 6968).length, 0);
  storage.setItem(favorites.key, "invalid");
  assert.deepEqual(favorites.list(), []);
});

test("Polling stops cleanly after first result", async () => {
  let calls = 0;
  const client = new TransportClient({ fetchImpl: async () => {
    calls++;
    return new Response(JSON.stringify({ data: [], meta: {} }), { status: 200 });
  }});
  const watcher = client.watchVehicles(9576, 1, { onData: () => watcher.stop() });
  await watcher.done;
  assert.equal(calls, 1);
});
