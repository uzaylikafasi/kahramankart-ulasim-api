/** Browser/Node HTTP client. Keep Pallium credentials on the API server. */
export class TransportClient {
  constructor({ baseUrl = "http://127.0.0.1:8000", apiKey = "", fetchImpl = globalThis.fetch } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.apiKey = apiKey;
    this.fetchImpl = fetchImpl;
  }

  async get(path, params = {}, { signal } = {}) {
    const url = new URL(`${this.baseUrl}/v1/${path}`);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    }
    const response = await this.fetchImpl(url, {
      signal,
      headers: this.apiKey ? { "X-API-Key": this.apiKey } : {},
    });
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload.error?.message ?? `API HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  search(q, options = {}) { return this.get("search", { q, ...options }); }
  nearbyStops(latitude, longitude) { return this.get("stops/nearby", { latitude, longitude }); }
  mapStops({ south, west, north, east }) { return this.get("stops/in-bounds", { south, west, north, east }); }
  stop(id) { return this.get(`stops/${encodeURIComponent(id)}`); }
  stopLines(id) { return this.get(`stops/${encodeURIComponent(id)}/lines`); }
  arrivals(id, options) { return this.get(`stops/${encodeURIComponent(id)}/arrivals`, {}, options); }
  line(id, direction = 1) { return this.get(`lines/${encodeURIComponent(id)}`, { direction }); }
  lineStops(id, direction = 1) { return this.get(`lines/${encodeURIComponent(id)}/stops`, { direction }); }
  vehicles(id, direction = 1, options) { return this.get(`lines/${encodeURIComponent(id)}/vehicles`, { direction }, options); }
  shape(id, direction = 1, format = "geojson") { return this.get(`lines/${encodeURIComponent(id)}/shape`, { direction, format }); }
  timetables(lineCode) { return this.get("timetables", { line_code: lineCode }); }
  announcements() { return this.get("announcements"); }
  journeyPoints(q) { return this.get("journey-points", { q }); }
  planJourney(fromStopId, toStopId, options = {}) {
    return this.get("journeys", { ...options, from_stop_id: fromStopId, to_stop_id: toStopId });
  }

  watchVehicles(id, direction, options) {
    return this.watch(signal => this.vehicles(id, direction, { signal }), options);
  }
  watchArrivals(id, options) {
    return this.watch(signal => this.arrivals(id, { signal }), options);
  }
  watch(load, { onData, onError = () => {}, intervalMs = 15000, signal } = {}) {
    if (typeof onData !== "function") throw new TypeError("onData callback is required");
    if (!Number.isFinite(intervalMs) || intervalMs < 10000) throw new RangeError("Minimum interval is 10000 ms");
    const controller = new AbortController();
    const stop = () => controller.abort();
    if (signal?.aborted) stop();
    signal?.addEventListener("abort", stop, { once: true });
    const done = (async () => {
      try {
        while (!controller.signal.aborted) {
          try { await onData(await load(controller.signal)); }
          catch (error) { if (!controller.signal.aborted) await onError(error); }
          if (controller.signal.aborted) break;
          await new Promise(resolve => {
            const finish = () => {
              clearTimeout(timer);
              controller.signal.removeEventListener("abort", finish);
              resolve();
            };
            const timer = setTimeout(finish, intervalMs);
            controller.signal.addEventListener("abort", finish, { once: true });
          });
        }
      } finally {
        signal?.removeEventListener("abort", stop);
      }
    })();
    return { stop, done };
  }
}

/** Device-local favorites; no account or server synchronization. */
export class FavoriteStore {
  constructor(storage = globalThis.localStorage, key = "kahraman-transport-favorites-v1") {
    if (!storage) throw new Error("Provide a Storage implementation on this platform");
    this.storage = storage;
    this.key = key;
  }
  list() {
    try {
      const values = JSON.parse(this.storage.getItem(this.key) ?? "[]");
      return Array.isArray(values) ? values.filter(x => x && ["stop", "line"].includes(x.kind) && /^\d+$/.test(String(x.id))) : [];
    } catch { return []; }
  }
  add({ kind, id, label = "", direction = 1 }) {
    if (!["stop", "line"].includes(kind) || !Number.isSafeInteger(Number(id)) || Number(id) <= 0) {
      throw new TypeError("A positive stop/line ID is required");
    }
    if (![1, 2, 3].includes(direction)) throw new RangeError("Invalid direction");
    const values = this.list().filter(x => !(x.kind === kind && String(x.id) === String(id)));
    values.push({ kind, id: Number(id), label: String(label), direction });
    this.storage.setItem(this.key, JSON.stringify(values));
    return values;
  }
  remove(kind, id) {
    const values = this.list().filter(x => !(x.kind === kind && String(x.id) === String(id)));
    this.storage.setItem(this.key, JSON.stringify(values));
    return values;
  }
}
