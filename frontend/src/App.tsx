import { useEffect, useState } from "react";
import AdminPage from "./pages/AdminPage";
import DigestPage from "./pages/DigestPage";
import MembersPage from "./pages/MembersPage";

// Routes live in the URL hash: #/digest/2026-09-14, #/members, #/admin
export interface Route {
  page: "digest" | "members" | "admin";
  week?: string;
}

function parseHash(hash: string): Route {
  const [page, arg] = hash.replace(/^#\/?/, "").split("/");
  if (page === "members" || page === "admin") return { page };
  return { page: "digest", week: arg && /^\d{4}-\d{2}-\d{2}$/.test(arg) ? arg : undefined };
}

export function navigate(route: Route) {
  window.location.hash = route.page === "digest" && route.week ? `/digest/${route.week}` : `/${route.page}`;
}

function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

const TABS: { page: Route["page"]; label: string }[] = [
  { page: "digest", label: "Digest" },
  { page: "members", label: "Members" },
  { page: "admin", label: "Admin" },
];

export default function App() {
  const route = useRoute();
  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="#/digest">
            <span className="brand-mark" aria-hidden>
              ✓
            </span>
            Vote Tracker
          </a>
          <nav className="tabs" aria-label="Sections">
            {TABS.map((t) => (
              <a
                key={t.page}
                href={`#/${t.page}`}
                className={route.page === t.page ? "tab active" : "tab"}
                aria-current={route.page === t.page ? "page" : undefined}
              >
                {t.label}
              </a>
            ))}
          </nav>
        </div>
      </header>
      <main className="content">
        {route.page === "digest" && <DigestPage week={route.week} />}
        {route.page === "members" && <MembersPage />}
        {route.page === "admin" && <AdminPage />}
      </main>
    </div>
  );
}
