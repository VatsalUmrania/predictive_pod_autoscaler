/**
 * NEXUS React SDK — <HealBoundary> Component
 * ============================================
 * React error boundary that integrates with the NEXUS self-healing system.
 * Wraps any React subtree, catches render/lifecycle errors, and reports them
 * to the NEXUS beacon (which forwards them to the NEXUS Status API).
 *
 * Usage:
 *   import { HealBoundary } from './nexus/sdk/js/react'
 *
 *   <HealBoundary
 *     component="CheckoutForm"
 *     critical={true}
 *     fallback={<StaticCheckoutForm />}
 *     reportAsset="checkout_conversion"
 *   >
 *     <CheckoutForm />
 *   </HealBoundary>
 *
 * Props:
 *   component    {string}  Human-readable name for this component (shown in NEXUS console)
 *   critical     {boolean} If true, failure triggers SRE alert after N attempts
 *   fallback     {ReactNode} UI to render when the component errors
 *   reportAsset  {string}  Business metric name tied to this component
 *   onError      {function} Optional callback: (error, info) => void
 *   maxRetries   {number}  Auto-retry render N times before showing fallback (default: 0)
 *   retryDelay   {number}  ms between retries (default: 1000)
 *
 * What it reports to NEXUS:
 *   - Component name + error message + truncated stack trace
 *   - Error info (React componentStack)
 *   - reportAsset (ties error to a business metric)
 *   - retryCount (how many times it auto-retried before failing)
 *
 * Requirements:
 *   - React 16.3+ (uses getDerivedStateFromError + componentDidCatch)
 *   - beacon.js must be loaded (uses NexusBeacon global or window.NexusBeacon)
 *   - OR: pass token + nexusUrl props for standalone usage without beacon.js
 *
 * CDN / NPM note:
 *   This file works with React loaded via CDN (window.React) or via import:
 *   const React = require('react') or import React from 'react'
 */

