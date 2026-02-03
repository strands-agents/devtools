import "./App.css";
import { HashRouter, Routes, Route } from "react-router-dom";
import { EvaluationProvider } from "./context/EvaluationContext";
import DashboardPage from "./pages/DashboardPage";
import TestResultsPage from "./pages/TestResultsPage";
import EvaluatorsPage from "./pages/EvaluatorsPage";
import TestCasesPage from "./pages/TestCasesPage";
import ScoreTrendsPage from "./pages/ScoreTrendsPage";
import AgentProgressPage from "./pages/AgentProgressPage";
import SettingsPage from "./pages/SettingsPage";

function App() {
  return (
    <HashRouter>
      <EvaluationProvider>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/results" element={<TestResultsPage />} />
          <Route path="/evaluators" element={<EvaluatorsPage />} />
          <Route path="/cases" element={<TestCasesPage />} />
          <Route path="/trends" element={<ScoreTrendsPage />} />
          <Route path="/agent-progress" element={<AgentProgressPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </EvaluationProvider>
    </HashRouter>
  );
}

export default App;
