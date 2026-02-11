// This file assumes that its run from an environment that already has github and core imported:
// const github = require('@actions/github');
// const core = require('@actions/core');

const fs = require('fs');

async function getIssueInfo(github, context, inputs) {
  const issueId = context.eventName === 'workflow_dispatch' 
    ? inputs.issue_id
    : context.payload.issue.number.toString();
  const command = context.eventName === 'workflow_dispatch'
    ? inputs.command
    : (context.payload.comment.body.match(/^\/strands\s*(.*?)$/m)?.[1]?.trim() || '');

  console.log(`Event: ${context.eventName}, Issue ID: ${issueId}, Command: "${command}"`);

  const issue = await github.rest.issues.get({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: issueId
  });

  return { issueId, command, issue };
}

async function determineBranch(github, context, issueId, mode, isPullRequest) {
  let branchName = 'main';
  let headRepo = null;

  if (mode === 'implementer' && !isPullRequest) {
    branchName = `agent-tasks/${issueId}`;
    
    const mainRef = await github.rest.git.getRef({
      owner: context.repo.owner,
      repo: context.repo.repo,
      ref: 'heads/main'
    });
    
    try {
      console.log("Implementer started on an issue, attempting to create a branch for implementation.")
      await github.rest.git.createRef({
        owner: context.repo.owner,
        repo: context.repo.repo,
        ref: `refs/heads/${branchName}`,
        sha: mainRef.data.object.sha
      });
      console.log(`Created branch ${branchName}`);
    } catch (error) {
      console.log(`Error message: ${String(error)}`)
      console.log(`Error JSON: ${JSON.stringify(error)}`)
      if (error.message?.includes('already exists')) {
        console.log(`Branch ${branchName} already exists`);
      } else {
        console.error("Unable to create branch. Make sure you have given this job step `content: write` permission.")
        throw error;
      }
    }
  } else if (isPullRequest) {
    const pr = await github.rest.pulls.get({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: issueId
    });
    branchName = pr.data.head.ref;
    
    // Check if PR is from a fork
    const baseRepo = `${context.repo.owner}/${context.repo.repo}`;
    const prHeadRepo = pr.data.head.repo?.full_name;
    
    if (prHeadRepo && prHeadRepo !== baseRepo) {
      headRepo = prHeadRepo;
      console.log(`Detected fork PR from ${headRepo}`);
    }
  }

  return { branchName, headRepo };
}

function buildPrompts(mode, issueId, isPullRequest, command, branchName, inputs) {
  const sessionId = inputs.session_id || (mode === 'implementer' 
    ? `${mode}-${branchName}`.replace(/[\/\\]/g, '-')
    : `${mode}-${issueId}`);

  const scriptFiles = {
    'implementer': 'devtools/strands-command/agent-sops/task-implementer.sop.md',
    'refiner': 'devtools/strands-command/agent-sops/task-refiner.sop.md',
    'release-notes': 'devtools/strands-command/agent-sops/task-release-notes.sop.md',
    'reviewer': 'devtools/strands-command/agent-sops/task-reviewer.sop.md',
    'doc-writer': 'devtools/strands-command/agent-sops/task-doc-writer.sop.md'
  };
  
  const scriptFile = scriptFiles[mode] || scriptFiles['refiner'];
  const systemPrompt = fs.readFileSync(scriptFile, 'utf8');
  
  let prompt = (isPullRequest) 
    ? 'The pull request id is:'
    : 'The issue id is:';
  prompt += `${issueId}\n${command}\nreview and continue`;

  return { sessionId, systemPrompt, prompt };
}

module.exports = async (context, github, core, inputs) => {
  try {
    const { issueId, command, issue } = await getIssueInfo(github, context, inputs);
    
    const isPullRequest = !!issue.data.pull_request;
    
    // Determine mode based on explicit command first, then context
    let mode;
    if (command.startsWith('release-notes') || command.startsWith('release notes')) {
      mode = 'release-notes';
    } else if (command.startsWith('implement')) {
      mode = 'implementer';
    } else if (command.startsWith('docs')) {
      mode = 'doc-writer';
    } else if (command.startsWith('review')) {
      mode = 'reviewer';
    } else if (command.startsWith('refine')) {
      mode = 'refiner';
    } else {
      // Default behavior when no explicit command: PR -> implementer, Issue -> refiner
      mode = isPullRequest ? 'implementer' : 'refiner';
    }
    console.log(`Is PR: ${isPullRequest}, Command: "${command}", Mode: ${mode}`);

    const { branchName, headRepo } = await determineBranch(github, context, issueId, mode, isPullRequest);
    console.log(`Building prompts - mode: ${mode}, issue: ${issueId}, is PR: ${isPullRequest}`);

    const { sessionId, systemPrompt, prompt } = buildPrompts(mode, issueId, isPullRequest, command, branchName, inputs);
    
    console.log(`Session ID: ${sessionId}`);
    console.log(`Task prompt: "${prompt}"`);

    const outputs = {
      branch_name: branchName,
      session_id: sessionId,
      system_prompt: systemPrompt,
      prompt: prompt,
      issue_id: issueId,
      head_repo: headRepo
    };
    
    fs.writeFileSync('strands-parsed-input.json', JSON.stringify(outputs, null, 2));
    console.log('Wrote strands-parsed-input.json');

  } catch (error) {
    const errorMsg = `Failed: ${error.message}`;
    console.error(errorMsg);
    core.setFailed(errorMsg);
  }
};
