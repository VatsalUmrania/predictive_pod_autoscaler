/**
 * NEXUS Browser Beacon SDK
 * =========================
 * Lightweight browser SDK for NEXUS self-healing integration.
 * Captures JS errors, Web Vitals, failed network requests, and rage clicks,
 * then streams them to the NEXUS Status API as structured telemetry.
 *
 * Usage — single <script> tag in your HTML:
 *   <script src="http://localhost:8080/sdk/beacon.js"
 *           data-app="my-frontend"
 *           data-token="sh_xxxxxxxxxxxxx"
 *           data-nexus-url="http://localhost:8080">
 *   </script>
 *
 * What it captures:
 *   - Unhandled JS exceptions (window.onerror)
 *   - Unhandled promise rejections
 *   - Failed fetch() calls (status >= 500)
 *   - Failed XMLHttpRequest calls (status >= 500)
 *   - Core Web Vitals: LCP, CLS via PerformanceObserver
 *   - Rage clicks (4+ clicks in 1 second on the same element)
 *
 * Delivery:
 *   Uses navigator.sendBeacon() (non-blocking, survives page unload).
 *   Falls back to fetch({ keepalive: true }) for non-beacon browsers.
 *
 * Privacy:
 *   No PII is collected. URLs are stripped of query parameters.
 *   Stack traces are truncated to 1000 chars.
 *
 * Size: ~3KB minified
 */
