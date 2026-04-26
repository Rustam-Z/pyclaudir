# docs/

The top-level [README](../README.md) is the high-level intro; everything below is for people (and agents) who need to go deeper.

## Files

### [documentation.md](documentation.md)
The full technical manual for pyclaudir itself. Read this when you're
modifying, debugging, or auditing the project.

Covers: every env var, the four-process architecture in detail, how to
add tools and skills, access control internals, memory + reminders,
the four monitoring windows (live log, session replay, raw wire log,
SQLite), the complete security model with all invariants, the manual
end-to-end checklist, and the full repo layout.

### [tools.md](tools.md)
The canonical list of every tool available to the bot — always-on
pyclaudir built-ins, always-on Claude Code built-ins (web), and opt-in
groups (subagents, shell, code, Jira, GitLab) with the env var that
flips each. Read this when deciding what capabilities to grant for
your deployment.

### [deployment.md](deployment.md)
Step-by-step guide for deploying pyclaudir to a VPS (Hetzner,
DigitalOcean, Contabo…) using Docker, plus a continuous-deployment
workflow. Read this when you're moving the bot from your laptop to a
server, or wiring it into CI.

### [reference-architectures.md](reference-architectures.md)
Notes on the two systems pyclaudir descends from: Anthropic's [official
Telegram plugin](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram) and the [Rust Claudir](https://gist.github.com/nodir-t/da74c78281f203b0439609ebe5866f49). Read this *before*
proposing architectural changes — it explains which patterns came from
where and why, so you don't accidentally regress to a shape that's
already been ruled out.

## Quick reference

| You want to… | Read |
|---|---|
| Run the bot locally | [../README.md](../README.md) |
| Decide which tools to enable | [tools.md](tools.md) |
| Understand a specific env var or security rule | [documentation.md](documentation.md) |
| Deploy to a server | [deployment.md](deployment.md) |
| Propose a structural / architectural change | [reference-architectures.md](reference-architectures.md) then [documentation.md](documentation.md) |
