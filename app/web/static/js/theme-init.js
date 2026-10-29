// Apply the persisted theme before first paint so light-theme users don't
// see a dark flash. Loaded synchronously at the top of <body> on every
// page (was an inline <script>; moved out so the CSP can drop
// 'unsafe-inline').
try {
  if (localStorage.getItem("myphotos-theme") === "light") {
    document.body.classList.add("light");
  }
} catch (_) { /* localStorage may be disabled */ }
