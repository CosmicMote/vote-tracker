// Light/dark choice for this browser only, kept in localStorage. index.html applies the saved
// choice before the first paint; "system" (the default) leaves it to prefers-color-scheme.

export type Theme = "system" | "light" | "dark";

const KEY = "tracker.theme";

export function getTheme(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system"; // storage blocked, e.g. some private windows
  }
}

export function setTheme(theme: Theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    // still applies for this page view
  }
}
