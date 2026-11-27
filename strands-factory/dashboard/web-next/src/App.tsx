import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { ROUTES, issueLink, pullLink, sessionLink } from "./lib/links";
import { TodayPage } from "./features/today/TodayPage";
import { SessionsPage } from "./features/strandly/SessionsPage";
import { SessionPage } from "./features/strandly/SessionPage";
import { IssuesPage } from "./features/issues/IssuesPage";
import { IssuePage } from "./features/issues/IssuePage";
import { PullsPage } from "./features/pulls/PullsPage";
import { PullPage } from "./features/pulls/PullPage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { SchedulesPage } from "./features/schedules/SchedulesPage";
import { SchedulePage } from "./features/schedules/SchedulePage";

/** Legacy `/chat/:sessionId` → `/strandly/:sessionId` (old deep links keep working). */
function LegacyChat() {
  const { sessionId } = useParams();
  return <Navigate to={sessionId ? sessionLink(sessionId) : ROUTES.strandly} replace />;
}
/** Legacy `/review/pr/:owner/:repo/:n` → PR page. */
function LegacyReview() {
  const { owner = "", repo = "", number = "" } = useParams();
  return <Navigate to={pullLink(`${owner}/${repo}`, Number(number))} replace />;
}
/** Legacy `/triage/item/:owner/:repo/:n` → issue page. */
function LegacyTriage() {
  const { owner = "", repo = "", number = "" } = useParams();
  return <Navigate to={issueLink(`${owner}/${repo}`, Number(number))} replace />;
}

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to={ROUTES.today} replace />} />
        <Route path={ROUTES.today} element={<TodayPage />} />
        <Route path={ROUTES.strandly} element={<SessionsPage />} />
        <Route path={`${ROUTES.strandly}/:sessionId`} element={<SessionPage />} />
        <Route path={ROUTES.issues} element={<IssuesPage />} />
        <Route path={`${ROUTES.issues}/:owner/:repo/:number`} element={<IssuePage />} />
        <Route path={ROUTES.pulls} element={<PullsPage />} />
        <Route path={`${ROUTES.pulls}/:owner/:repo/:number`} element={<PullPage />} />
        <Route path={ROUTES.schedules} element={<SchedulesPage />} />
        <Route path={`${ROUTES.schedules}/:id`} element={<SchedulePage />} />
        <Route path={ROUTES.settings} element={<SettingsPage />} />
        {/* Legacy routes from the previous IA. */}
        <Route path="/chat" element={<Navigate to={ROUTES.strandly} replace />} />
        <Route path="/chat/:sessionId" element={<LegacyChat />} />
        <Route path="/sessions" element={<Navigate to={ROUTES.strandly} replace />} />
        <Route path="/review/pr/:owner/:repo/:number" element={<LegacyReview />} />
        <Route path="/triage/item/:owner/:repo/:number" element={<LegacyTriage />} />
        <Route path="/reviews" element={<Navigate to={ROUTES.pulls} replace />} />
        <Route path="/triage" element={<Navigate to={ROUTES.issues} replace />} />
        <Route path="*" element={<Navigate to={ROUTES.today} replace />} />
      </Routes>
    </AppShell>
  );
}
