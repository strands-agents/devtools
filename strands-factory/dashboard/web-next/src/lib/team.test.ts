import { afterEach, describe, expect, it } from "vitest";
import { agentQueryLogin, configurePortalIdentity, DEFAULT_TEAM_SCOPE } from "./team";
import { bucketQuery } from "../features/today/data";

afterEach(() => configurePortalIdentity({}));
describe("deployment-selected portal identity", () => {
  it("has no real operator or organization defaults", () => {
    configurePortalIdentity({});
    expect(DEFAULT_TEAM_SCOPE).toEqual({ orgs: [], maintainersOrg: "", maintainersTeamSlug: "" });
    expect(agentQueryLogin()).toBe("__unconfigured__");
  });
  it("retargets agent queries and default teams without rebuilding", () => {
    configurePortalIdentity({ agentLogin: "example-first", githubOrgs: ["example-org"], maintainersOrg: "example-org", maintainersTeamSlug: "maintainers" });
    expect(bucketQuery("strandly-pr", "viewer", DEFAULT_TEAM_SCOPE.orgs)).toContain("author:example-first");
    configurePortalIdentity({ agentLogin: "example-second", githubOrgs: ["other-org"] });
    const query = bucketQuery("strandly-pr", "viewer", DEFAULT_TEAM_SCOPE.orgs);
    expect(query).toContain("author:example-second");
    expect(query).toContain("org:other-org");
    expect(query).not.toContain("example-first");
  });
});
