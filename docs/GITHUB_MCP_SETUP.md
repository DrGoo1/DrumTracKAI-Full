# GitHub MCP setup for ChatGPT, Windsurf, and Copilot

The repository-native workflow is most effective when the local IDE can read
GitHub issues, pull requests, review comments, and Actions results directly.
This setup is performed once per workstation; credentials and MCP configuration
must remain outside this repository.

## DrumTracKAI PC: Windsurf

Windsurf configuration is global and is stored outside the repository. The
current official configuration path is:

```text
~/.codeium/windsurf/mcp_config.json
```

On Windows this resolves under the current user's profile directory.

### Preferred secure option: official local server with OAuth

This avoids storing a PAT in the Windsurf configuration. Docker Desktop must be
installed and running.

```json
{
  "mcpServers": {
    "github": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-p",
        "127.0.0.1:8085:8085",
        "-e",
        "GITHUB_OAUTH_CALLBACK_PORT",
        "ghcr.io/github/github-mcp-server"
      ],
      "env": {
        "GITHUB_OAUTH_CALLBACK_PORT": "8085"
      }
    }
  }
}
```

After saving:

1. Open Cascade.
2. Select the MCP/hammer icon.
3. Refresh the server list.
4. Start the GitHub server and complete the browser sign-in.
5. Verify that the GitHub server has a green status indicator.

### Remote server alternative

Windsurf currently supports the remote GitHub MCP server with PAT
authentication. Use a separate fine-grained token restricted to
`DrGoo1/DrumTracKAI-Full` and the minimum issue, pull-request, Actions, and
repository-content permissions required. Never place the token in Git, task
files, run reports, screenshots, or chat logs.

```json
{
  "mcpServers": {
    "github": {
      "serverUrl": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "Authorization": "Bearer REPLACE_WITH_FINE_GRAINED_TOKEN"
      }
    }
  }
}
```

Because Windsurf's configuration is global and does not currently interpolate
environment variables, protect the configuration file with user-only operating
system permissions and rotate the token periodically.

Do not use `@modelcontextprotocol/server-github`; the former npm server is
deprecated. Use GitHub's remote server or the official Docker image.

### Verify the connection

In Cascade, ask:

```text
Read the open agent tasks in DrGoo1/DrumTracKAI-Full and summarize their task
IDs, base branches, execution targets, and acceptance criteria. Do not modify
them.
```

Then use a bounded issue with:

```text
Use the drumtrackai-engineer agent. Implement GitHub issue #<number> according
to AGENTS.md and docs/AI_ENGINEERING_CONTRACT.md. Use the issue's declared base
branch, create the run report, run the required validation, and keep the PR in
draft until PC/GPU/deployment certification is complete.
```

## Studio Mac: VS Code Copilot

### Prerequisites

- VS Code 1.101 or newer;
- GitHub Copilot signed into the GitHub account that can access this repository;
- Copilot Chat in **Agent** mode.

### Recommended OAuth setup

1. Open the VS Code Extensions panel.
2. Search for `@mcp github`.
3. Install the official **GitHub MCP Server** from the MCP registry.
4. Confirm that the server is trusted.
5. Open the command palette and run **MCP: List Servers**.
6. Select the GitHub server and complete the browser OAuth flow if prompted.

The equivalent manual `mcp.json` entry is:

```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    }
  }
}
```

OAuth is preferred because it does not require storing a personal access token
in the workspace.

## Minimum permissions and safety

Use separate credentials for the Mac and PC. Grant only what each execution
lane needs:

| Lane | Typical access |
|---|---|
| ChatGPT/reviewer | Repository, issue, PR, and Actions read; controlled review writes |
| PC/Windsurf | Repository read/write on focused branches; issues, PRs, Actions |
| Mac/Copilot | Repository read/write on focused branches; issues, PRs, Actions |

Do not grant administration, secret-management, package deletion, release
publication, or organization-wide access unless a specific task requires it and
the product owner approves it.

Model weights, S3/Render credentials, production databases, private source
audio, calibration renders, and licensed content remain outside MCP prompts and
Git commits.

## Daily operating pattern

1. Product decisions are converted into a GitHub agent-task issue or a validated
   `.ai/tasks/<task-id>.json` file.
2. Windsurf reads the issue directly and starts from its declared base branch,
   currently often `sync/render-main` for active DrumTracKAI deployment work.
3. It implements on one task branch and creates a validated run report.
4. It opens a draft PR.
5. ChatGPT or the control-plane reviewer reads the PR directly and posts review
   comments.
6. Windsurf reads those comments directly, updates the branch, and records new
   validation evidence.
7. Required PC/GPU/model/deployment/listening certification runs on the exact PR
   commit and is attached as a sanitized certification report.
8. The product owner decides when the PR may merge.

The normal prompt becomes one line:

```text
Implement GitHub issue #<number> and follow the repository agent workflow.
```

## Troubleshooting

- **Windsurf tools do not appear:** validate `mcp_config.json`, refresh the MCP
  toolbar, then restart Windsurf.
- **Docker OAuth callback fails:** verify that local port `8085` is available and
  Docker is allowed to publish it only on `127.0.0.1`.
- **Server not visible in VS Code:** run **MCP: List Servers**, confirm Agent
  mode, update VS Code, and restart the editor.
- **Access denied:** confirm that the authenticated account or fine-grained PAT
  has access to the selected repository and required issue/PR permissions.
- **Too many exposed tools:** disable unused MCP tools in the IDE and retain only
  repository, issue, pull-request, and Actions operations needed for the task.

## Official references

- GitHub MCP Server: https://github.com/github/github-mcp-server
- GitHub MCP setup documentation:
  https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/set-up-the-github-mcp-server
- Official Windsurf installation guide:
  https://github.com/github/github-mcp-server/blob/main/docs/installation-guides/install-windsurf.md