(function (factory) {
  // UMD wrapper — works with CommonJS (Node/bundlers) and browser globals
  if (typeof module !== 'undefined' && module.exports) {
    // CommonJS / Node.js
    var React = require('react');
    module.exports = factory(React);
  } else if (typeof window !== 'undefined' && window.React) {
    // Browser global
    var exported = factory(window.React);
    window.NexusReact = exported;
  }
}(function (React) {
  'use strict';

  // ── Internal: report to NEXUS ───────────────────────────────────────────────

  function _report(component, error, info, reportAsset, retryCount, token, nexusUrl) {
    var payload = {
      type:         'component_error',
      component:    component,
      message:      error && error.message   ? String(error.message).slice(0, 300) : String(error).slice(0, 300),
      stack:        error && error.stack     ? String(error.stack).slice(0, 1000)  : null,
      component_stack: info && info.componentStack ? String(info.componentStack).slice(0, 800) : null,
      report_asset: reportAsset || null,
      retry_count:  retryCount  || 0,
      critical:     true,
      ts:           Date.now(),
    };

    // 1. Use NexusBeacon global (beacon.js loaded via <script> tag)
    if (typeof window !== 'undefined' && window.NexusBeacon) {
      window.NexusBeacon.track('component_error', payload);
      return;
    }

    // 2. Standalone: POST directly to NEXUS if token + nexusUrl provided
    if (token && nexusUrl) {
      var url  = (nexusUrl.replace(/\/$/, '')) + '/sdk/frontend';
      var body = JSON.stringify(payload);
      try {
        if (typeof fetch !== 'undefined') {
          fetch(url, {
            method:    'POST',
            headers:   { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
            body:      body,
            keepalive: true,
          }).catch(function () {});
        } else if (typeof XMLHttpRequest !== 'undefined') {
          var xhr = new XMLHttpRequest();
          xhr.open('POST', url, true);
          xhr.setRequestHeader('Content-Type', 'application/json');
          xhr.setRequestHeader('Authorization', 'Bearer ' + token);
          xhr.send(body);
        }
      } catch (e) { /* silent */ }
    }
  }

  // ── HealBoundary class component ────────────────────────────────────────────

  var HealBoundary = (function (_Component) {
    function HealBoundary(props) {
      _Component.call(this, props);
      this.state = {
        hasError:    false,
        error:       null,
        retryCount:  0,
        retrying:    false,
      };
      this._retryTimer = null;
    }

    // Inherit from React.Component
    HealBoundary.prototype = Object.create(_Component.prototype);
    HealBoundary.prototype.constructor = HealBoundary;

    // Static: update state when error is caught
    HealBoundary.getDerivedStateFromError = function (error) {
      return { hasError: true, error: error };
    };

    // Instance: side effects after error (reporting, retry)
    HealBoundary.prototype.componentDidCatch = function (error, info) {
      var props      = this.props;
      var retryCount = this.state.retryCount;

      // Report to NEXUS
      _report(
        props.component   || 'Unknown',
        error,
        info,
        props.reportAsset || null,
        retryCount,
        props.token       || null,
        props.nexusUrl    || 'http://localhost:8080'
      );

      // Optional developer callback
      if (typeof props.onError === 'function') {
        props.onError(error, info);
      }

      // Auto-retry logic
      var maxRetries = typeof props.maxRetries === 'number' ? props.maxRetries : 0;
      if (retryCount < maxRetries) {
        var self       = this;
        var retryDelay = typeof props.retryDelay === 'number' ? props.retryDelay : 1000;
        this._retryTimer = setTimeout(function () {
          self.setState(function (s) {
            return { hasError: false, error: null, retryCount: s.retryCount + 1 };
          });
        }, retryDelay);
      }
    };

    HealBoundary.prototype.componentWillUnmount = function () {
      if (this._retryTimer) clearTimeout(this._retryTimer);
    };

    // Manual retry (can be called from fallback UI)
    HealBoundary.prototype.retry = function () {
      this.setState({ hasError: false, error: null });
    };

    HealBoundary.prototype.render = function () {
      if (!this.state.hasError) {
        return this.props.children || null;
      }

      // Show fallback
      var fallback = this.props.fallback;
      if (!fallback) {
        // Default minimal fallback if none provided
        return React.createElement('div', {
          style: {
            padding:      '16px',
            background:   '#1a0a0a',
            border:       '1px solid #ff4d6a',
            borderRadius: '8px',
            color:        '#ff8888',
            fontSize:     '13px',
          }
        },
          React.createElement('strong', null,
            '⚠️ ' + (this.props.component || 'Component') + ' failed'
          ),
          React.createElement('p', { style: { margin: '8px 0 0', color: '#aaa' } },
            'NEXUS has been notified and is investigating. '
          )
        );
      }

      // If fallback is a function, call it with (error, retry)
      if (typeof fallback === 'function') {
        return fallback(this.state.error, this.retry.bind(this));
      }

      return fallback;
    };

    return HealBoundary;
  }(React.Component));

  // ── useHealGuard hook (React 16.8+) ────────────────────────────────────────
  // Wraps an async function with NEXUS error reporting.
  // Usage:
  //   const safeCheckout = useHealGuard(checkout, { component: 'CheckoutButton' });

  function useHealGuard(fn, options) {
    var opts = options || {};
    return function () {
      var args = Array.prototype.slice.call(arguments);
      var result = fn.apply(null, args);
      if (result && typeof result.catch === 'function') {
        return result.catch(function (err) {
          _report(
            opts.component   || fn.name || 'async_handler',
            err, null,
            opts.reportAsset || null,
            0,
            opts.token       || null,
            opts.nexusUrl    || 'http://localhost:8080'
          );
          if (opts.rethrow !== false) throw err;
        });
      }
      return result;
    };
  }

  return {
    HealBoundary:  HealBoundary,
    useHealGuard:  useHealGuard,
  };
}));
