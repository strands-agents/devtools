/**
 * Check user authorization and determine approval environment
 * @param {object} context - GitHub Actions context
 * @param {object} github - GitHub API client
 * @param {object} options - Configuration options
 * @param {string} options.username - Username to check (required)
 * @param {string} options.allowedRoles - Comma-separated list of allowed roles (required)
 * @returns {Promise<string>} 'auto-approve' or 'manual-approval'
 */

async function checkAuthorization(context, github, options) {
  if (!options.username) {
    throw new Error('Username is required but was not provided');
  }
  
  if (!options.allowedRoles) {
    throw new Error('Allowed roles are required but were not provided');
  }

  const allowedRoles = options.allowedRoles.split(',').map(r => r.trim());

  const permissionResponse = await github.rest.repos.getCollaboratorPermissionLevel({
    owner: context.repo.owner,
    repo: context.repo.repo,
    username: options.username,
  });
  const role_name = permissionResponse.data.role_name;
  const hasWriteAccess = allowedRoles.includes(role_name);
  
  if (!hasWriteAccess) {
    console.log(`User ${options.username} does not have write access to the repository (role: ${role_name}, allowed: ${allowedRoles.join(', ')})`);
    return "manual-approval";
  } else {
    console.log(`Verified ${options.username} has write access (role: ${role_name}). Auto Approving.`);
    return "auto-approve";
  }

}

module.exports = checkAuthorization;
