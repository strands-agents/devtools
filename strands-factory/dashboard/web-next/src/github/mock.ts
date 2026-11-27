/**
 * Mock GitHub client — the no-token fallback on the `mock` data source (see GitHubContext). It
 * resolves a demo identity so viewer-dependent UI renders; feature reads (`graphql` / `rest`)
 * reject with MOCK_UNAVAILABLE so every page shows its "add a GitHub token in Settings" state, and
 * writes are no-ops that just log.
 */
import { MOCK_UNAVAILABLE, type GitHubClient, type GithubViewer } from "./types";

const delay = (ms = 160) => new Promise((r) => setTimeout(r, ms));

/** The demo identity. */
export const MOCK_VIEWER: GithubViewer = { login: "example-maintainer", name: "Local Maintainer (mock)" };

export class MockGitHubClient implements GitHubClient {
  async viewer(): Promise<GithubViewer> {
    await delay(120);
    return MOCK_VIEWER;
  }

  async graphql<T>(): Promise<T> {
    throw new Error(MOCK_UNAVAILABLE);
  }

  async rest<T>(): Promise<T> {
    throw new Error(MOCK_UNAVAILABLE);
  }

  async assign(repo: string, number: number, assignees: string[]): Promise<void> {
    await delay();
    console.info(`[mock] assign ${repo}#${number} → ${assignees.join(", ")}`);
  }

  async addLabels(repo: string, number: number, labels: string[]): Promise<void> {
    await delay();
    console.info(`[mock] label ${repo}#${number} + ${labels.join(", ")}`);
  }

  async removeLabel(repo: string, number: number, label: string): Promise<void> {
    await delay();
    console.info(`[mock] unlabel ${repo}#${number} − ${label}`);
  }

  async postComment(repo: string, number: number, body: string): Promise<void> {
    await delay();
    console.info(`[mock] comment on ${repo}#${number}: ${body.slice(0, 80)}`);
  }
}
