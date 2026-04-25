/**
 * NEXUS Node.js SDK
 * ==================
 * Server-side SDK for Node.js backends (Express, Fastify, Koa, serverless).
 * Instruments HTTP requests, DB queries, and function invocations, sending
 * structured telemetry to the NEXUS Status API.
 *
 * Usage:
 *   const { SelfHeal } = require('./selfheal');   // or from npm package
 *
 *   // Initialize (call once at startup)
 *   SelfHeal.init({
 *     appName:        'checkout-service',
 *     token:          process.env.SELFHEAL_TOKEN,
 *     criticalRoutes: ['/api/checkout', '/api/payment'],
 *   });
 *
 *   // Express middleware
 *   app.use(SelfHeal.middleware());
 *
 *   // DB wrapper (pg Pool example)
 *   const db = SelfHeal.wrapDB(new Pool({ connectionString: DATABASE_URL }), {
 *     slowQueryThreshold: 200,
 *     trackTables: true,
 *   });
 *
 *   // Serverless / edge function wrapper
 *   export default SelfHeal.handler(async (req, res) => {
 *     // your function code
 *   }, { functionName: 'process-webhook' });
 *
 * No dependencies beyond Node.js built-ins (http, https, url).
 * Optional: use httpx or got for async HTTP if available.
 */

'use strict';

const http  = require('http');
const https = require('https');
const { URL } = require('url');