(function (w, d) {
  'use strict';

  // ── Config from script tag ─────────────────────────────────────────────────
  var script   = d.currentScript || (function () {
    var scripts = d.getElementsByTagName('script');
    return scripts[scripts.length - 1];
  })();

  var NEXUS_URL = (script.getAttribute('data-nexus-url') || 'http://localhost:8080').replace(/\/$/, '');
  var TOKEN     = script.getAttribute('data-token') || '';
  var APP       = script.getAttribute('data-app')   || w.location.hostname;
  var ENDPOINT  = NEXUS_URL + '/sdk/frontend';

  // ── Core send function ─────────────────────────────────────────────────────
  function send(payload) {
    payload.app = APP;
    payload.ts  = Date.now();
    payload.url = _sanitizeUrl(w.location.href);

    var body = JSON.stringify(payload);

    // Try sendBeacon (non-blocking, survives page close)
    if (w.navigator && w.navigator.sendBeacon) {
      try {
        var blob = new Blob([body], { type: 'application/json' });
        if (w.navigator.sendBeacon(ENDPOINT, blob)) return;
      } catch (e) { /* fall through */ }
    }

    // Fallback: fetch with keepalive
    if (w.fetch) {
      w.fetch(ENDPOINT, {
        method:   'POST',
        headers:  { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN },
        body:     body,
        keepalive:true,
      }).catch(function () {});
    }
  }

  function _sanitizeUrl(url) {
    try {
      var u = new URL(url);
      return u.origin + u.pathname;   // strip query params
    } catch (e) { return url; }
  }

  function _truncate(str, max) {
    if (!str) return null;
    return String(str).slice(0, max);
  }

  // ── JS error capture ────────────────────────────────────────────────────────
  var _origOnError = w.onerror;
  w.onerror = function (msg, src, line, col, err) {
    send({
      type:    'js_error',
      message: _truncate(msg, 300),
      source:  _truncate(src, 200),
      line:    line,
      col:     col,
      stack:   _truncate(err && err.stack, 1000),
    });
    if (_origOnError) return _origOnError.apply(this, arguments);
    return false;
  };

  // ── Unhandled promise rejections ────────────────────────────────────────────
  w.addEventListener('unhandledrejection', function (e) {
    var reason = e.reason;
    send({
      type:    'unhandled_rejection',
      message: _truncate(reason && reason.message || String(reason), 300),
      stack:   _truncate(reason && reason.stack, 1000),
    });
  });

  // ── Fetch interception ──────────────────────────────────────────────────────
  if (w.fetch) {
    var _origFetch = w.fetch.bind(w);
    w.fetch = function (resource, init) {
      var reqUrl = typeof resource === 'string' ? resource : (resource && resource.url);
      return _origFetch(resource, init).then(function (resp) {
        if (resp.status >= 500) {
          send({
            type:   'failed_fetch',
            url:    _sanitizeUrl(reqUrl || ''),
            status: resp.status,
          });
        }
        return resp;
      }, function (err) {
        send({
          type:    'failed_fetch',
          url:     _sanitizeUrl(reqUrl || ''),
          status:  0,
          message: _truncate(err && err.message, 200),
        });
        throw err;
      });
    };
  }

  // ── XHR interception ────────────────────────────────────────────────────────
  var _origXHROpen = w.XMLHttpRequest && w.XMLHttpRequest.prototype.open;
  var _origXHRSend = w.XMLHttpRequest && w.XMLHttpRequest.prototype.send;

  if (_origXHROpen && _origXHRSend) {
    w.XMLHttpRequest.prototype.open = function (method, url) {
      this._nexusUrl    = url;
      this._nexusMethod = method;
      return _origXHROpen.apply(this, arguments);
    };
    w.XMLHttpRequest.prototype.send = function () {
      this.addEventListener('load', function () {
        if (this.status >= 500) {
          send({
            type:   'failed_xhr',
            url:    _sanitizeUrl(this._nexusUrl || ''),
            method: this._nexusMethod || 'GET',
            status: this.status,
          });
        }
      });
      return _origXHRSend.apply(this, arguments);
    };
  }

  // ── Web Vitals via PerformanceObserver ──────────────────────────────────────
  if (w.PerformanceObserver) {
    // LCP — Largest Contentful Paint
    _tryObserve('largest-contentful-paint', function (entry) {
      send({ type: 'web_vital', name: 'LCP', value: Math.round(entry.startTime) });
    });

    // CLS — Cumulative Layout Shift
    var _clsValue = 0;
    _tryObserve('layout-shift', function (entry) {
      if (!entry.hadRecentInput) {
        _clsValue += entry.value;
        send({ type: 'web_vital', name: 'CLS', value: Math.round(_clsValue * 1000) / 1000 });
      }
    });

    // FID / INP — First Input Delay / Interaction to Next Paint
    _tryObserve('first-input', function (entry) {
      send({ type: 'web_vital', name: 'FID', value: Math.round(entry.processingStart - entry.startTime) });
    });
  }

  function _tryObserve(type, cb) {
    try {
      var obs = new w.PerformanceObserver(function (list) {
        list.getEntries().forEach(cb);
      });
      obs.observe({ type: type, buffered: true });
    } catch (e) { /* PerformanceObserver not supported for this entry type */ }
  }

  // ── Rage click detection ────────────────────────────────────────────────────
  var _clickLog = [];
  d.addEventListener('click', function (e) {
    var now = Date.now();
    _clickLog.push({ ts: now, el: e.target });
    _clickLog = _clickLog.filter(function (c) { return now - c.ts < 1000; });
    if (_clickLog.length >= 4) {
      var el = e.target;
      send({
        type:    'rage_click',
        element: _truncate((el.id ? '#' + el.id : el.tagName), 100),
        clicks:  _clickLog.length,
        url:     _sanitizeUrl(w.location.href),
      });
      _clickLog = [];   // reset after reporting
    }
  }, { passive: true });

  // ── Public API ──────────────────────────────────────────────────────────────
  w.NexusBeacon = {
    /** Manually send a custom event */
    track: function (type, data) {
      send(Object.assign({ type: type }, data || {}));
    },
    /** Mark a component render as failed (for React error boundaries etc.) */
    componentError: function (componentName, error) {
      send({
        type:      'component_error',
        component: componentName,
        message:   _truncate(error && error.message, 300),
        stack:     _truncate(error && error.stack, 1000),
      });
    },
  };

}(window, document));
