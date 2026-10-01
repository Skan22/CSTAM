import { useEffect, useState } from "react";
import { ROUTES, Shell } from "./components/Shell";
import { Card } from "./components/ui";
import { LiveProvider, useSession } from "./lib/live";
import { can } from "./lib/roles";
import { Audit } from "./pages/Audit";
import { Gateways } from "./pages/Gateways";
import { Ipam } from "./pages/Ipam";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Settings } from "./pages/Settings";
import { TeamHome } from "./pages/TeamHome";
import { Teams } from "./pages/Teams";
import { Traffic } from "./pages/Traffic";

const PAGES: Record<string, () => React.JSX.Element> = {
  overview: Overview, teams: Teams, gateways: Gateways, traffic: Traffic, ipam: Ipam, audit: Audit, settings: Settings,
};

function useRoute(): string {
  const read = () => location.hash.replace(/^#\/?/, "").split("/")[0] || "overview";
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export default function App() {
  const s = useSession();
  const route = useRoute();
  if (!s) return <Login />;
  if (s.role === "team") {
    // A team account has one page and its own event stream; the staff routes would only 403.
    return (
      <LiveProvider stream="/v1/me/events">
        <Shell route="team" title="Your team" nav={[{ path: "team", label: "My team", min: "viewer" }]}>
          <TeamHome />
        </Shell>
      </LiveProvider>
    );
  }

  const def = ROUTES.find((r) => r.path === route);
  const allowed = def && can(s.role, def.min);
  const Page = allowed ? PAGES[route] : undefined;
  return (
    <LiveProvider>
      <Shell route={route}>
        {Page ? <Page /> : (
          <Card title={def ? "Not permitted" : "Not found"}>
            <p>{def ? `The ${def.label} page needs the ${def.min} role.` : "There is no such page."} <a className="underline" href="#/overview">Back to the overview</a></p>
          </Card>
        )}
      </Shell>
    </LiveProvider>
  );
}
