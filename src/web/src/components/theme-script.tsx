const THEME_SCRIPT = `
(function () {
  try {
    // Follow the operating system's color scheme preference.
    var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", prefersDark);
  } catch (e) {}
})();
`.trim()

/**
 * Applies the system theme before first paint so the page never flashes
 * the wrong color scheme on load.
 */
export function ThemeScript() {
  return <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
}
