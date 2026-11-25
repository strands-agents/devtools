/** Deployment-selected portal identity. Empty defaults never select a real operator or org. */
export interface PortalIdentity {
  agentLogin?: string;
  githubOrgs?: string[];
  maintainersOrg?: string;
  maintainersTeamSlug?: string;
}
export let AGENT_LOGIN = "";
export const DEFAULT_TEAM_SCOPE = { orgs: [] as string[], maintainersOrg: "", maintainersTeamSlug: "" };
export const GITHUB_ORGS = DEFAULT_TEAM_SCOPE.orgs;

/** Called once from runtime config before the portal's feature tree is mounted. */
export function configurePortalIdentity(identity: PortalIdentity = {}): void {
  AGENT_LOGIN = identity.agentLogin?.trim() ?? "";
  DEFAULT_TEAM_SCOPE.orgs.splice(0, DEFAULT_TEAM_SCOPE.orgs.length, ...(identity.githubOrgs ?? []));
  DEFAULT_TEAM_SCOPE.maintainersOrg = identity.maintainersOrg?.trim() ?? "";
  DEFAULT_TEAM_SCOPE.maintainersTeamSlug = identity.maintainersTeamSlug?.trim() ?? "";
}
export function agentQueryLogin(): string {
  // Underscores are not a valid GitHub user login. Never turn an unset identity into a broad query.
  return AGENT_LOGIN || "__unconfigured__";
}
