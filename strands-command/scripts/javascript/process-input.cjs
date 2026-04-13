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

function buildPrompts(mode, issueId, isPullRequest, command, branchName, inputs, agentType) {
  const sessionId = inputs.session_id || (mode === 'implementer' 
    ? `${mode}-${branchName}`.replace(/[\/\\]/g, '-')
    : `${mode}-${issueId}`);

  // Beta agent uses BETA_SYSTEM_PROMPT.md (loaded by the runner) + skill activation.
  // The system prompt here is just a thin context layer — the real instructions come
  // from the BETA_SYSTEM_PROMPT.md file and the activated skill.
  if (agentType === 'beta') {
    // Read BETA_SYSTEM_PROMPT.md if available — provides the base system prompt
    let systemPrompt = '';
    const promptPaths = [
      'devtools/strands-command/agent-skills/BETA_SYSTEM_PROMPT.md',
      'agent-skills/BETA_SYSTEM_PROMPT.md',
    ];

    for (const promptPath of promptPaths) {
      try {
        if (fs.existsSync(promptPath)) {
          systemPrompt = fs.readFileSync(promptPath, 'utf8');
          console.log(`Loaded beta system prompt from ${promptPath}`);
          break;
        }
      } catch (e) {
        console.log(`Could not read ${promptPath}: ${e.message}`);
      }
    }

    // Fallback if file not found
    if (!systemPrompt) {
      systemPrompt = `You are an autonomous GitHub agent powered by Strands Agents SDK with extended capabilities including agent skills, sub-agent orchestration, and programmatic tool calling.`;
    }

    let prompt = (isPullRequest)
      ? 'The pull request id is:'
      : 'The issue id is:';
    prompt += `${issueId}\n${command}\nreview and continue`;

    return { sessionId, systemPrompt, prompt, mode };
  }

  // Standard agent uses SOP-based system prompts
  const scriptFiles = {
    'implementer': 'devtools/strands-command/agent-sops/task-implementer.sop.md',
    'refiner': 'devtools/strands-command/agent-sops/task-refiner.sop.md',
    'release-notes': 'devtools/strands-command/agent-sops/task-release-notes.sop.md',
    'reviewer': 'devtools/strands-command/agent-sops/task-reviewer.sop.md'
  };
  
  const scriptFile = scriptFiles[mode] || scriptFiles['refiner'];
  const systemPrompt = fs.readFileSync(scriptFile, 'utf8');
  
  let prompt = (isPullRequest) 
    ? 'The pull request id is:'
    : 'The issue id is:';
  prompt += `${issueId}\n${command}\nreview and continue`;

  return { sessionId, systemPrompt, prompt, mode };
}

module.exports = async (context, github, core, inputs) => {
  try {
    const { issueId, command, issue } = await getIssueInfo(github, context, inputs);
    
    const isPullRequest = !!issue.data.pull_request;

    // Check if this is a beta command: /strands beta <subcommand>
    let agentType = 'standard';
    let effectiveCommand = command;

    if (command.startsWith('beta ') || command === 'beta') {
      agentType = 'beta';
      effectiveCommand = command.replace(/^beta\s*/, '').trim();
      console.log(`Beta agent requested. Effective command: "${effectiveCommand}"`);
    }
    
    // Determine mode based on explicit command first, then context
    let mode;
    if (effectiveCommand.startsWith('adversarial-test') || effectiveCommand.startsWith('adversarial test')) {
      mode = 'adversarial-test';
    } else if (effectiveCommand.startsWith('release-digest') || effectiveCommand.startsWith('release digest')) {
      mode = 'release-digest';
    } else if (effectiveCommand.startsWith('meta-reason') || effectiveCommand.startsWith('meta reason')) {
      mode = 'meta-reason';
    } else if (effectiveCommand.startsWith('release-notes') || effectiveCommand.startsWith('release notes')) {
      mode = 'release-notes';
    } else if (effectiveCommand.startsWith('implement')) {
      mode = 'implementer';
    } else if (effectiveCommand.startsWith('review')) {
      mode = 'reviewer';
    } else if (effectiveCommand.startsWith('refine')) {
      mode = 'refiner';
    } else {
      // Default behavior when no explicit command: PR -> reviewer, Issue -> refiner
      mode = isPullRequest ? 'reviewer' : 'refiner';
    }

    // Beta-only modes require the beta agent
    const betaOnlyModes = ['adversarial-test', 'release-digest', 'meta-reason'];
    if (betaOnlyModes.includes(mode) && agentType !== 'beta') {
      agentType = 'beta';
      console.log(`Mode '${mode}' requires beta agent — auto-promoting to beta`);
    }

    console.log(`Is PR: ${isPullRequest}, Command: "${command}", Mode: ${mode}, Agent: ${agentType}`);

    const { branchName, headRepo } = await determineBranch(github, context, issueId, mode, isPullRequest);
    console.log(`Building prompts - mode: ${mode}, issue: ${issueId}, is PR: ${isPullRequest}`);

    const { sessionId, systemPrompt, prompt } = buildPrompts(mode, issueId, isPullRequest, effectiveCommand, branchName, inputs, agentType);
    
    console.log(`Session ID: ${sessionId}`);
    console.log(`Task prompt: "${prompt}"`);

    const outputs = {
      branch_name: branchName,
      session_id: sessionId,
      system_prompt: systemPrompt,
      prompt: prompt,
      issue_id: issueId,
      head_repo: headRepo,
      agent_type: agentType,
      agent_mode: mode,
    };
    
    fs.writeFileSync('strands-parsed-input.json', JSON.stringify(outputs, null, 2));
    console.log('Wrote strands-parsed-input.json');

  } catch (error) {
    const errorMsg = `Failed: ${error.message}`;
    console.error(errorMsg);
    core.setFailed(errorMsg);
  }
};
