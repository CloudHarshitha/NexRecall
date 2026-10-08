// custom.js - Redirect Chainlit's built-in login page to our custom /auth screen.
// This runs in the browser via Chainlit's custom_js injection.

(function () {
  // When Chainlit serves its React SPA for /login, we redirect immediately
  // to our custom combined auth page.  We do NOT intercept /login/callback
  // because Chainlit handles that internally to set the JWT cookie.
  if (
    window.location.pathname === '/login' ||
    window.location.pathname === '/login/'
  ) {
    // Preserve any ?next= param so deep links still work after login
    const next = new URLSearchParams(window.location.search).get('next') || '';
    const dest = '/auth' + (next ? '?next=' + encodeURIComponent(next) : '');
    window.location.replace(dest);
  }
}());