// ──────────────────────────────────────────────────────────────────────────────
// Table name extractor (regex — no SQL parser dependency)
// ──────────────────────────────────────────────────────────────────────────────
const _TABLE_RE = /(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+["'`]?(\w+)["'`]?/gi;

function extractTables(sql) {
  const tables = [];
  let m;
  while ((m = _TABLE_RE.exec(sql)) !== null) {
    tables.push(m[1].toLowerCase());
  }
  _TABLE_RE.lastIndex = 0;
  return [...new Set(tables)];
}

// ──────────────────────────────────────────────────────────────────────────────
// Core SDK class
// ──────────────────────────────────────────────────────────────────────────────

class SelfHealSDK {
  constructor() {
    this._nexusUrl       = null;
    this._token          = null;
    this._appName        = null;
    this._criticalRoutes = [];
    this._initialized    = false;
  }

  /**
   * Initialize the SDK. Call once at application startup.
   * @param {Object} options
   * @param {string} options.appName         - Application name (must match selfheal.yaml)
   * @param {string} options.token           - SELFHEAL_TOKEN from NEXUS
   * @param {string} [options.nexusUrl]      - Override NEXUS API URL
   * @param {string[]} [options.criticalRoutes] - Routes to treat as critical
   */
  init(options) {
    this._nexusUrl       = (options.nexusUrl || process.env.NEXUS_API_URL || 'http://localhost:8080').replace(/\/$/, '');
    this._token          = options.token || options.env || process.env.SELFHEAL_TOKEN || '';
    this._appName        = options.appName || 'unknown';
    this._criticalRoutes = options.criticalRoutes || [];
    this._initialized    = true;

    if (!this._token) {
      console.warn('[SelfHeal] No SELFHEAL_TOKEN provided. Telemetry disabled.');
    } else {
      // Register app + critical routes on startup (best-effort, non-blocking)
      this._registerCriticalRoutes().catch(() => {});
    }

    return this;
  }

  // ── Express / Fastify / Koa middleware ───────────────────────────────────────

  /**
   * Returns an Express/Fastify middleware function.
   * Captures 5xx responses and slow requests.
   * @param {Object} [options]
   * @param {number} [options.slowThresholdMs=500] - Emit slow-response event above this
   */
  middleware(options) {
    const self         = this;
    const slowMs       = (options && options.slowThresholdMs) || 500;

    return function nexusSelfHealMiddleware(req, res, next) {
      const start = Date.now();
      const url   = req.path || req.url || '/';

      const _origEnd = res.end.bind(res);
      res.end = function (...args) {
        const duration = Date.now() - start;
        const status   = res.statusCode;

        if (status >= 500 || duration > slowMs) {
          self._emit({
            type:        'route_error',
            route:       url,
            method:      req.method || 'GET',
            status_code: status,
            duration_ms: duration,
            slow:        status < 500 && duration > slowMs,
          }).catch(() => {});
        }

        return _origEnd(...args);
      };

      if (typeof next === 'function') next();
    };
  }

  // ── DB wrapper ────────────────────────────────────────────────────────────────

  /**
   * Wrap a database connection pool (pg.Pool, mysql2.createPool, etc.)
   * to track query performance and optionally feed spike predictions.
   *
   * @param {Object} pool - The database pool object
   * @param {Object} [options]
   * @param {number}  [options.slowQueryThreshold=200] - Slow query threshold (ms)
   * @param {boolean} [options.trackTables=false]      - Extract table names for spike prediction
   */
  wrapDB(pool, options) {
    const self           = this;
    const slowMs         = (options && options.slowQueryThreshold) || 200;
    const trackTables    = (options && options.trackTables) || false;

    return new Proxy(pool, {
      get(target, prop) {
        if (prop !== 'query' && prop !== 'execute') {
          const val = target[prop];
          return typeof val === 'function' ? val.bind(target) : val;
        }

        return function wrappedQuery(text, values, callback) {
          const sql   = typeof text === 'object' ? (text.text || '') : (text || '');
          const start = Date.now();

          // Support both callback and promise styles
          const result = target[prop].apply(target, arguments);

          if (result && typeof result.then === 'function') {
            return result.then(
              function (res) {
                const duration = Date.now() - start;
                self._emitQuery(sql, duration, trackTables, slowMs).catch(() => {});
                return res;
              },
              function (err) {
                const duration = Date.now() - start;
                self._emit({
                  type:        'db_query',
                  sql_preview: sql.slice(0, 120),
                  duration_ms: duration,
                  tables:      trackTables ? extractTables(sql) : [],
                  error:       err && err.message,
                }).catch(() => {});
                throw err;
              }
            );
          }

          return result;
        };
      },
    });
  }

  // ── Serverless / edge function wrapper ──────────────────────────────────────

  /**
   * Wrap a serverless function handler to capture errors + cold starts.
   * @param {Function} fn - Async handler function (req, res) => ...
   * @param {Object} [options]
   * @param {string} [options.functionName] - Human-readable name for this function
   * @param {number} [options.coldStartBudget] - Alert if cold start exceeds N ms
   */
  handler(fn, options) {
    const self     = this;
    const fnName   = (options && options.functionName) || fn.name || 'anonymous';
    const budget   = (options && options.coldStartBudget) || 800;
    let   started  = false;

    return async function nexusWrappedHandler(req, res) {
      const start      = Date.now();
      const coldStart  = !started;
      started          = true;

      try {
        const result = await fn(req, res);
        const dur    = Date.now() - start;
        if (coldStart && dur > budget) {
          self._emit({
            type:          'function_cold_start',
            function_name: fnName,
            duration_ms:   dur,
            budget_ms:     budget,
          }).catch(() => {});
        }
        return result;
      } catch (err) {
        const dur = Date.now() - start;
        self._emit({
          type:          'function_error',
          function_name: fnName,
          error:         err && err.message,
          duration_ms:   dur,
          cold_start:    coldStart,
        }).catch(() => {});
        throw err;
      }
    };
  }

  // ── Internal helpers ──────────────────────────────────────────────────────────

  async _emitQuery(sql, duration_ms, trackTables, slowMs) {
    const tables = trackTables ? extractTables(sql) : [];
    await this._emit({
      type:        'db_query',
      sql_preview: sql.slice(0, 120),
      duration_ms: Math.round(duration_ms),
      tables,
      slow:        duration_ms > slowMs,
    });
  }

  async _registerCriticalRoutes() {
    if (!this._criticalRoutes.length) return;
    await this._emit({
      type:             'critical_routes_registered',
      critical_routes:  this._criticalRoutes,
    });
  }

  /**
   * Emit a payload to the NEXUS SDK ingest endpoint.
   * Non-blocking: uses Node.js http/https directly, no external deps.
   */
  _emit(payload) {
    if (!this._token || !this._nexusUrl) return Promise.resolve();

    payload.app = this._appName;
    payload.ts  = Date.now();

    const body    = Buffer.from(JSON.stringify(payload));
    const urlObj  = new URL('/sdk/event', this._nexusUrl);
    const lib     = urlObj.protocol === 'https:' ? https : http;

    return new Promise((resolve) => {
      const req = lib.request(
        {
          hostname: urlObj.hostname,
          port:     urlObj.port || (urlObj.protocol === 'https:' ? 443 : 80),
          path:     urlObj.pathname,
          method:   'POST',
          headers: {
            'Content-Type':   'application/json',
            'Authorization':  'Bearer ' + this._token,
            'Content-Length': body.length,
          },
        },
        (res) => { res.resume(); resolve(); }
      );
      req.on('error', () => resolve());
      req.setTimeout(3000, () => { req.destroy(); resolve(); });
      req.write(body);
      req.end();
    });
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Module-level singleton
// ──────────────────────────────────────────────────────────────────────────────

const SelfHeal = new SelfHealSDK();

module.exports = { SelfHeal, SelfHealSDK };
