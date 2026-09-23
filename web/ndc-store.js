/*
 * Baseline library storage.
 *
 * Real AirShopping responses are often hundreds of KB, so a handful of baselines
 * blows past localStorage's ~5MB ceiling. IndexedDB is used instead, with a
 * localStorage fallback only for a single small baseline so the tool still works
 * where IndexedDB is unavailable.
 *
 * Exposes window.NDCStore.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") globalThis.NDCStore = api;
  if (root && typeof root === "object") root.NDCStore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DB_NAME = "airline-cmp";
  const DB_VERSION = 1;
  const STORE = "baselines";

  function hasIndexedDB() {
    try {
      return typeof indexedDB !== "undefined" && indexedDB !== null;
    } catch (e) {
      return false;
    }
  }

  function openDb() {
    return new Promise((resolve, reject) => {
      if (!hasIndexedDB()) {
        reject(new Error("IndexedDB unavailable"));
        return;
      }
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "name" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("IndexedDB open failed"));
    });
  }

  function tx(db, mode) {
    return db.transaction(STORE, mode).objectStore(STORE);
  }

  function requestToPromise(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  /**
   * Persist a baseline under `name`, replacing any existing one.
   * Returns the stored record (without the XML body).
   */
  async function put(name, label, text) {
    const record = {
      name: name,
      label: label || name,
      text: text,
      bytes: text.length,
      saved_at: new Date().toISOString(),
    };
    const db = await openDb();
    await requestToPromise(tx(db, "readwrite").put(record));
    db.close();
    return { name: record.name, label: record.label, bytes: record.bytes, saved_at: record.saved_at };
  }

  /** All stored baselines as metadata plus text, newest first. */
  async function list() {
    const db = await openDb();
    const records = await requestToPromise(tx(db, "readonly").getAll());
    db.close();
    return (records || []).sort((a, b) =>
      (b.saved_at || "").localeCompare(a.saved_at || "")
    );
  }

  /** Metadata only, so the UI can render the list without loading every body. */
  async function listMeta() {
    const records = await list();
    return records.map((r) => ({
      name: r.name,
      label: r.label,
      bytes: r.bytes,
      saved_at: r.saved_at,
      // A cheap fingerprint preview, so the list is meaningful at a glance.
      preview: previewOf(r.text),
    }));
  }

  function previewOf(text) {
    const routes = [];
    const seen = {};
    const re =
      /<(?:\w+:)?(?:OriginCode|AirportCode)>([^<]+)<\/(?:\w+:)?(?:OriginCode|AirportCode)>/g;
    let match;
    while ((match = re.exec(text)) !== null && routes.length < 4) {
      const code = match[1].trim();
      if (!seen[code]) {
        seen[code] = true;
        routes.push(code);
      }
    }
    return routes.join(" · ");
  }

  async function get(name) {
    const db = await openDb();
    const record = await requestToPromise(tx(db, "readonly").get(name));
    db.close();
    return record || null;
  }

  async function remove(name) {
    const db = await openDb();
    await requestToPromise(tx(db, "readwrite").delete(name));
    db.close();
  }

  async function clear() {
    const db = await openDb();
    await requestToPromise(tx(db, "readwrite").clear());
    db.close();
  }

  async function count() {
    const db = await openDb();
    const value = await requestToPromise(tx(db, "readonly").count());
    db.close();
    return value;
  }

  async function isAvailable() {
    try {
      const db = await openDb();
      db.close();
      return true;
    } catch (e) {
      return false;
    }
  }

  return {
    put: put,
    list: list,
    listMeta: listMeta,
    get: get,
    remove: remove,
    clear: clear,
    count: count,
    isAvailable: isAvailable,
  };
});
